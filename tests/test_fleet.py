from __future__ import annotations

import pytest
import csv
import shutil
import json
import textwrap
from pathlib import Path

import wss
from wss import fleet

from .conftest import write_source_yaml


CURRENT_PIN = f"v{wss.__version__}"


def make_repo(
    root: Path,
    name: str,
    *,
    cadence: str = "weekly",
    workflow_cadence: str | None = None,
    capture_cron: str = "10 22 * * 1",
    derive_cron: str = "20 0 * * 2",
    pin: str | None = None,
    sources: int = 2,
    health: bool = True,
    swallow_plan: bool = False,
) -> Path:
    # The default tracks the engine, or every version bump breaks six tests
    # that have nothing to do with pinning.
    pin = pin or CURRENT_PIN
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

    # A real repo has a derived/ directory; without one the scan correctly
    # reports that it could not check partition size.
    part = repo / "derived" / "observations"
    part.mkdir(parents=True, exist_ok=True)
    (part / "2026-09.csv").write_text("series_id,entity_id\n")

    if health:
        (repo / "health").mkdir()
        with (repo / "health" / "health.csv").open("w", newline="") as fh:
            w = csv.DictWriter(
                fh,
                fieldnames=[
                    "source_id", "first_success_at", "last_success_at", "last_attempt_at",
                    "consecutive_failures", "consecutive_throttled",
                    "expected_interval_h", "staleness_h",
                    "gate_fail_rate_28d", "status",
                ],
            )
            w.writeheader()
            for sid in ids:
                w.writerow(
                    {
                        "source_id": sid, "first_success_at": "", "last_success_at": "",
                        "last_attempt_at": "", "consecutive_failures": "0",
                        "consecutive_throttled": "0",
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
    make_repo(tmp_path, "wss-alpha")               # on the engine's own version
    make_repo(tmp_path, "wss-beta", pin="v0.6.2")
    skew = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"]
    assert len(skew) == 1
    assert skew[0].repo == "wss-beta"
    assert CURRENT_PIN in skew[0].decision


def test_a_fleet_that_agrees_but_lags_the_engine_is_still_skew(tmp_path):
    """The whole fleet sat on v0.6.19 while the engine shipped gzip in v0.6.34.

    Every repo held .csv.gz partitions its own CI could not read, and the sift
    said nothing, because comparing repos only against each other makes
    unanimity look like health.
    """
    for i in range(3):
        make_repo(tmp_path, f"wss-r{i}", pin="v0.6.19")
    skew = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"]
    assert len(skew) == 1                          # collapsed into one row
    assert all(f"wss-r{i}" in skew[0].detail for i in range(3))
    assert CURRENT_PIN in skew[0].decision


def test_a_repo_ahead_of_an_old_sift_is_not_flagged(tmp_path):
    """max() keeps a stale checkout of the engine from filing noise."""
    make_repo(tmp_path, "wss-alpha", pin="v99.0.0")
    make_repo(tmp_path, "wss-beta", pin="v99.0.0")
    assert "pin_skew" not in kinds(fleet.scan(fleet.find_repos(tmp_path)))


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
    make_repo(other, "wss-alpha")
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
    make_repo(tmp_path, "wss-ahead")               # on the engine's own version
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
    repo = make_repo(tmp_path, "wss-alpha")
    shutil.rmtree(repo / "derived")                        # as the sift sees it
    hit = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "partition_unchecked"]
    assert len(hit) == 1
    assert "not checked" in hit[0].detail

    part = tmp_path / "wss-alpha" / "derived" / "observations"
    part.mkdir(parents=True, exist_ok=True)
    (part / "2026-09.csv").write_text("x" * 1024)
    assert [f for f in fleet.scan(fleet.find_repos(tmp_path))
            if f.kind == "partition_unchecked"] == []


def _ledger(tmp_path, *entries):
    path = tmp_path / "incidents.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def test_a_recurrence_comes_back_louder_not_quieter(tmp_path):
    """A second occurrence means the previous fix was wrong, which is more
    serious than the first, not less. Severity escalates one step."""
    make_repo(tmp_path, "wss-alpha", pin="v0.6.9")
    make_repo(tmp_path, "wss-beta", pin="v0.6.8")
    plain = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"]
    assert plain and plain[0].severity == "drift"

    led = _ledger(tmp_path, {"closed": "2026-09-01", "repo": "wss-beta",
                             "kind": "pin_skew", "entity": plain[0].entity,
                             "shallow": "re-pinned by hand",
                             "systemic": "added a bump workflow"})
    out, _ = fleet.run_scan(tmp_path, ledger=led)
    assert "OCCURRENCE 2" in out
    assert "did not hold" in out
    assert "added a bump workflow" in out          # names what was tried before
    escalated = [f for f in fleet.apply_incidents(
        fleet.scan(fleet.find_repos(tmp_path)), fleet.load_incidents(led))
        if f.kind == "pin_skew"]
    assert escalated[0].severity == "rot"          # drift -> rot, per ESCALATION


def test_an_incident_closed_without_a_cause_reports_forever(tmp_path):
    """The anti-monkey-patch mechanism: an admitted patch cannot be silenced by
    fixing the symptom again, only by recording what stops it recurring."""
    make_repo(tmp_path, "wss-alpha")               # a fleet with nothing wrong
    assert fleet.scan(fleet.find_repos(tmp_path)) == []

    led = _ledger(tmp_path, {"closed": "2026-09-08", "repo": "wss-alpha",
                             "kind": "captures_nothing", "entity": "capture-weekly.yml",
                             "shallow": "set CADENCE to weekly", "systemic": None})
    out, _ = fleet.run_scan(tmp_path, ledger=led)
    assert "patched_not_fixed" in out or "no recorded cause" in out
    assert "set CADENCE to weekly" in out          # the patch is named

    # recording the cause is the only thing that clears it
    led.write_text(led.read_text().replace('"systemic": null',
                                           '"systemic": "template no longer defaults to daily"'))
    out, _ = fleet.run_scan(tmp_path, ledger=led)
    assert "no recorded cause" not in out


def test_a_malformed_ledger_line_is_reported_not_skipped(tmp_path):
    """An unreadable ledger silently stops catching recurrences."""
    make_repo(tmp_path, "wss-alpha")
    path = tmp_path / "incidents.jsonl"
    path.write_text('{"closed":"2026-09-08","kind":"x","entity":"y","systemic":"z"}\n{ broken\n')
    out, _ = fleet.run_scan(tmp_path, ledger=path)
    assert "ledger_malformed" in out and "line 2" in out


def test_a_live_recurrence_is_not_also_reported_as_patch_debt(tmp_path):
    """Patch debt is about a QUIET incident. If the symptom is back it is already
    reported as a recurrence, which is louder; saying both doubles the row."""
    make_repo(tmp_path, "wss-alpha", pin="v0.6.9")
    make_repo(tmp_path, "wss-beta", pin="v0.6.8")
    skew = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"][0]
    led = _ledger(tmp_path, {"closed": "2026-09-01", "repo": "wss-beta",
                             "kind": "pin_skew", "entity": skew.entity,
                             "shallow": "re-pinned by hand", "systemic": None})
    got = fleet.apply_incidents(fleet.scan(fleet.find_repos(tmp_path)),
                                fleet.load_incidents(led))
    kinds = [f.kind for f in got]
    assert kinds.count("pin_skew") == 1          # the recurrence
    assert "patched_not_fixed" not in kinds      # not also the debt


def test_escalation_never_changes_the_category(tmp_path):
    """rot -> dead, not rot -> blind. `blind` means unwatched, not "worse"."""
    assert fleet.ESCALATION["rot"] == "dead"
    assert fleet.ESCALATION["blind"] == "dead"
    assert fleet.ESCALATION["drift"] == "rot"
    assert fleet.ESCALATION["dead"] == "dead"


def test_an_accepted_condition_is_annotated_never_escalated(tmp_path):
    """Some findings are supposed to be there. An accessdata source always carries
    a small failure count from CI attempts it cannot win. Escalating that to DEAD
    every week is the alert fatigue the ledger exists to prevent."""
    make_repo(tmp_path, "wss-alpha", pin="v0.6.9")
    make_repo(tmp_path, "wss-beta", pin="v0.6.8")
    skew = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "pin_skew"][0]
    led = _ledger(tmp_path, {"closed": "2026-09-08", "repo": "wss-beta",
                             "kind": "pin_skew", "entity": skew.entity,
                             "shallow": "-", "systemic": "this repo is pinned on purpose",
                             "accepted": True})
    got = [f for f in fleet.apply_incidents(fleet.scan(fleet.find_repos(tmp_path)),
                                            fleet.load_incidents(led))
           if f.kind == "pin_skew"]
    assert len(got) == 1
    assert got[0].severity == "drift"            # unchanged, not escalated
    assert got[0].detail.startswith("ACCEPTED.")
    assert "pinned on purpose" in got[0].decision


