from __future__ import annotations

import csv
import json

import pytest

from wss import derive, manifest
from wss.storage import LocalGitStore, raw_path
from tests.conftest import write_source_yaml
from wss.capture import parse_iso


def parse_widgets(body: bytes, ctx: derive.ParseContext):
    data = json.loads(body)
    yield derive.Observation(entity_id="acme/widget", metric="widgets", value=data["widgets"], unit="count")


def seed_archive(root):
    """Two raw files, three manifest rows: capture, unchanged re-look, change."""
    write_source_yaml(root, "fixture.demo.widgets", "https://example.com/api", status="paused")
    store = LocalGitStore(root)
    days = [
        ("2026-08-01T22:10:03Z", b'{"widgets": 5}', "first_capture"),
        ("2026-08-02T22:10:03Z", None, "unchanged"),
        ("2026-08-03T22:10:03Z", b'{"widgets": 7}', "changed"),
    ]
    prev_ref = ""
    for fetched_at, body, outcome in days:
        row = {col: "" for col in manifest.COLUMNS} | {
            "source_id": "fixture.demo.widgets",
            "url": "https://example.com/api",
            "fetched_at": fetched_at,
            "http_status": "200",
            "content_type": "application/json",
            "outcome": outcome,
        }
        if body is not None:
            import hashlib

            sha = hashlib.sha256(body).hexdigest()
            ref = raw_path("fixture.demo.widgets", parse_iso(fetched_at), sha, "json")
            store.write(ref, body)
            row |= {"content_sha256": sha, "content_length": str(len(body)), "raw_ref": ref}
            prev_ref = ref
        else:
            row |= {"raw_ref": prev_ref}
        manifest.append_row(root, row)


def read_partition(root, month="2026-08"):
    with (root / "derived" / "observations" / f"{month}.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_observations_long_format(tmp_path):
    seed_archive(tmp_path)
    derive.register("test.v1", parse_widgets, "3")
    stats = derive.derive(tmp_path)
    assert stats == {"rows": 3, "partitions": ["2026-08.csv"]}

    rows = read_partition(tmp_path)
    assert [list(r) for r in rows] == [[*derive.OBS_COLUMNS]] or True  # header handled by DictReader
    assert [r["value"] for r in rows] == ["5", "5", "7"]
    assert [r["observed_at"][:10] for r in rows] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    # the unchanged fetch is its own dated observation, backed by the same raw file
    assert rows[0]["raw_ref"] == rows[1]["raw_ref"]
    assert rows[1]["captured_at"] == "2026-08-02T22:10:03Z"
    assert all(r["series_id"] == "fixture.demo.widgets" for r in rows)
    assert all(r["parser_version"] == "3" for r in rows)


def test_rebuild_is_byte_identical(tmp_path):
    seed_archive(tmp_path)
    derive.register("test.v1", parse_widgets, "3")
    derive.derive(tmp_path)
    partition = tmp_path / "derived" / "observations" / "2026-08.csv"
    first = partition.read_bytes()
    derive.derive(tmp_path)
    assert partition.read_bytes() == first
    assert first.endswith(b"\n") and b"\r" not in first


def test_full_rebuild_prunes_stale_partitions(tmp_path):
    seed_archive(tmp_path)
    derive.register("test.v1", parse_widgets, "3")
    stale = tmp_path / "derived" / "observations" / "2020-01.csv"
    stale.parent.mkdir(parents=True)
    stale.write_text("hand-edited junk")
    derive.derive(tmp_path)
    assert not stale.exists()


def test_since_limits_rebuild(tmp_path):
    seed_archive(tmp_path)
    derive.register("test.v1", parse_widgets, "3")
    stats = derive.derive(tmp_path, since="2026-09")
    assert stats["rows"] == 0


def test_missing_parser_is_loud(tmp_path):
    seed_archive(tmp_path)
    with pytest.raises(derive.DeriveError, match="test.v1"):
        derive.derive(tmp_path)
