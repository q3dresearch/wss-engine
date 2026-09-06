"""An unchanged source looked at twice must not double its observations."""

from __future__ import annotations

import csv

from wss import derive
from tests.conftest import write_source_yaml

BODY = b'{"as_of": "2026-01-15", "items": [{"id": "a", "count": 7}]}'


def _register():
    def parse(body, ctx):
        import json
        d = json.loads(body)
        for it in d["items"]:
            # observed_at from the payload — what casing-a-site asks for
            yield derive.Observation(entity_id=it["id"], metric="count",
                                     value=it["count"], unit="count",
                                     observed_at=d["as_of"] + "T00:00:00Z")
    derive.register("repeat.v1", parse, "1")


def _manifest(root, sid, rows):
    p = root / "manifest" / sid
    p.mkdir(parents=True, exist_ok=True)
    cols = ["fetched_at", "url", "outcome", "raw_ref", "content_sha256",
            "bytes", "status_code"]
    with (p / "2026-01.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)


def test_unchanged_relook_does_not_duplicate(tmp_path):
    _register()
    write_source_yaml(tmp_path, "a.b.c", "https://example.gov/x",
                      schema_id="repeat.v1")
    raw = tmp_path / "raw" / "a.b.c" / "2026" / "01"
    raw.mkdir(parents=True)
    ref = "raw/a.b.c/2026/01/20260115T000000Z-deadbeef.json"
    (tmp_path / ref).write_bytes(BODY)
    _manifest(tmp_path, "a.b.c", [
        {"fetched_at": "2026-01-15T09:00:00Z", "url": "https://example.gov/x",
         "outcome": "first_capture", "raw_ref": ref, "content_sha256": "d" * 64,
         "bytes": len(BODY), "status_code": "200"},
        {"fetched_at": "2026-01-15T18:00:00Z", "url": "https://example.gov/x",
         "outcome": "unchanged", "raw_ref": ref, "content_sha256": "d" * 64,
         "bytes": len(BODY), "status_code": "200"},
    ])

    derive.derive(tmp_path, parser_modules=[])
    rows = list(csv.DictReader(
        (tmp_path / "derived" / "observations" / "2026-01.csv").open(encoding="utf-8")))

    assert len(rows) == 1, f"expected one observation, got {len(rows)}"
    assert rows[0]["captured_at"] == "2026-01-15T09:00:00Z", \
        "should keep the earliest capture that saw this state"
