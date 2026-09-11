"""Health table from the manifest; auto-disable for dead sources.

At fleet scale something is always broken. Five consecutive failures flip a
source to auto_disabled and surface it in state/auto_disabled.json (the
workflow opens a GitHub issue from that file). Triage is a weekly pass over
a sorted table, never a stream of alerts.

NOT EVERY FAILURE IS EVIDENCE. A 429 or a 503 is the publisher answering --
the service exists, and the rate or the moment is wrong. Counting those
alongside a 404 meant peeringdb's three sources spent an evening walking
toward a switch-off that would have been permanent (auto-disable only ever
flips one way) over a cadence nobody had asked about. Throttled days are
counted separately, do not reach the threshold, and do not reset it either:
they are non-observations, and the fleet scan makes a run of them loud.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import manifest
from .csvio import write_csv
from .registry import CADENCE_HOURS, Source

HEALTH_COLUMNS = [
    "source_id",
    "first_success_at",  # coverage start — with last_success_at, the series' date range
    "last_success_at",
    "last_attempt_at",
    "consecutive_failures",
    "consecutive_throttled",  # trailing days refused for RATE, not counted above
    "expected_interval_h",
    "staleness_h",
    "gate_fail_rate_28d",
    "attempts_28d",  # the DENOMINATOR under gate_fail_rate_28d, without which
                     # "50% quarantined" can mean one failure out of two
    "status",
]

AUTO_DISABLE_THRESHOLD = 5

# The line is drawn at "the publisher answered". A 429 or 503 is a statement
# that the service is there and we asked wrong. A connection error or a
# timeout is indistinguishable from a host that no longer exists, so it stays
# countable -- otherwise a decommissioned URL would never disable itself.
THROTTLE_STATUSES = {"429", "503"}
# Matches the current reason (`throttled_status_429`), the historical one
# (`retries_exhausted_status_429`, which is what the eight peeringdb rows
# already in the manifest say), and the budget-exhausted suffix.
THROTTLE_REASON = re.compile(r"status_(?:429|503)")

# Best-first, so a day with any success is a success regardless of what else
# happened in it, and a day with any real breakage outranks a throttle.
_DAY_RANK = {"ok": 2, "throttled": 1, "broken": 0}


def _day_state(row: dict) -> str:
    """What one manifest row says about the day it landed in."""
    outcome = row.get("outcome", "")
    if outcome in manifest.SUCCESS_OUTCOMES:
        return "ok"
    # A quarantine is a GATE result -- the bytes arrived and failed our own
    # floor. That is drift in the page, never a rate limit.
    if outcome == "error" and (
        row.get("http_status") in THROTTLE_STATUSES
        or THROTTLE_REASON.search(row.get("reason") or "")
    ):
        return "throttled"
    return "broken"


def _hours_between(later: str, earlier: str) -> float:
    a = datetime.fromisoformat(later.replace("Z", "+00:00"))
    b = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    return (a - b).total_seconds() / 3600


def compute_health(root: Path | str, sources: list[Source], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict] = []
    for source in sources:
        first_success = ""
        last_success = ""
        last_attempt = ""
        attempts_28d = 0
        quarantined_28d = 0
        # Failures are counted per ATTEMPT DAY, not per row. A row is one
        # endpoint fetch, so counting rows made the threshold mean "five runs"
        # for a one-endpoint source and "one run" for a five-endpoint one --
        # fda.recalls.cder has five yearly endpoints and auto-disabled on its
        # first blocked run, while a single-endpoint source on the same blocked
        # host survived. A 304-endpoint source would have scored 304.
        by_day: dict[str, str] = {}       # day -> ok | throttled | broken
        for row in manifest.iter_rows(root, source.source_id):
            outcome = row.get("outcome", "")
            if outcome == "skipped":
                continue
            last_attempt = row["fetched_at"]
            day = row["fetched_at"][:10]
            if outcome in manifest.SUCCESS_OUTCOMES:
                first_success = first_success or row["fetched_at"]
                last_success = row["fetched_at"]
            if outcome in manifest.SUCCESS_OUTCOMES or outcome in manifest.FAILURE_OUTCOMES:
                state = _day_state(row)
                prev = by_day.get(day)
                if prev is None or _DAY_RANK[state] > _DAY_RANK[prev]:
                    by_day[day] = state
            if _hours_between(now_iso, row["fetched_at"]) <= 28 * 24:
                attempts_28d += 1
                if outcome == "quarantined":
                    quarantined_28d += 1
        # A day counts against the source only if NOTHING succeeded that day --
        # and a throttled day counts against nothing at all. It neither
        # increments the run nor resets it, because "you asked too fast" is no
        # evidence either way about whether the source still exists.
        consecutive = 0
        throttled_days = 0
        for day in sorted(by_day, reverse=True):
            state = by_day[day]
            if state == "ok":
                break
            if state == "throttled":
                throttled_days += 1
            else:
                consecutive += 1
        staleness = f"{_hours_between(now_iso, last_success):.1f}" if last_success else ""
        fail_rate = f"{quarantined_28d / attempts_28d:.3f}" if attempts_28d else ""
        out.append(
            {
                "source_id": source.source_id,
                "first_success_at": first_success,
                "last_success_at": last_success,
                "last_attempt_at": last_attempt,
                "consecutive_failures": consecutive,
                "consecutive_throttled": throttled_days,
                "expected_interval_h": CADENCE_HOURS[source.cadence],
                "staleness_h": staleness,
                "gate_fail_rate_28d": fail_rate,
                # Ship the denominator beside the rate. It was computed here
                # and discarded, so `wss fleet-scan` reported "50% of fetches
                # quarantined over 28d" on a source with two fetches -- the
                # fleet's own counts-need-denominators rule, broken by the tool
                # that enforces everything else.
                "attempts_28d": attempts_28d,
                "status": source.status,
            }
        )
    # Worst first: most consecutive failures, then stalest (never-succeeded counts as stalest).
    out.sort(
        key=lambda r: (
            -int(r["consecutive_failures"]),
            -(float(r["staleness_h"]) if r["staleness_h"] else 1e12),
            r["source_id"],
        )
    )
    return out


def apply_auto_disable(
    root: Path | str,
    sources: list[Source],
    health_rows: list[dict],
    threshold: int = AUTO_DISABLE_THRESHOLD,
    now: datetime | None = None,
) -> list[dict]:
    """Flip active sources past the failure threshold to auto_disabled.

    Edits only the status line of the registry file, in place, so the rest of
    the entry (comments included) is untouched. Returns the newly disabled.
    """
    now = now or datetime.now(timezone.utc)
    by_id = {s.source_id: s for s in sources}
    disabled: list[dict] = []
    for row in health_rows:
        source = by_id.get(row["source_id"])
        if source is None or source.status != "active":
            continue
        if int(row["consecutive_failures"]) < threshold:
            continue
        text = source.path.read_text(encoding="utf-8")
        new_text, n = re.subn(
            r"^status:.*$",
            f"status: auto_disabled  # was active; auto-disabled {now:%Y-%m-%d} "
            f"after {row['consecutive_failures']} consecutive failures",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        if n != 1:
            raise RuntimeError(f"could not rewrite status line in {source.path}")
        source.path.write_text(new_text, encoding="utf-8")
        row["status"] = "auto_disabled"
        disabled.append(
            {
                "source_id": source.source_id,
                "consecutive_failures": int(row["consecutive_failures"]),
                "consecutive_throttled": int(row.get("consecutive_throttled") or 0),
                "last_success_at": row["last_success_at"],
                "last_attempt_at": row["last_attempt_at"],
                "registry_file": str(source.path.name),
            }
        )
    return disabled


def write_health(root: Path | str, health_rows: list[dict]) -> Path:
    path = Path(root) / "health" / "health.csv"
    write_csv(path, HEALTH_COLUMNS, health_rows)
    return path


def write_auto_disabled(root: Path | str, disabled: list[dict], now: datetime | None = None) -> Path:
    """Always written (empty list included) so workflows can read it blindly."""
    now = now or datetime.now(timezone.utc)
    state_dir = Path(root) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "auto_disabled.json"
    payload = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "disabled": disabled}
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def run_health(
    root: Path | str,
    sources: list[Source],
    threshold: int = AUTO_DISABLE_THRESHOLD,
    dry_run: bool = False,
    now: datetime | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> list[dict]:
    rows = compute_health(root, sources, now=now)
    disabled: list[dict] = []
    if not dry_run:
        disabled = apply_auto_disable(root, sources, rows, threshold=threshold, now=now)
    write_health(root, rows)
    if not dry_run:
        write_auto_disabled(root, disabled, now=now)
    for d in disabled:
        log(f"auto_disabled {d['source_id']} after {d['consecutive_failures']} consecutive failures")
    return disabled
