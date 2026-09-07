from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from wss import manifest, registry
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