def test_a_missing_ledger_is_an_error_not_an_empty_history(tmp_path):
    """Returning [] for a missing file made "no memory" look like "nothing has
    ever gone wrong". The sift ran a whole cycle that way."""
    make_repo(tmp_path, "wss-alpha")
    with pytest.raises(FileNotFoundError, match="ledger not found"):
        fleet.load_incidents(tmp_path / "nope.jsonl")


def test_disabled_workflow_is_a_finding():
    """A workflow GitHub switched off is the quietest failure in the fleet.

    Nothing fails, nothing turns red, no run appears. Every other check in
    fleet.py keeps passing, because they all read files still sitting there
    from the last successful run.
    """
    states = {"wss-demo": {"capture-monthly": "disabled_inactivity",
                           "health": "disabled_manually",
                           "derive": "active"}}
    found = {f.entity: f for f in fleet.scan_workflow_states(states)}
    assert set(found) == {"capture-monthly", "health"}      # active one is silent
    # A capture that cannot fire is losing data now; the rest mean nobody is
    # watching, which is bad later rather than immediately.
    assert found["capture-monthly"].severity == "dead"
    assert found["health"].severity == "blind"
    assert "60 days" in found["capture-monthly"].detail


def test_scan_without_states_reports_nothing_about_workflows(tmp_path):
    assert fleet.scan_workflow_states({}) == []


