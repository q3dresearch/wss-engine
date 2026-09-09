from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from wss import health, manifest, registry
from wss.health import run_health
from tests.conftest import write_source_yaml

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def add_row(root, source_id, day, outcome, reason=""):
    row = {col: "" for col in manifest.COLUMNS} | {
        "source_id": source_id,
        "url": "https://example.com/api",
        "fetched_at": f"2026-08-{day:02d}T22:10:03Z",
        "outcome": outcome,
        "reason": reason,
    }
    if outcome in manifest.SUCCESS_OUTCOMES:
        row |= {"http_status": "200", "content_sha256": "abc", "content_length": "100", "raw_ref": "raw/x"}
    manifest.append_row(root, row)


def health_csv(root):
    with (root / "health" / "health.csv").open(newline="") as fh:
        return {r["source_id"]: r for r in csv.DictReader(fh)}


def test_auto_disable_after_five_consecutive_failures(tmp_path):
    path = write_source_yaml(tmp_path, "fixture.demo.flaky", "https://example.com/api")
    add_row(tmp_path, "fixture.demo.flaky", 20, "first_capture")
    for day in range(21, 26):
        add_row(tmp_path, "fixture.demo.flaky", day, "error", "retries_exhausted_status_500")

    sources = registry.load_registry(tmp_path)
    disabled = run_health(tmp_path, sources, now=NOW)

    assert [d["source_id"] for d in disabled] == ["fixture.demo.flaky"]
    assert "status: auto_disabled" in path.read_text()
    assert "5 consecutive failures" in path.read_text()
    # the edit keeps the rest of the file untouched and valid
    assert registry.validate_registry(tmp_path) == []

    payload = json.loads((tmp_path / "state" / "auto_disabled.json").read_text())
    assert payload["disabled"][0]["source_id"] == "fixture.demo.flaky"
    assert payload["disabled"][0]["consecutive_failures"] == 5

    row = health_csv(tmp_path)["fixture.demo.flaky"]
    assert row["consecutive_failures"] == "5"
    assert row["status"] == "auto_disabled"
    assert row["first_success_at"] == "2026-08-20T22:10:03Z"  # coverage range start
    assert row["last_success_at"] == "2026-08-20T22:10:03Z"
    assert row["expected_interval_h"] == "168"

    # already disabled → not reported again on the next pass
    sources = registry.load_registry(tmp_path)
    assert run_health(tmp_path, sources, now=NOW) == []


