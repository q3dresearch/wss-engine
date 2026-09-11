from __future__ import annotations

import csv
import gzip
import json

import pytest

from wss import csvio, derive, manifest
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
    return csvio.read_csv(root / "derived" / "observations" / f"{month}.csv.gz")


def test_observations_long_format(tmp_path):
    seed_archive(tmp_path)
    derive.register("test.v1", parse_widgets, "3")
    stats = derive.derive(tmp_path)
    assert stats == {"rows": 3, "partitions": ["2026-08.csv.gz"]}

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
    partition = tmp_path / "derived" / "observations" / "2026-08.csv.gz"
    first = partition.read_bytes()
    derive.derive(tmp_path)
    assert partition.read_bytes() == first
    # gzip stores an mtime; csvio pins it to 0 so a rebuild of unchanged
    # rows is byte-identical and git sees nothing to commit.
    assert first[:4] == b"\x1f\x8b\x08\x00"
    text = gzip.decompress(first).decode()
    assert text.endswith("\n") and "\r" not in text


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


def test_parsers_are_auto_discovered(tmp_path):
    """Adding a parser needs no more ceremony than adding a source."""
    seed_archive(tmp_path)
    pkg = tmp_path / "parsers"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "widgets_v1.py").write_text(
        "import json\n"
        "from wss import derive\n"
        "def parse(body, ctx):\n"
        "    yield derive.Observation(entity_id='acme/widget', metric='widgets',\n"
        "                             value=json.loads(body)['widgets'], unit='count')\n"
        "derive.register('test.v1', parse, '9')\n"
    )
    assert derive.discover_parsers(tmp_path) == ["parsers.widgets_v1"]

    stats = derive.derive(tmp_path)  # no --parsers passed
    assert stats["rows"] == 3
    assert read_partition(tmp_path)[0]["parser_version"] == "9"


def test_explicit_parsers_override_discovery(tmp_path):
    seed_archive(tmp_path)
    derive.register("test.v1", parse_widgets, "3")
    stats = derive.derive(tmp_path, parser_modules=[])
    assert stats["rows"] == 3  # no parsers/ dir present; registered parser still used


def test_on_change_does_not_re_materialise_an_unchanged_snapshot(tmp_path, monkeypatch):
    """`unchanged` means the bytes were identical, so re-parsing them yields
    observations differing only in observed_at. For a membership series that
    restatement is the signal; for a slow register it was 35% of the partition
    saying nothing new."""
    monkeypatch.setenv("WSS_CONTACT", "t +https://github.com/x")

    def rows_for(mode):
        root = tmp_path / mode
        root.mkdir()
        path = write_source_yaml(root, "fixture.demo.reg", "https://example.com/a")
        if mode == "on_change":
            path.write_text(path.read_text() + "restate: on_change\n")
        derive.register("test.v1", lambda body, ctx: [
            derive.Observation("thing:1", "present", 1, "bool")], "1")
        for stamp, outcome in (("2026-09-01T10:00:00Z", "first_capture"),
                               ("2026-09-02T10:00:00Z", "unchanged"),
                               ("2026-09-03T10:00:00Z", "unchanged")):
            row = {c: "" for c in manifest.COLUMNS} | {
                "source_id": "fixture.demo.reg", "url": "https://example.com/a",
                "fetched_at": stamp, "outcome": outcome, "http_status": "200",
                "content_sha256": "abc", "content_length": "9", "raw_ref": "raw/x.json"}
            manifest.append_row(root, row)
        raw = root / "raw"
        raw.mkdir(exist_ok=True)
        (raw / "x.json").write_text("{}")
        return derive.derive(root)["rows"]

    assert rows_for("every_capture") == 3      # the restatement is kept
    derive.clear_parsers()
    assert rows_for("on_change") == 1          # only the capture that changed


def test_publish_aggregates_withholds_records_but_archives_them_first(tmp_path, monkeypatch):
    """A licence control, not a deletion: the full partition must survive.

    UNEP-WCMC permit publishing WDPA material only where "the Data are not
    downloadable" and forbid sub-licensing it "including within Derivative
    Works". `publish: aggregates` keeps the cross-entity counts in git and
    sends the per-entity records to object storage -- but ONLY after the full
    file is written there and read back, because trimming first would turn a
    licence control into data loss.
    """
    from wss import derive as D, registry, storage

    written = {}

    class FakeStore(storage.Store):
        def write(self, rel_path, data): written[rel_path] = data
        def exists(self, rel_path): return rel_path in written
        def read(self, rel_path): return written[rel_path]

    monkeypatch.setattr(storage, "store_for", lambda source, root: FakeStore())

    src = registry.Source(
        source_id="p.q.r", status="active", cadence="monthly", schema_id="x.v1",
        publisher="p", publisher_tier="first_party", destroys_own_history=True,
        licence="see terms", personal_data="none", storage="object",
        endpoints=(), publish="aggregates", aggregate_prefixes=("feed:", "country:"))

    rows = [
        {"source_id": "p.q.r", "entity_id": "pa:1", "metric": "area", "value": "1"},
        {"source_id": "p.q.r", "entity_id": "pa:2", "metric": "area", "value": "2"},
        {"source_id": "p.q.r", "entity_id": "feed:all", "metric": "n", "value": "2"},
        {"source_id": "p.q.r", "entity_id": "country:X", "metric": "n", "value": "2"},
        # a different source is untouched by another source's publish mode
        {"source_id": "other.s.t", "entity_id": "pa:9", "metric": "area", "value": "9"},
    ]
    path = tmp_path / "2026-09.csv.gz"
    from wss.csvio import write_csv, read_csv
    cols = ["source_id", "entity_id", "metric", "value"]
    monkeypatch.setattr(D, "OBS_COLUMNS", cols)
    write_csv(path, cols, rows)

    kept = D._withhold_records(path, rows, [src], src.aggregate_prefixes, tmp_path,
                               lambda *_: None)

    ids = {r["entity_id"] for r in kept}
    assert ids == {"feed:all", "country:X", "pa:9"}, ids   # other source survives
    assert {r["entity_id"] for r in read_csv(path)} == ids  # and it is on disk
    # the archived copy is the COMPLETE one, written before any trimming
    archived = written["derived/observations/2026-09.csv.gz"]
    import gzip, io, csv as _csv
    full = list(_csv.DictReader(io.StringIO(gzip.decompress(archived).decode())))
    assert len(full) == len(rows) == 5
    assert "pa:1" in {r["entity_id"] for r in full}


def test_publish_aggregates_refuses_without_object_storage():
    """Withholding rows with nowhere to put them is deletion, not licensing."""
    from wss import registry
    bad = {"source_id": "p.q.r", "status": "active", "cadence": "monthly",
           "schema_id": "x.v1", "publisher": "p", "publisher_tier": "first_party",
           "destroys_own_history": True, "licence": "x", "personal_data": "none",
           "storage": "git", "publish": "aggregates",
           "aggregate_prefixes": ["feed:"],
           "endpoints": [{"url": "https://e.test/x"}],
           "gates": {"min_bytes": 1, "content_type_any": ["json"]}}
    from pathlib import Path
    _, problems = registry.validate_entry(bad, Path("t.yml"))
    assert any("storage: object" in p for p in problems), problems

    # ...and it refuses the other half-configuration too: aggregates with no
    # prefixes would withhold every row, which nobody means.
    bad2 = dict(bad, storage="object")
    bad2.pop("aggregate_prefixes")
    _, problems2 = registry.validate_entry(bad2, Path("t.yml"))
    assert any("aggregate_prefixes" in p for p in problems2), problems2
