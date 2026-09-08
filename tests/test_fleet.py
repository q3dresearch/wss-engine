from __future__ import annotations

import csv
import json
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
    swallow_plan: bool = False,
) -> Path:
    repo = root / name
    (repo / ".github" / "workflows").mkdir(parents=True)
    ids = []
    for i in range(sources):
        sid = f"fixture.demo.s{i}"
        ids.append(sid)
        write_source_yaml(repo, sid, f"https://example.com/{i}", cadence=cadence)

    plan_step = (
        'echo "shards=$(wss plan --cadence $CADENCE)" >> "$GITHUB_OUTPUT"'
        if swallow_plan
        else 'shards=$(wss plan --cadence $CADENCE)'
    )
    (repo / ".github" / "workflows" / f"capture-{cadence}.yml").write_text(
        textwrap.dedent(
            f"""\
            name: capture-{cadence}
            on:
              schedule:
                - cron: "{capture_cron}"
            timeout-minutes: 30
            env:
              CADENCE: {workflow_cadence or cadence}
              ENGINE_SPEC: "wss @ git+https://github.com/o/wss-engine.git@{pin}"
            jobs:
              plan:
                steps:
                  - run: {plan_step}
            """
        )
    )
    (repo / ".github" / "workflows" / "derive.yml").write_text(
        f'name: derive\ntimeout-minutes: 30\non:\n  schedule:\n    - cron: "{derive_cron}"\n'
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


def test_a_plan_step_that_swallows_its_exit_code_is_drift(tmp_path):
    """`echo "shards=$(wss plan ...)"` returns echo's status, so every guard
    inside plan is discarded and an empty matrix still reads as a green run."""
    make_repo(tmp_path, "wss-alpha", swallow_plan=True)
    findings = fleet.scan(fleet.find_repos(tmp_path))
    swallowed = [f for f in findings if f.kind == "plan_exit_swallowed"]
    assert len(swallowed) == 1
    assert "assign first" in swallowed[0].decision


def test_the_assigned_form_is_not_flagged(tmp_path):
    make_repo(tmp_path, "wss-alpha", swallow_plan=False)
    assert fleet.scan(fleet.find_repos(tmp_path)) == []


def test_a_comment_quoting_the_bad_pattern_is_not_a_finding(tmp_path):
    """The fix ships with a comment showing what not to do. Matching it would
    make every correctly-fixed repo report the defect it just fixed."""
    repo = make_repo(tmp_path, "wss-alpha", swallow_plan=False)
    wf = repo / ".github" / "workflows" / "capture-weekly.yml"
    wf.write_text(
        wf.read_text().replace(
            "  - run: shards=$(wss plan --cadence $CADENCE)",
            '  # never `echo "shards=$(wss plan ...)"` -- that returns echo\'s status\n'
            "  - run: shards=$(wss plan --cadence $CADENCE)",
        )
    )
    assert [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "plan_exit_swallowed"] == []


def _heartbeat(repo, **fields):
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "last_run.json").write_text(json.dumps(fields))


def test_a_committed_heartbeat_that_planned_nothing_is_dead(tmp_path):
    """Config parsing says what a capture *would* select; the heartbeat is the
    run's own account of what it actually did, in git, after the fact."""
    repo = make_repo(tmp_path, "wss-gho")
    _heartbeat(repo, cadence="weekly", sources_planned=0, completed_at="2026-09-07T22:10:00Z")
    dead = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "last_run_planned_nothing"]
    assert len(dead) == 1 and dead[0].severity == "dead"
    assert "sources_planned=0" in dead[0].detail


def test_a_healthy_heartbeat_is_not_a_finding(tmp_path):
    repo = make_repo(tmp_path, "wss-gho")
    _heartbeat(repo, cadence="weekly", sources_planned=2, completed_at="2026-09-07T22:10:00Z")
    assert fleet.scan(fleet.find_repos(tmp_path)) == []


def test_an_empty_field_from_a_missing_job_output_is_caught(tmp_path):
    """`"sources_planned":%s` with an unset needs.plan.outputs.count renders as
    `"sources_planned":,` -- invalid JSON, and silently so."""
    repo = make_repo(tmp_path, "wss-gho")
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "last_run.json").write_text('{"cadence":"weekly","sources_planned":,"x":1}')
    kinds_found = {f.kind for f in fleet.scan(fleet.find_repos(tmp_path))}
    assert "heartbeat_unreadable" in kinds_found


