"""Generate a synthetic data root in the real capture shape, ground truth planted.

Four entities with known trajectories:

    sandbox/legacy-gpt       incumbent decaying  (~1.5%/day down)
    sandbox/nova-lm          challenger accelerating (~6%/day up, appears day 20)
    sandbox/steady-diffuser  plateau (flat ± 2% noise)
    sandbox/flash-1b         faded — collapses after day 30, drops out of the
                             listing entirely once below 1,000 downloads

Also planted, to exercise the capture contract end to end:

    day 45  quarantined response ("Access Denied") — a gap, never archived
    day 60  byte-identical body — outcome `unchanged`, no file write, but a
            manifest row (and therefore a dated observation) all the same

Everything is deterministic for a given (days, seed): analysis built on this
data can assert exact recovery of the planted truth before real data exists.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wss import manifest, storage  # noqa: E402

SOURCE_ID = "sandbox.models.demo"
END_DATE = date(2026, 8, 31)
UNCHANGED_DAY = 60
QUARANTINE_DAY = 45
NOVA_FIRST_DAY = 20
FLASH_BREAK_DAY = 30

REGISTRY_YAML = f"""\
source_id: {SOURCE_ID}
status: active
cadence: daily
schema_id: sandbox.v1

publisher: Sandbox
publisher_tier: first_party
destroys_own_history: true
licence: "synthetic data, no licence needed"
personal_data: none
storage: git

endpoints:
  - url: https://sandbox.invalid/api/models
    delay_seconds: 0

gates:
  expect_status: 200
  min_bytes: 20
  content_type_any: [json]
  must_not_contain: ["Access Denied"]
"""


def _downloads(entity: str, day: int, rng: random.Random) -> int | None:
    """Planted trajectories. None means absent from the listing that day."""
    if entity == "legacy-gpt":
        return round(1_000_000 * 0.985**day)
    if entity == "nova-lm":
        if day < NOVA_FIRST_DAY:
            return None
        return round(5_000 * 1.06 ** (day - NOVA_FIRST_DAY))
    if entity == "steady-diffuser":
        return round(250_000 * (1 + rng.uniform(-0.02, 0.02)))
    if entity == "flash-1b":
        value = 400_000 if day < FLASH_BREAK_DAY else 400_000 * 0.88 ** (day - FLASH_BREAK_DAY)
        return round(value) if value >= 1_000 else None
    raise ValueError(entity)


def _likes(entity: str, day: int) -> int:
    return {
        "legacy-gpt": 5_000 + day * 2,
        "nova-lm": max(0, (day - NOVA_FIRST_DAY) * 15),
        "steady-diffuser": 3_000 + day,
        "flash-1b": 2_500,
    }[entity]


def _created_at(entity: str, start: date) -> str:
    fixed = {
        "legacy-gpt": "2024-01-15T00:00:00.000Z",
        "steady-diffuser": "2024-06-01T00:00:00.000Z",
        "flash-1b": "2025-11-01T00:00:00.000Z",
    }
    if entity in fixed:
        return fixed[entity]
    nova_day = start + timedelta(days=NOVA_FIRST_DAY)
    return f"{nova_day.isoformat()}T09:00:00.000Z"


def body_for_day(day: int, start: date, seed: int) -> bytes:
    items = []
    for entity in ("legacy-gpt", "nova-lm", "steady-diffuser", "flash-1b"):
        rng = random.Random(f"{seed}:{entity}:{day}")
        downloads = _downloads(entity, day, rng)
        if downloads is None:
            continue
        items.append(
            {
                "id": f"sandbox/{entity}",
                "downloads": downloads,
                "likes": _likes(entity, day),
                "createdAt": _created_at(entity, start),
            }
        )
    items.sort(key=lambda i: (-i["downloads"], i["id"]))
    return json.dumps(items, separators=(",", ":")).encode()


def simulate(out_root: Path | str, days: int = 120, seed: int = 7) -> dict:
    """Build registry + raw + manifest under out_root. Returns the ground truth."""
    root = Path(out_root)
    start = END_DATE - timedelta(days=days - 1)
    (root / "registry").mkdir(parents=True, exist_ok=True)
    (root / "registry" / f"{SOURCE_ID}.yml").write_text(REGISTRY_YAML, encoding="utf-8")
    store = storage.LocalGitStore(root)
    url = "https://sandbox.invalid/api/models"

    prev_body: bytes | None = None
    prev_ref = ""
    for day in range(days):
        day_date = start + timedelta(days=day)
        fetched_dt = datetime(day_date.year, day_date.month, day_date.day, 22, 10, 3, tzinfo=timezone.utc)
        fetched_at = fetched_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {col: "" for col in manifest.COLUMNS} | {
            "source_id": SOURCE_ID,
            "url": url,
            "fetched_at": fetched_at,
        }

        if day == QUARANTINE_DAY:
            bad = b"<html>Access Denied - your request was blocked</html>"
            sha = hashlib.sha256(bad).hexdigest()
            ref = storage.quarantine_path(SOURCE_ID, fetched_dt, sha, "html")
            store.write(ref, bad)
            row |= {
                "http_status": "200",
                "content_type": "text/html",
                "content_length": str(len(bad)),
                "content_sha256": sha,
                "outcome": "quarantined",
                "raw_ref": ref,
                "reason": "contains_forbidden_text",
            }
            manifest.append_row(root, row)
            continue

        body = prev_body if (day == UNCHANGED_DAY and prev_body is not None) else body_for_day(day, start, seed)
        sha = hashlib.sha256(body).hexdigest()
        row |= {
            "http_status": "200",
            "content_type": "application/json",
            "content_length": str(len(body)),
            "content_sha256": sha,
        }
        if prev_body is not None and body == prev_body:
            row |= {"outcome": "unchanged", "raw_ref": prev_ref}
        else:
            ref = storage.raw_path(SOURCE_ID, fetched_dt, sha, "json")
            store.write(ref, body)
            row |= {"outcome": "changed" if prev_body is not None else "first_capture", "raw_ref": ref}
            prev_ref = ref
        prev_body = body
        manifest.append_row(root, row)

    day_iso = lambda d: (start + timedelta(days=d)).isoformat()  # noqa: E731
    return {
        "source_id": SOURCE_ID,
        "start_date": start.isoformat(),
        "end_date": END_DATE.isoformat(),
        "days": days,
        "unchanged_date": day_iso(UNCHANGED_DAY),
        "quarantine_date": day_iso(QUARANTINE_DAY),
        "nova_first_date": day_iso(NOVA_FIRST_DAY),
        "entities": {
            "sandbox/legacy-gpt": "incumbent decaying",
            "sandbox/nova-lm": "challenger accelerating",
            "sandbox/steady-diffuser": "plateau",
            "sandbox/flash-1b": "faded",
        },
    }


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "out")
    truth = simulate(out)
    print(json.dumps(truth, indent=2))