def test_success_resets_the_streak(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.wobbly", "https://example.com/api")
    # all within the 28-day window of NOW (2026-09-01)
    for day in range(10, 14):
        add_row(tmp_path, "fixture.demo.wobbly", day, "quarantined", "missing_required_text")
    add_row(tmp_path, "fixture.demo.wobbly", 14, "changed")
    for day in range(15, 18):
        add_row(tmp_path, "fixture.demo.wobbly", day, "error")

    sources = registry.load_registry(tmp_path)
    assert run_health(tmp_path, sources, now=NOW) == []
    row = health_csv(tmp_path)["fixture.demo.wobbly"]
    assert row["consecutive_failures"] == "3"
    assert row["status"] == "active"
    assert row["gate_fail_rate_28d"] == "0.500"  # 4 quarantined of 8 attempts


def test_dry_run_touches_nothing(tmp_path):
    path = write_source_yaml(tmp_path, "fixture.demo.flaky", "https://example.com/api")
    for day in range(1, 8):
        add_row(tmp_path, "fixture.demo.flaky", day, "error")
    sources = registry.load_registry(tmp_path)
    disabled = run_health(tmp_path, sources, dry_run=True, now=NOW)
    assert disabled == []
    assert "status: active" in path.read_text()
    assert not (tmp_path / "state" / "auto_disabled.json").exists()
    assert (tmp_path / "health" / "health.csv").exists()


def test_skipped_rows_do_not_count_either_way(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.polite", "https://example.com/api")
    for day in range(1, 5):
        add_row(tmp_path, "fixture.demo.polite", day, "error")
    add_row(tmp_path, "fixture.demo.polite", 5, "skipped", "robots_disallowed")
    add_row(tmp_path, "fixture.demo.polite", 6, "error")
    sources = registry.load_registry(tmp_path)
    disabled = run_health(tmp_path, sources, now=NOW)
    assert [d["source_id"] for d in disabled] == ["fixture.demo.polite"]  # 5 failures, skip ignored


def add_row_at(root, source_id, stamp, outcome, reason=""):
    """Like add_row but with an explicit timestamp, so several endpoint fetches
    can share one day the way a real multi-endpoint run does."""
    row = {col: "" for col in manifest.COLUMNS} | {
        "source_id": source_id, "url": "https://example.com/api",
        "fetched_at": stamp, "outcome": outcome, "reason": reason,
    }
    if outcome in manifest.SUCCESS_OUTCOMES:
        row |= {"http_status": "200", "content_sha256": "abc",
                "content_length": "100", "raw_ref": "raw/x"}
    manifest.append_row(root, row)


def test_a_multi_endpoint_source_does_not_auto_disable_in_one_run(tmp_path):
    """Failures are counted per attempt DAY, not per endpoint fetch.

    Counting rows made the threshold mean "five runs" for a one-endpoint source
    and "one run" for a five-endpoint one: fda.recalls.cder has five yearly
    endpoints and auto-disabled the first time CI was blocked, while a
    single-endpoint source on the same blocked host survived untouched.
    """
    write_source_yaml(tmp_path, "fixture.demo.multi", "https://example.com/api")
    for i in range(5):                      # one run, five endpoints, all failed
        add_row_at(tmp_path, "fixture.demo.multi", f"2026-08-08T10:0{i}:00Z", "quarantined")
    rows = {r["source_id"]: r for r in health.compute_health(
        tmp_path, registry.load_registry(tmp_path), now=NOW)}
    assert rows["fixture.demo.multi"]["consecutive_failures"] == 1

    # a later day where anything succeeded clears the streak
    add_row_at(tmp_path, "fixture.demo.multi", "2026-08-09T10:00:00Z", "unchanged")
    add_row_at(tmp_path, "fixture.demo.multi", "2026-08-09T10:01:00Z", "quarantined")
    rows = {r["source_id"]: r for r in health.compute_health(
        tmp_path, registry.load_registry(tmp_path), now=NOW)}
    assert rows["fixture.demo.multi"]["consecutive_failures"] == 0

    # five separate days with nothing succeeding is still five
    for d in range(10, 15):
        add_row_at(tmp_path, "fixture.demo.multi", f"2026-08-{d}T10:00:00Z", "error")
    rows = {r["source_id"]: r for r in health.compute_health(
        tmp_path, registry.load_registry(tmp_path), now=NOW)}
    assert rows["fixture.demo.multi"]["consecutive_failures"] == 5


# --- throttles are not evidence -------------------------------------------
# peeringdb's three sources were refused eight times in one evening under a
# backoff too short to outlive the window enforcing it. Auto-disable is a
# one-way door, so counting a 429 the way a 404 is counted would have switched
# off three live sources over a cadence nobody had asked about.

def test_throttled_days_never_reach_the_threshold(tmp_path):
    path = write_source_yaml(tmp_path, "fixture.demo.throttled", "https://example.com/api")
    add_row(tmp_path, "fixture.demo.throttled", 20, "first_capture")
    for day in range(21, 28):       # seven straight days, past a threshold of five
        add_row(tmp_path, "fixture.demo.throttled", day, "error",
                "retries_exhausted_status_429")

    sources = registry.load_registry(tmp_path)
    assert run_health(tmp_path, sources, now=NOW) == []
    assert "status: active" in path.read_text()

    row = health_csv(tmp_path)["fixture.demo.throttled"]
    assert row["consecutive_failures"] == "0"
    assert row["consecutive_throttled"] == "7"


def test_throttled_day_neither_counts_nor_resets(tmp_path):
    """A refusal is a non-observation: it says nothing either way."""
    write_source_yaml(tmp_path, "fixture.demo.mixed", "https://example.com/api")
    add_row(tmp_path, "fixture.demo.mixed", 20, "first_capture")
    for day, reason in [(21, "bad_status_404"), (22, "throttled_status_503"),
                        (23, "bad_status_404"), (24, "throttled_status_429"),
                        (25, "bad_status_404")]:
        add_row(tmp_path, "fixture.demo.mixed", day, "error", reason)

    run_health(tmp_path, registry.load_registry(tmp_path), now=NOW)
    row = health_csv(tmp_path)["fixture.demo.mixed"]
    assert row["consecutive_failures"] == "3"      # the 404s, uninterrupted
    assert row["consecutive_throttled"] == "2"


def test_a_success_still_ends_the_run(tmp_path):
    write_source_yaml(tmp_path, "fixture.demo.recovered", "https://example.com/api")
    add_row(tmp_path, "fixture.demo.recovered", 20, "error", "bad_status_404")
    add_row(tmp_path, "fixture.demo.recovered", 21, "changed")
    add_row(tmp_path, "fixture.demo.recovered", 22, "error", "throttled_status_429")

    run_health(tmp_path, registry.load_registry(tmp_path), now=NOW)
    row = health_csv(tmp_path)["fixture.demo.recovered"]
    assert row["consecutive_failures"] == "0"
    assert row["consecutive_throttled"] == "1"


def test_quarantine_is_never_a_throttle(tmp_path):
    """A gate result is drift in the page. The bytes arrived; we rejected them.

    The reason string can still carry the digits -- `bad_status_429` is a gate
    verdict, not a rate limit -- so the classifier keys on the outcome first.
    """
    path = write_source_yaml(tmp_path, "fixture.demo.gated", "https://example.com/api")
    add_row(tmp_path, "fixture.demo.gated", 20, "first_capture")
    for day in range(21, 26):
        add_row(tmp_path, "fixture.demo.gated", day, "quarantined", "bad_status_429")

    disabled = run_health(tmp_path, registry.load_registry(tmp_path), now=NOW)
    assert [d["source_id"] for d in disabled] == ["fixture.demo.gated"]
    assert "status: auto_disabled" in path.read_text()
    assert health_csv(tmp_path)["fixture.demo.gated"]["consecutive_throttled"] == "0"


def test_a_day_that_also_succeeded_outranks_both(tmp_path):
    """Multi-endpoint sources mix outcomes within one run."""
    write_source_yaml(tmp_path, "fixture.demo.partial", "https://example.com/api")
    add_row(tmp_path, "fixture.demo.partial", 21, "error", "throttled_status_429")
    add_row(tmp_path, "fixture.demo.partial", 21, "changed")
    add_row(tmp_path, "fixture.demo.partial", 21, "error", "bad_status_404")

    run_health(tmp_path, registry.load_registry(tmp_path), now=NOW)
    row = health_csv(tmp_path)["fixture.demo.partial"]
    assert row["consecutive_failures"] == "0"
    assert row["consecutive_throttled"] == "0"
