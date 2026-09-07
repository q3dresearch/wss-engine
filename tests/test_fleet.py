from __future__ import annotations

import csv
import textwrap
from pathlib import Path

from wss import fleet

from .conftest import write_source_yaml


def make_repo(
    root: Path,
    name: str,
    *,
    cadence: str = "weekly",
    workflow_cadence: str | None = None,
    capture_cron: str = "10 22 * * 1",
    derive_cron: str = "20 0 * * 2",
    pin: str = "v0.6.4",
    sources: int = 2,
    health: bool = True,
) -> Path:
    repo = root / name
    (repo / ".github" / "workflows").mkdir(parents=True)
    ids = []
    for i in range(sources):
        sid = f"fixture.demo.s{i}"
        ids.append(sid)
        write_source_yaml(repo, sid, f"https://example.com/{i}", cadence=cadence)

    (repo / ".github" / "workflows" / f"capture-{cadence}.yml").write_text(
        textwrap.dedent(
            f"""\
            name: capture-{cadence}
            on:
              schedule:
                - cron: "{capture_cron}"
            env:
              CADENCE: {workflow_cadence or cadence}
              ENGINE_SPEC: "wss @ git+https://github.com/o/wss-engine.git@{pin}"
            """
        )
    )
    (repo / ".github" / "workflows" / "derive.yml").write_text(
        f'name: derive\non:\n  schedule:\n    - cron: "{derive_cron}"\n'
    )
    (repo / "requirements.txt").write_text(
        f"wss @ git+https://github.com/o/wss-engine.git@{pin}\n"
    )

    if health:
        (repo / "health").mkdir()
        with (repo / "health" / "health.csv").open("w", newline="") as fh:
            w = csv.DictWriter(
                fh,
                fieldnames=[
                    "source_id", "first_success_at", "last_success_at", "last_attempt_at",
                    "consecutive_failures", "expected_interval_h", "staleness_h",
                    "gate_fail_rate_28d", "status",
                ],
            )
            w.writeheader()
            for sid in ids:
                w.writerow(
                    {
                        "source_id": sid, "first_success_at": "", "last_success_at": "",
                        "last_attempt_at": "", "consecutive_failures": "0",
                        "expected_interval_h": "168", "staleness_h": "10",
                        "gate_fail_rate_28d": "0.0", "status": "active",
                    }
                )
    return repo


def kinds(findings):
    return {f.kind for f in findings}


def test_a_healthy_fleet_reports_nothing(tmp_path):
    make_repo(tmp_path, "wss-alpha")
    make_repo(tmp_path, "wss-beta", cadence="monthly", capture_cron="10 22 3 * *",
              derive_cron="20 0 4 * *")
    assert fleet.scan(fleet.find_repos(tmp_path)) == []


def test_a_capture_that_selects_nothing_is_dead_not_a_warning(tmp_path):
    # the real bug: renamed to monthly, CADENCE left at the template's daily
    make_repo(tmp_path, "wss-gho", cadence="monthly", workflow_cadence="daily",
              capture_cron="30 2 5 * *", derive_cron="40 4 5 * *")
    findings = fleet.scan(fleet.find_repos(tmp_path))
    dead = [f for f in findings if f.kind == "captures_nothing"]
    assert len(dead) == 1
    assert dead[0].severity == "dead"
    assert "selects 0 of 2" in dead[0].detail
    assert "monthly" in dead[0].decision          # names the cadence to switch to
    assert "cadence_mismatch" in kinds(findings)  # and the filename disagreement


def test_sources_with_no_health_row_are_blind(tmp_path):
    make_repo(tmp_path, "wss-gho", sources=3, health=False)
    blind = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "untracked_sources"]
    assert len(blind) == 1
    assert blind[0].severity == "blind"
    assert "3 of 3" in blind[0].detail
    assert "fixture.demo.s0" in blind[0].entity   # names them, does not just count


def test_derive_running_faster_than_capture_is_drift(tmp_path):
    make_repo(tmp_path, "wss-gho", cadence="monthly", capture_cron="30 2 5 * *",
              derive_cron="20 0 * * *")  # daily derive, monthly capture
    findings = fleet.scan(fleet.find_repos(tmp_path))
    too_often = [f for f in findings if f.kind == "runs_too_often"]
    assert len(too_often) == 1
    assert "~24h" in too_often[0].detail and "~720h" in too_often[0].detail


def test_pin_skew_names_the_lagging_repo_and_the_target(tmp_path):
    make_repo(tmp_path, "wss-alpha", pin="v0.6.4")
    make_repo(tmp_path, "wss-beta", pin="v0.6.2")
    skew = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"]
    assert len(skew) == 1
    assert skew[0].repo == "wss-beta"
    assert "v0.6.4" in skew[0].decision


def test_auto_disabled_and_stalled_sources_each_ask_for_a_decision(tmp_path):
    repo = make_repo(tmp_path, "wss-alpha")
    path = repo / "health" / "health.csv"
    rows = list(csv.DictReader(path.open()))
    rows[0]["status"] = "auto_disabled"
    rows[0]["consecutive_failures"] = "5"
    rows[1]["staleness_h"] = "999"          # promised every 168h
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    found = {f.kind: f for f in fleet.scan(fleet.find_repos(tmp_path))}
    assert found["auto_disabled"].severity == "rot"
    assert found["stalled"].severity == "rot"
    assert found["stalled"].decision           # every finding states an action
    assert all(f.decision for f in fleet.scan(fleet.find_repos(tmp_path)))


def test_fail_on_gates_the_exit_code(tmp_path):
    make_repo(tmp_path, "wss-gho", cadence="monthly", workflow_cadence="daily",
              capture_cron="30 2 5 * *", derive_cron="40 4 5 * *")
    _, clean = fleet.run_scan(tmp_path)
    assert clean == 0                                  # reporting alone never fails
    _, code = fleet.run_scan(tmp_path, fail_on="dead")
    assert code == 1
    # a fleet whose worst finding is drift must not trip a --fail-on dead gate
    other = tmp_path / "only-drift"
    other.mkdir()
    make_repo(other, "wss-alpha", pin="v0.6.4")
    make_repo(other, "wss-beta", pin="v0.6.2")
    _, code = fleet.run_scan(other, fail_on="dead")
    assert code == 0


def test_an_invalid_registry_is_dead_and_says_so(tmp_path):
    repo = make_repo(tmp_path, "wss-alpha")
    (repo / "registry" / "fixture.demo.s0.yml").write_text("source_id: fixture.demo.s0\ncadence: daily\n")
    findings = fleet.scan(fleet.find_repos(tmp_path))
    assert findings[0].kind == "registry_invalid"
    assert findings[0].severity == "dead"