def test_a_defect_every_repo_inherited_is_one_finding_not_eight(tmp_path):
    """Thirty-two rows saying "bump the action" is the content-mill failure the
    sift rules forbid -- the reader stops before reaching the real finding."""
    for i in range(4):
        repo = make_repo(tmp_path, f"wss-r{i}")
        wf = repo / ".github" / "workflows" / "capture-weekly.yml"
        wf.write_text(wf.read_text().replace("timeout-minutes: 30\n", ""))
        wf.write_text(wf.read_text() + "    steps:\n      - uses: actions/checkout@v4\n")
    findings = fleet.scan(fleet.find_repos(tmp_path))
    for kind in ("no_timeout", "node20_action"):
        rows = [f for f in findings if f.kind == kind]
        assert len(rows) == 1, f"{kind} produced {len(rows)} rows, expected 1"
        assert "4 repos" == rows[0].repo
        assert "wss-r0" in rows[0].detail and "wss-r3" in rows[0].detail
        assert "fix the template first" in rows[0].decision


def test_pin_skew_collapses_too_but_still_names_every_repo(tmp_path):
    make_repo(tmp_path, "wss-ahead", pin="v0.6.9")
    for i in range(3):
        make_repo(tmp_path, f"wss-behind{i}", pin="v0.6.8")
    skew = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"]
    assert len(skew) == 1
    assert all(f"wss-behind{i}" in skew[0].detail for i in range(3))
    assert "wss-ahead" not in skew[0].detail   # it is the target, not a finding


def test_a_partition_running_out_of_room_is_reported(tmp_path):
    """GitHub refuses a file over 100 MB. A derived partition is append-only
    within its month, so the moment to speak is while there is room to act."""
    repo = make_repo(tmp_path, "wss-alpha")
    part = repo / "derived" / "observations"
    part.mkdir(parents=True)
    (part / "2026-09.csv").write_text("x" * int(46 * 1048576))
    hit = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "partition_near_limit"]
    assert len(hit) == 1 and hit[0].severity == "rot"
    assert "46 MB" in hit[0].detail

    (part / "2026-09.csv").write_text("x" * int(101 * 1048576))
    hit = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "partition_near_limit"]
    assert hit[0].severity == "dead"      # a push will now be refused outright


def test_a_small_partition_is_not_a_finding(tmp_path):
    repo = make_repo(tmp_path, "wss-alpha")
    part = repo / "derived" / "observations"
    part.mkdir(parents=True)
    (part / "2026-09.csv").write_text("x" * 1024)
    assert [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "partition_near_limit"] == []


def test_a_pause_with_a_recorded_reason_is_not_drift(tmp_path):
    """A pause is drift only when nobody wrote down why. Flagging a documented
    decision every week trains the reader to skip the section it lives in."""
    repo = make_repo(tmp_path, "wss-alpha", sources=2)
    entry = repo / "registry" / "fixture.demo.s0.yml"
    entry.write_text(entry.read_text().replace("status: active", "status: paused"))
    flagged = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "paused"]
    assert len(flagged) == 1                       # undocumented -> reported

    entry.write_text("# PAUSED ON PURPOSE -- the publisher archives it.\n" + entry.read_text())
    flagged = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "paused"]
    assert flagged == []                           # documented -> a decision, not drift


def test_a_missing_derived_dir_is_reported_not_silently_passed(tmp_path):
    """The weekly sift sparse-checks out registry/, health/ and the workflows,
    so derived/ is absent and the partition check cannot run. Passing silently
    is how a check stops being a check."""
    make_repo(tmp_path, "wss-alpha")                       # no derived/ at all
    hit = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "partition_unchecked"]
    assert len(hit) == 1
    assert "not checked" in hit[0].detail

    part = tmp_path / "wss-alpha" / "derived" / "observations"
    part.mkdir(parents=True)
    (part / "2026-09.csv").write_text("x" * 1024)
    assert [f for f in fleet.scan(fleet.find_repos(tmp_path))
            if f.kind == "partition_unchecked"] == []