def test_run_scan_refuses_a_missing_states_file(tmp_path):
    """Silently checking nothing is how a clean report gets trusted."""
    with pytest.raises(FileNotFoundError, match="reports a clean fleet"):
        fleet.run_scan(tmp_path, workflow_states=tmp_path / "absent.json")


def test_a_recovered_source_left_disabled_is_dead_not_rot(tmp_path):
    """Auto-disable is one-way: nothing ever turns a source back on.

    fda.recalls.cder sat auto_disabled with consecutive_failures down to 1 and
    a success two days before. Healthy, and capturing nothing, and the sweep
    reported it as ordinary rot alongside genuinely broken sources.
    """
    repo = tmp_path / "wss-demo"
    repo.mkdir()
    # An empty registry short-circuits the whole scan with registry_invalid,
    # so the repo needs real entries before any health check is reached.
    write_source_yaml(repo, "demo.api.recovered", "https://example.com/a")
    write_source_yaml(repo, "demo.api.broken", "https://example.com/b")
    (repo / "health").mkdir(parents=True)
    (repo / "health" / "health.csv").write_text(
        "source_id,status,consecutive_failures,last_success_at,expected_interval_h,staleness_h\n"
        "demo.api.recovered,auto_disabled,1,2026-09-07T00:00:00Z,168,20\n"
        "demo.api.broken,auto_disabled,11,2026-01-01T00:00:00Z,168,9000\n")
    found = {f.entity: f for f in fleet.scan_repo(repo)}
    assert found["demo.api.recovered"].kind == "disabled_but_healthy"
    assert found["demo.api.recovered"].severity == "dead"   # losing data right now
    assert found["demo.api.broken"].kind == "auto_disabled"
    assert found["demo.api.broken"].severity == "rot"


def test_a_throttled_source_is_loud_even_though_it_will_never_disable(tmp_path):
    """The exemption in health.py is what makes this finding necessary.

    A run of 429s is deliberately kept out of consecutive_failures, so nothing
    would ever say it out loud unless the scan did -- and it will sit there
    forever, because a source that never reaches the threshold never flips.
    """
    repo = make_repo(tmp_path, "wss-alpha")
    path = repo / "health" / "health.csv"
    rows = list(csv.DictReader(path.open()))
    rows[0]["consecutive_throttled"] = "6"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    found = {f.kind: f for f in fleet.scan(fleet.find_repos(tmp_path))}
    assert found["throttled"].severity == "rot"
    assert "429" in found["throttled"].detail
    assert "delay_seconds" in found["throttled"].decision


def test_one_throttled_evening_is_not_a_finding(tmp_path):
    repo = make_repo(tmp_path, "wss-alpha")
    path = repo / "health" / "health.csv"
    rows = list(csv.DictReader(path.open()))
    rows[0]["consecutive_throttled"] = "2"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert "throttled" not in kinds(fleet.scan(fleet.find_repos(tmp_path)))


def test_a_health_table_from_an_older_engine_still_scans(tmp_path):
    """consecutive_throttled is new; repos in the fleet lag the engine."""
    repo = make_repo(tmp_path, "wss-alpha")
    path = repo / "health" / "health.csv"
    rows = list(csv.DictReader(path.open()))
    fields = [f for f in rows[0] if f != "consecutive_throttled"]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    assert "throttled" not in kinds(fleet.scan(fleet.find_repos(tmp_path)))


# --- a workflow that runs and fails every time -----------------------------
# Invisible until v0.6.42: `state` stays "active", so the disabled-check
# skipped it, and every file-based check kept passing on outputs from the last
# run that worked. wss-drug-scarcity's monthly shard sat red for three days
# while three new sources were added to it.

