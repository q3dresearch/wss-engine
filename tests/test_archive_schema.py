"""Capture-only sources: archive a document and watch it for silent revisions.

Court opinions, IR decks and terms-of-service pages exist to be archived and
watched, not measured. `schema_id: archive.v1` needs no user parser.
"""

from __future__ import annotations

import csv

import pytest

from wss import csvio, cli, derive, manifest
from tests.conftest import write_source_yaml
from tests.fixture_server import FixtureServer

OPINION_V1 = "<html><body>" + "Opinion of the Court. " * 40 + "</body></html>"
OPINION_V2 = OPINION_V1.replace("Opinion of the Court.", "Opinion of the Court (revised).", 1)


def observations(root):
    rows = []
    for path in csvio.partition_paths(root / "derived" / "observations"):
        rows.extend(csvio.read_csv(path))
    return rows


def test_archive_schema_needs_no_user_parser(tmp_path, contact_env):
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/opinion", OPINION_V1, content_type="text/html")
        write_source_yaml(
            tmp_path,
            "scotus.opinions.slip",
            server.url + "/opinion",
            schema_id=derive.ARCHIVE_SCHEMA,
            gates="gates:\n  expect_status: 200\n  min_bytes: 10\n  content_type_any: [html]\n",
        )

        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0
        # a silent revision the day after
        server.set("/opinion", OPINION_V2, content_type="text/html")
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0

    # derive works with no --parsers at all: the built-in covers archive.v1
    assert cli.main(["--root", str(tmp_path), "derive"]) == 0

    outcomes = [r["outcome"] for r in manifest.iter_rows(tmp_path, "scotus.opinions.slip")]
    assert outcomes == ["first_capture", "changed"]  # the revision was detected

    rows = observations(tmp_path)
    assert [r["metric"] for r in rows] == ["content_bytes", "content_bytes"]
    sizes = [int(r["value"]) for r in rows]
    assert sizes[1] != sizes[0]  # size shift is the queryable revision signal
    assert all(r["unit"] == "bytes" for r in rows)
    # both versions are in the archive, each cited by its own row
    assert len({r["raw_ref"] for r in rows}) == 2


def test_unrelated_missing_parser_is_still_loud(tmp_path):
    write_source_yaml(tmp_path, "demo.web.thing", "https://example.com/x", status="paused")
    manifest.append_row(
        tmp_path,
        {col: "" for col in manifest.COLUMNS}
        | {
            "source_id": "demo.web.thing",
            "url": "https://example.com/x",
            "fetched_at": "2026-08-01T00:00:00Z",
            "outcome": "first_capture",
            "raw_ref": "raw/x",
        },
    )
    with pytest.raises(derive.DeriveError, match="test.v1"):
        derive.derive(tmp_path)
