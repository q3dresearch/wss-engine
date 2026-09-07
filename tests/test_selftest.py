"""Self-test (acceptance criteria 3 & 4): drives every outcome against a local
fixture server on an OS-assigned port, then proves the fleet path — plan,
deterministic shards, capture per shard — with three dummy registry entries.
No network leaves the machine."""

from __future__ import annotations

import json

import pytest

from wss import cli, manifest
from tests.conftest import write_source_yaml
from tests.fixture_server import FixtureServer

SOURCES = {
    "fixture.demo.alpha": "/alpha",
    "fixture.demo.beta": "/beta",
    "fixture.demo.gamma": "/gamma",
}


def good_body(name: str, version: int) -> str:
    return json.dumps({"name": name, "downloads": 100 + version, "pad": "x" * 30})


def make_fleet(root, server: FixtureServer) -> None:
    server.allow_all_robots()
    for source_id, path in SOURCES.items():
        server.set(path, good_body(path, 1))
        write_source_yaml(root, source_id, server.url + path)


def run(root, *argv) -> int:
    return cli.main(["--root", str(root), *argv])


def rows_for(root, source_id):
    return list(manifest.iter_rows(root, source_id))


def raw_file_count(root) -> int:
    return sum(1 for p in (root / "raw").rglob("*") if p.is_file())


@pytest.mark.usefixtures("contact_env")
def test_selftest_all_outcomes(tmp_path):
    root = tmp_path
    with FixtureServer() as server:
        make_fleet(root, server)

        # first_capture — and a heartbeat even on the very first run
        assert run(root, "capture", "--cadence", "weekly", "--shard", "1/1") == 0
        for source_id in SOURCES:
            assert [r["outcome"] for r in rows_for(root, source_id)] == ["first_capture"]
        assert raw_file_count(root) == 3
        heartbeat = json.loads((root / "state" / "last_run.json").read_text())
        assert heartbeat["outcomes"] == {"first_capture": 3}

        # unchanged — dedupe skips the file write, never the manifest row
        assert run(root, "capture", "--cadence", "weekly", "--shard", "1/1") == 0
        assert raw_file_count(root) == 3
        for source_id in SOURCES:
            last = rows_for(root, source_id)[-1]
            assert last["outcome"] == "unchanged"
            assert last["raw_ref"]  # points at the original capture
        heartbeat = json.loads((root / "state" / "last_run.json").read_text())
        assert heartbeat["outcomes"] == {"unchanged": 3}  # heartbeat even when nothing changed

        # changed — only the mutated source gains a raw file
        server.set("/alpha", good_body("/alpha", 2))
        assert run(root, "capture", "--cadence", "weekly", "--shard", "1/1") == 0
        assert rows_for(root, "fixture.demo.alpha")[-1]["outcome"] == "changed"
        assert rows_for(root, "fixture.demo.beta")[-1]["outcome"] == "unchanged"
        assert raw_file_count(root) == 4

        # quarantined — gate failure, loud exit, bytes kept out of raw/
        server.set("/beta", json.dumps({"error": "Access Denied", "pad": "x" * 30}))
        assert run(root, "capture", "--cadence", "weekly", "--shard", "1/1") == 1
        beta_last = rows_for(root, "fixture.demo.beta")[-1]
        assert beta_last["outcome"] == "quarantined"
        assert beta_last["reason"] == "contains_forbidden_text"
        assert beta_last["raw_ref"].startswith("quarantine/")
        assert (root / beta_last["raw_ref"]).is_file()
        assert raw_file_count(root) == 4  # nothing entered the archive

        # error — retries exhausted on 500s, loud exit
        server.set("/beta", good_body("/beta", 1))
        server.set("/gamma", "boom", status=500, content_type="text/plain")
        assert run(root, "capture", "--cadence", "weekly", "--shard", "1/1") == 1
        gamma_last = rows_for(root, "fixture.demo.gamma")[-1]
        assert gamma_last["outcome"] == "error"
        assert gamma_last["reason"] == "retries_exhausted_status_500"

        # skipped — robots.txt honoured (fresh Fetcher per run re-reads robots)
        server.set("/gamma", good_body("/gamma", 1))
        server.set("/robots.txt", "User-agent: *\nDisallow: /alpha\n", content_type="text/plain")
        assert run(root, "capture", "--cadence", "weekly", "--shard", "1/1") == 0
        alpha_last = rows_for(root, "fixture.demo.alpha")[-1]
        assert alpha_last["outcome"] == "skipped"
        assert alpha_last["reason"] == "robots_disallowed"

        # every fetch appended a manifest row — 6 runs, 6 rows per source
        for source_id in SOURCES:
            assert len(rows_for(root, source_id)) == 6


@pytest.mark.usefixtures("contact_env")
def test_fleet_path_plan_shard_capture(tmp_path, capsys):
    """Three dummy sources prove registry → plan → matrix → capture end to end."""
    root = tmp_path
    with FixtureServer() as server:
        make_fleet(root, server)

        assert run(root, "plan", "--cadence", "weekly", "--shards", "3") == 0
        shards = json.loads(capsys.readouterr().out.strip())
        assert run(root, "plan", "--cadence", "weekly", "--shards", "3") == 0
        assert json.loads(capsys.readouterr().out.strip()) == shards  # deterministic

        assert shards, "three sources must occupy at least one shard"
        assert all(s.endswith("/3") for s in shards)

        for shard in shards:
            assert run(root, "capture", "--cadence", "weekly", "--shard", shard) == 0
        capsys.readouterr()  # drop capture logs

        # every source captured exactly once across the whole matrix
        for source_id in SOURCES:
            assert [r["outcome"] for r in rows_for(root, source_id)] == ["first_capture"]

        # wrong-cadence plan is empty → workflow matrix is skipped entirely
        assert run(root, "plan", "--cadence", "monthly", "--shards", "3") == 0
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == []


def test_capture_requires_contact(tmp_path, monkeypatch):
    monkeypatch.delenv("WSS_CONTACT", raising=False)
    write_source_yaml(tmp_path, "fixture.demo.alpha", "http://127.0.0.1:9/x")
    assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 2