def test_a_workflow_failing_every_run_is_dead_not_silent(tmp_path):
    make_repo(tmp_path, "wss-alpha")
    states = {"wss-alpha": {"capture-weekly.yml": {
        "state": "active", "recent": ["failure", "failure", "success"]}}}
    found = {f.kind: f for f in fleet.scan(fleet.find_repos(tmp_path), states)}
    assert "workflow_failing" in found
    f = found["workflow_failing"]
    assert f.severity == "dead"            # a capture that cannot fire loses data now
    assert "2 run(s) failed" in f.detail
    assert "still 'active'" in f.detail    # the reason it was invisible
    assert "FAILING JOB" in f.decision


def test_one_failure_is_a_flake(tmp_path):
    make_repo(tmp_path, "wss-alpha")
    states = {"wss-alpha": {"capture-weekly.yml": {
        "state": "active", "recent": ["failure", "success", "success"]}}}
    assert "workflow_failing" not in kinds(fleet.scan(fleet.find_repos(tmp_path), states))


def test_a_failing_non_capture_workflow_is_blind_not_dead(tmp_path):
    """Nobody is watching is bad later; nothing is collecting is bad now."""
    make_repo(tmp_path, "wss-alpha")
    states = {"wss-alpha": {"health.yml": {
        "state": "active", "recent": ["failure", "failure"]}}}
    found = {f.kind: f for f in fleet.scan(fleet.find_repos(tmp_path), states)}
    assert found["workflow_failing"].severity == "blind"


def test_a_run_still_in_progress_does_not_break_the_streak(tmp_path):
    """`null` conclusions are dropped, not counted as a pass.

    A queued run sitting in front of two real failures would otherwise reset
    the streak to zero and hide exactly the case this check exists for.
    """
    make_repo(tmp_path, "wss-alpha")
    states = {"wss-alpha": {"capture-weekly.yml": {
        "state": "active", "recent": [None, "failure", "failure"]}}}
    assert "workflow_failing" in kinds(fleet.scan(fleet.find_repos(tmp_path), states))


def test_the_plain_string_form_still_works(tmp_path):
    """An older collector sends {name: state}. It must not crash."""
    make_repo(tmp_path, "wss-alpha")
    states = {"wss-alpha": {"capture-weekly.yml": "disabled_inactivity"}}
    found = kinds(fleet.scan(fleet.find_repos(tmp_path), states))
    assert "workflow_disabled" in found and "workflow_failing" not in found


def test_failing_AND_disabled_reports_both(tmp_path):
    """They are different problems with different fixes."""
    make_repo(tmp_path, "wss-alpha")
    states = {"wss-alpha": {"capture-weekly.yml": {
        "state": "disabled_inactivity", "recent": ["failure", "failure"]}}}
    found = kinds(fleet.scan(fleet.find_repos(tmp_path), states))
    assert {"workflow_failing", "workflow_disabled"} <= found


def _set_health(repo, **cols):
    """Rewrite the fixture's health.csv with extra/overridden columns."""
    import csv as _csv
    path = repo / "health" / "health.csv"
    rows = list(_csv.DictReader(path.open()))
    fields = list(rows[0].keys()) + [k for k in cols if k not in rows[0]]
    for r in rows:
        r.update({k: str(v) for k, v in cols.items()})
    with path.open("w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_gate_rotting_needs_a_denominator(tmp_path):
    """A 50% quarantine rate over two fetches is an incident, not a trend.

    ansm.shortages.fr was reported ROT at "50% of fetches quarantined over 28d"
    on ONE failure out of two fetches three minutes apart, at the same byte
    length, on a page that was fine. The loudest severity in the scan crying
    wolf is worse than no scan: it teaches the reader to skim the section.
    """
    repo = make_repo(tmp_path, "wss-alpha")
    _set_health(repo, gate_fail_rate_28d="0.5", attempts_28d="2")
    assert "gate_rotting" not in kinds(fleet.scan(fleet.find_repos(tmp_path)))

    # Same rate, enough fetches to mean something.
    _set_health(repo, gate_fail_rate_28d="0.5",
                attempts_28d=str(fleet.GATE_ROT_MIN_ATTEMPTS))
    rot = [f for f in fleet.scan(fleet.find_repos(tmp_path)) if f.kind == "gate_rotting"]
    assert rot, "a real rate over enough fetches must still report"
    # ...and it names the denominator, so the reader never has to ask.
    assert all(f"of {fleet.GATE_ROT_MIN_ATTEMPTS} fetches" in f.detail for f in rot)

    # A health table written before attempts_28d existed still reports, rather
    # than silently going quiet on every older repo in the fleet.
    _set_health(repo, gate_fail_rate_28d="0.9", attempts_28d="")
    assert "gate_rotting" in kinds(fleet.scan(fleet.find_repos(tmp_path)))
