"""Cross-repo fleet inspection -- the sift job.

`wss health` answers "is this source stale?" inside one repo. Nothing answered
"is this repo still capturing at all?", which is how two repos ran green for
days while selecting zero sources: the capture workflow was renamed to weekly
but its CADENCE env stayed `daily`, no registry entry declared daily, and an
empty shard list is a successful run.

So this scans every repo at once and emits decisions, not counts. Every finding
names the thing it is about and says what a human has to do about it; a bare
tally just sends the reader back to look up the names.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml

from . import registry

# dead:  capturing nothing right now -- data is being lost
# blind: running, but nothing is watching it
# rot:   a specific source is failing and needs a retire/repoint call
# drift: config disagrees with itself; not yet losing data
SEVERITY = ("dead", "blind", "rot", "drift")

STALE_FACTOR = 2.0        # staleness beyond 2x the promised interval is a stall
# Majors still shipping a Node 20 entrypoint. GitHub force-runs them on Node 24
# and warns on every job; the warning becomes an error on its own schedule, and
# a fleet only finds out when every repo breaks at once.
NODE20_ACTIONS = {
    "actions/checkout": 4, "actions/setup-python": 5,
    "actions/upload-artifact": 4, "actions/download-artifact": 4,
}
# The graveyard marker: a paused entry that says why is a decision, not drift.
PAUSE_DOCUMENTED = re.compile(r"PAUSED ON PURPOSE|RETIRED ON PURPOSE", re.I)
GATE_ROT_RATE = 0.50      # half the fetches quarantined = the page has moved on
# GitHub refuses a file over 100 MB outright and warns from 50. A derived
# partition is append-only within its month, so the moment to say something is
# while there is still room to act.
PARTITION_WARN_MB = 45.0
PARTITION_HARD_MB = 100.0


@dataclass
class Finding:
    severity: str
    kind: str
    repo: str
    entity: str      # the source_id / workflow / repo this is *about*
    detail: str      # what was measured
    decision: str    # what to do about it


def _cron_period_hours(expr: str) -> float:
    """Roughly how often a 5-field cron fires. Coarse on purpose: the only
    question asked of it is whether one workflow runs more often than another."""
    parts = expr.split()
    if len(parts) != 5:
        return float("nan")
    _minute, hour, dom, _month, dow = parts
    if dom != "*":
        return 720.0
    if dow != "*":
        return 168.0
    if hour != "*":
        return 24.0
    return 1.0


def _workflows(repo: Path) -> dict[str, dict]:
    """Map workflow filename -> {cron, cadence} without a YAML round-trip.

    `on:` parses as the boolean True in YAML 1.1, and these files carry
    ${{ }} expressions, so the fields worth reading are read by regex.
    """
    out: dict[str, dict] = {}
    wf_dir = repo / ".github" / "workflows"
    for path in sorted(wf_dir.glob("*.yml")) if wf_dir.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Comments explaining a bad pattern quote it verbatim, so match against
        # the code only -- otherwise documenting the fix re-triggers the finding.
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        cron = re.search(r"""cron:\s*['"]([^'"]+)['"]""", text)
        cadence = re.search(r"^\s*CADENCE:\s*(\S+)", text, re.M)
        pin = re.search(r"wss-engine\.git@(v[\d.]+)", text)
        out[path.name] = {
            "cron": cron.group(1) if cron else None,
            "cadence": cadence.group(1) if cadence else None,
            "pin": pin.group(1) if pin else None,
            # `echo "shards=$(wss plan ...)"` reports echo's exit status, so a
            # failing plan looks like a passing step. Any guard inside plan is
            # discarded by it.
            "uses": re.findall(r"uses:\s*([\w./-]+)@v(\d+)", code),
            "has_timeout": bool(re.search(r"^\s*timeout-minutes:", code, re.M)),
            "swallows_plan": bool(re.search(r'echo\s+"[^"]*\$\(\s*wss\s+plan', code)),
        }
    return out


def _health_rows(repo: Path) -> list[dict]:
    path = repo / "health" / "health.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _pin(repo: Path) -> str | None:
    req = repo / "requirements.txt"
    if not req.is_file():
        return None
    m = re.search(r"wss-engine\.git@(v[\d.]+)", req.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def scan_repo(repo: Path) -> list[Finding]:
    name = repo.name
    found: list[Finding] = []

    try:
        sources = registry.load_registry(repo)
    except registry.RegistryError as exc:
        return [Finding("dead", "registry_invalid", name, name,
                        str(exc).splitlines()[0],
                        "fix the registry -- every workflow in this repo is failing at the gate")]

    active = [s for s in sources if s.status == "active"]
    registry_dir = repo / "registry"
    workflows = _workflows(repo)

    # --- is anything actually being captured? -------------------------------
    captures = {f: w for f, w in workflows.items() if f.startswith("capture-")}
    if active and not captures:
        found.append(Finding("dead", "no_capture_workflow", name, name,
                             f"{len(active)} active source(s), no capture workflow",
                             "add a capture workflow or mark the sources paused"))
    for fname, wf in captures.items():
        cadence = wf["cadence"]
        selected = [s for s in active if s.cadence == cadence] if cadence else []
        if cadence and not selected:
            have = sorted({s.cadence for s in active})
            found.append(Finding("dead", "captures_nothing", name, fname,
                                 f"CADENCE={cadence} selects 0 of {len(active)} active source(s); "
                                 f"the registry declares {', '.join(have) or 'nothing'}",
                                 f"set CADENCE to one the registry uses ({', '.join(have) or 'n/a'}) "
                                 "-- this workflow is green and capturing nothing"))
        if wf.get("swallows_plan"):
            found.append(Finding("drift", "plan_exit_swallowed", name, fname,
                                 'the plan step wraps `wss plan` in echo "...=$(...)", '
                                 "so its exit status is echo's and a failed plan reads as success",
                                 "assign first (`shards=$(wss plan ...)`) -- otherwise every guard "
                                 "inside plan is discarded and an empty matrix stays green"))
        stem = fname[len("capture-"):].removesuffix(".yml")
        if cadence and stem != cadence:
            found.append(Finding("drift", "cadence_mismatch", name, fname,
                                 f"filename says {stem}, CADENCE says {cadence}",
                                 "rename the file or fix CADENCE so the schedule is readable"))

    # --- does anything run more often than the data changes? ----------------
    cap_periods = [_cron_period_hours(w["cron"]) for w in captures.values() if w["cron"]]
    fastest = min(cap_periods) if cap_periods else None
    for fname in ("derive.yml", "health.yml"):
        wf = workflows.get(fname)
        if not wf or not wf["cron"] or fastest is None:
            continue
        period = _cron_period_hours(wf["cron"])
        if period < fastest:
            found.append(Finding("drift", "runs_too_often", name, fname,
                                 f"fires every ~{period:.0f}h against a capture that changes every ~{fastest:.0f}h",
                                 f"slow {fname} to the capture cadence -- the extra runs cannot find new data"))

    # --- will the runner still accept this next month? ---
    stale = {}
    no_timeout = []
    for fname, wf in workflows.items():
        if not wf.get("has_timeout"):
            no_timeout.append(fname)
        for action, major in wf.get("uses", []):
            floor = NODE20_ACTIONS.get(action)
            if floor is not None and int(major) <= floor:
                stale.setdefault(f"{action}@v{major}", []).append(fname)
    for pinned, files in sorted(stale.items()):
        found.append(Finding("drift", "node20_action", name, pinned,
                             f"used by {len(files)} workflow(s): {', '.join(sorted(files))}",
                             "bump the major -- GitHub force-runs Node 20 actions on Node 24 "
                             "today and warns; when it stops, every repo breaks at once"))
    if no_timeout:
        found.append(Finding("drift", "no_timeout", name,
                             ", ".join(sorted(no_timeout)),
                             f"{len(no_timeout)} workflow(s) set no timeout-minutes",
                             "a hung fetch burns the 6-hour default before anyone notices -- "
                             "set a ceiling that matches how long the job should take"))

    # --- what does the last committed run say it did? ---
    # Config parsing infers whether a capture *would* select anything; this is
    # the run's own account of what it actually planned, in git, after the fact.
    beat = repo / "state" / "last_run.json"
    if beat.is_file():
        try:
            planned = json.loads(beat.read_text()).get("sources_planned")
        except json.JSONDecodeError:
            planned = None
            found.append(Finding("drift", "heartbeat_unreadable", name, "state/last_run.json",
                                 "committed heartbeat is not valid JSON",
                                 "check the printf in the commit job -- a missing job output "
                                 "renders as an empty field"))
        if planned is not None and str(planned).strip() in ("0", ""):
            found.append(Finding("dead", "last_run_planned_nothing", name, "state/last_run.json",
                                 f"the last committed run recorded sources_planned={planned!r}",
                                 "this repo's most recent capture selected no sources -- fix "
                                 "CADENCE or the registry before the next scheduled run"))

    # --- is any partition running out of room? ---
    part_dir = repo / "derived" / "observations"
    if not part_dir.is_dir():
        # Not "no problem" -- "not looked at". The weekly sift sparse-checks out
        # registry/, health/ and the workflows only, because a full checkout
        # would pull hundreds of MB of derived CSVs. So this check runs locally
        # and is inert in CI, and saying so beats passing silently.
        found.append(Finding("drift", "partition_unchecked", name, "derived/observations",
                             "not present in this checkout, so partition size was not checked",
                             "run `wss fleet-scan` against full local checkouts, or add "
                             "derived/ to the sift's sparse-checkout and accept the download"))
    else:
        biggest = max(
            ((p.stat().st_size / 1048576, p.name) for p in part_dir.glob("*.csv")),
            default=(0.0, ""))
        size_mb, part = biggest
        if size_mb >= PARTITION_WARN_MB:
            sev = "dead" if size_mb >= PARTITION_HARD_MB else "rot"
            found.append(Finding(
                sev, "partition_near_limit", name, f"derived/observations/{part}",
                f"{size_mb:.0f} MB against GitHub's {PARTITION_HARD_MB:.0f} MB hard limit",
                "a source with no event date writes its whole snapshot at capture time, "
                "so the month's partition grows per capture -- slow the cadence, cut "
                "metrics per entity, or move the source to object storage before a "
                "push is refused"))

    # --- is anyone watching? ------------------------------------------------
    rows = _health_rows(repo)
    tracked = {r["source_id"] for r in rows}
    blind = [s.source_id for s in active if s.source_id not in tracked]
    if blind:
        shown = ", ".join(blind[:4]) + (f" +{len(blind) - 4} more" if len(blind) > 4 else "")
        found.append(Finding("blind", "untracked_sources", name, shown,
                             f"{len(blind)} of {len(active)} active source(s) have no health row",
                             "run the health workflow once -- until it does, a stall here is invisible"))

    # --- which named source needs a decision? -------------------------------
    for row in rows:
        sid = row.get("source_id", "?")
        if row.get("status") == "auto_disabled":
            found.append(Finding("rot", "auto_disabled", name, sid,
                                 f"{row.get('consecutive_failures', '?')} consecutive failures",
                                 "retire it, repoint the URL, or fix the gate -- it is off until you do"))
            continue
        expected, stale = _float(row.get("expected_interval_h")), _float(row.get("staleness_h"))
        if expected and stale and stale > expected * STALE_FACTOR:
            found.append(Finding("rot", "stalled", name, sid,
                                 f"{stale:.0f}h since last success, promised every {expected:.0f}h",
                                 "check whether the publisher moved the endpoint"))
        rate = _float(row.get("gate_fail_rate_28d"))
        if rate is not None and rate >= GATE_ROT_RATE:
            found.append(Finding("rot", "gate_rotting", name, sid,
                                 f"{rate:.0%} of fetches quarantined over 28d",
                                 "the page is drifting under its gates -- re-case it before it dies"))

    # A pause is drift only when nobody wrote down why. wss-drug-scarcity's
    # fda.nsde.marketing opens "PAUSED ON PURPOSE -- this entry documents a
    # decision, not a collection" and explains that FDA retains delisted
    # products, so capturing adds nothing. Flagging that every week trains the
    # reader to skip the whole section, which costs more than the finding.
    for source in sources:
        if source.status != "paused":
            continue
        entry = registry_dir / f"{source.source_id}.yml"
        text = entry.read_text(encoding="utf-8", errors="replace") if entry.is_file() else ""
        if PAUSE_DOCUMENTED.search(text):
            continue
        found.append(Finding("drift", "paused", name, source.source_id,
                             "paused, with no recorded reason",
                             "resume it, or write down why it is parked -- a pause nobody "
                             "explained is a decision nobody made. The convention is a "
                             "'PAUSED ON PURPOSE' block saying what was tested and what "
                             "would change the answer"))
    return found


def scan_workflow_states(states: dict) -> list[Finding]:
    """Findings from GitHub's own view of whether a workflow will ever fire.

    A scheduled workflow that GitHub has switched off is the quietest failure
    there is. Nothing fails, nothing turns red, no run appears -- the repo
    simply stops collecting, and every other check in this file keeps passing
    because they all read files that are still sitting there from the last
    successful run.

    The documented trigger is inactivity: "In a public repository, scheduled
    workflows are automatically disabled when no repository activity has
    occurred in 60 days." Sixty days is also long enough that nobody remembers
    what changed, and a wss repo that stops for sixty days has lost sixty days
    that cannot be re-fetched.

    `states` is {repo: {workflow_name: state}}, supplied by the caller because
    it needs a GitHub token and everything else here is a pure function of
    files on disk.
    """
    found: list[Finding] = []
    for repo, workflows in sorted(states.items()):
        for name, state in sorted(workflows.items()):
            if state == "active":
                continue
            # A capture that cannot fire is losing data now; the rest mean
            # nobody is watching, which is bad later rather than immediately.
            capturing = "capture" in name.lower()
            reason = ("disabled by GitHub after 60 days without repository "
                      "activity" if state == "disabled_inactivity" else
                      f"state is {state!r}")
            found.append(Finding(
                "dead" if capturing else "blind", "workflow_disabled", repo, name,
                f"workflow will not fire -- {reason}",
                "re-enable it in Actions, then work out what stopped the "
                "repository looking active. A monthly bot commit may not count."))
    return found


def scan(repos: list[Path], workflow_states: dict | None = None) -> list[Finding]:
    found: list[Finding] = []
    for repo in repos:
        found.extend(scan_repo(repo))
    if workflow_states:
        found.extend(scan_workflow_states(workflow_states))

    # Pin skew is the one thing only visible across repos.
    pins = {r.name: _pin(r) for r in repos if _pin(r)}
    if len(set(pins.values())) > 1:
        newest = max(set(pins.values()), key=lambda v: [int(x) for x in v.lstrip("v").split(".")])
        for repo_name, pin in sorted(pins.items()):
            if pin != newest:
                found.append(Finding("drift", "pin_skew", repo_name, repo_name,
                                     f"engine {pin}, fleet is on {newest}",
                                     f"re-pin to {newest} so a fix reaches every repo"))
    # A defect every repo inherited from one template is one decision, not N.
    # Thirty-two rows saying "bump the action" is the content-mill failure the
    # sift design rules forbid: the reader stops reading before the real finding.
    for kind, headline in (
        ("node20_action", "actions still on a Node 20 major"),
        ("no_timeout", "workflows with no timeout-minutes"),
        ("pin_skew", "repos behind the fleet's engine"),
        ("partition_unchecked", "repos whose derived/ was not in the checkout"),
    ):
        group = [f for f in found if f.kind == kind]
        if len(group) <= 1:
            continue
        repos = sorted({f.repo for f in group})
        entities = sorted({e for f in group for e in f.entity.split(", ")})
        found = [f for f in found if f.kind != kind]
        found.append(Finding(
            group[0].severity, kind, f"{len(repos)} repos", ", ".join(entities)[:160],
            f"{headline}: {len(group)} occurrence(s) across {', '.join(repos)}",
            group[0].decision + " -- fleet-wide, so fix the template first",
        ))

    found.sort(key=lambda f: (SEVERITY.index(f.severity), f.repo, f.kind))
    return found


# --------------------------------------------------------------- the ledger
#
# Every incident in this fleet has had a shallow fix and a systemic one, and on
# 2026-09-08 the score was eight out of eight: setting CADENCE fixed two dead
# repos, but the generator still defaulted to daily; re-basing gid windows fixed
# a source for the SECOND time, because the procedure lived in nobody's head.
#
# So the ledger is not a log. It is a claim, per incident, that the cause was
# removed -- and two checks that make the claim expensive to fake:
#
#   RECURRENCE     a finding matching a closed incident comes back LOUDER, not
#                  quieter. A second occurrence means the previous fix was wrong,
#                  which is more serious than the first, not less.
#   ACCEPTED       `"accepted": true` marks a condition that will not go away and
#                  is not meant to -- an accessdata source that always carries a
#                  small failure count from CI attempts it cannot win. It keeps
#                  its severity, gains the recorded reasoning, and never
#                  escalates. Without this the ledger turns a known state into a
#                  weekly DEAD alert, which is the alert fatigue it exists to
#                  prevent.
#   PATCH DEBT     an incident closed with `systemic: null` is an admitted monkey
#                  patch, and it is reported on every sweep forever. It cannot be
#                  silenced by fixing the symptom again -- only by recording what
#                  stops it recurring.
#
# The second is the point. Anyone can make a finding disappear; the ledger asks
# what they changed so nobody has to make it disappear twice.

INCIDENT_FIELDS = ("closed", "repo", "kind", "entity", "shallow", "systemic", "accepted")

# SEVERITY is a list of CATEGORIES, not a scale -- `blind` means "unwatched",
# not "between dead and rot". Escalating by list index turned a recurring `rot`
# into `blind`, which reads as a different problem rather than a worse one.
ESCALATION = {"drift": "rot", "rot": "dead", "blind": "dead", "dead": "dead"}


def load_incidents(path: Path | str) -> list[dict]:
    """Read an append-only JSONL ledger. A malformed line is reported, not skipped."""
    path = Path(path)
    if not path.is_file():
        # NOT an empty ledger. A missing file means the sweep is running without
        # its memory, and returning [] made that indistinguishable from "nothing
        # has ever gone wrong" -- the sift ran for a full cycle that way because
        # its workflow never checked out the repo holding this file.
        raise FileNotFoundError(
            f"ledger not found: {path}. A sweep without its ledger cannot detect "
            f"a recurrence, and silently reports a clean history it did not read.")
    out = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            out.append({"_malformed": f"line {number}: {exc}"})
            continue
        entry["_line"] = number
        out.append(entry)
    return out


def _incident_key(kind: str, entity: str) -> tuple[str, str]:
    # Deliberately NOT keyed on repo: a cause that moves between repos is the
    # same cause, and the whole point is noticing that it travelled.
    return (kind or "", (entity or "").strip())


def apply_incidents(findings: list[Finding], incidents: list[dict]) -> list[Finding]:
    """Escalate recurrences, and report every admitted patch, on every sweep."""
    history: dict[tuple[str, str], list[dict]] = {}
    for entry in incidents:
        if "_malformed" in entry:
            continue
        history.setdefault(_incident_key(entry.get("kind"), entry.get("entity")), []).append(entry)

    out: list[Finding] = []
    for finding in findings:
        prior = history.get(_incident_key(finding.kind, finding.entity), [])
        if not prior:
            out.append(finding)
            continue
        last = sorted(prior, key=lambda e: str(e.get("closed", "")))[-1]
        if last.get("accepted"):
            # A known, permanent condition. Annotate, never escalate: a finding
            # that is SUPPOSED to be there must not get louder every week.
            out.append(Finding(
                finding.severity, finding.kind, finding.repo, finding.entity,
                f"ACCEPTED. {finding.detail}",
                f"known since {last.get('closed', '?')} — "
                f"{last.get('systemic') or last.get('shallow') or 'no reason recorded'}"))
            continue
        worse = ESCALATION.get(finding.severity, finding.severity)
        out.append(Finding(
            worse, finding.kind, finding.repo, finding.entity,
            f"OCCURRENCE {len(prior) + 1}. {finding.detail}",
            f"the fix on {last.get('closed', '?')} did not hold — it was "
            f"\"{last.get('systemic') or last.get('shallow') or 'unrecorded'}\". "
            f"Do not repeat it: find what let it come back. Original: {finding.decision}"))

    # Patch debt is about a QUIET incident: patched, no cause recorded, waiting.
    # If the symptom is back it is already reported as a recurrence, which is the
    # louder signal -- saying both just doubles the row.
    live = {_incident_key(f.kind, f.entity) for f in findings}
    for entry in incidents:
        if "_malformed" in entry:
            out.append(Finding(
                "drift", "ledger_malformed", "wss-manager", "incidents.jsonl",
                entry["_malformed"],
                "fix the line -- an unreadable ledger silently stops catching recurrences"))
            continue
        if entry.get("systemic") or entry.get("accepted"):
            continue
        if _incident_key(entry.get("kind"), entry.get("entity")) in live:
            continue
        out.append(Finding(
            "rot", "patched_not_fixed", entry.get("repo", "?"),
            f"{entry.get('kind', '?')}: {entry.get('entity', '?')}",
            f"closed {entry.get('closed', '?')} with a fix but no recorded cause — "
            f"\"{entry.get('shallow', 'unrecorded')}\"",
            "record what stops it recurring in `systemic`, or say plainly that the "
            "symptom is all there is. This reports every sweep until one of those "
            "happens -- fixing the symptom again will not clear it"))
    return out


def find_repos(root: Path) -> list[Path]:
    """Every sibling directory that looks like a domain repo (has a registry)."""
    return sorted(p for p in root.iterdir() if (p / "registry").is_dir())


def render(findings: list[Finding], repos: list[Path]) -> str:
    lines = []
    total = sum(len(registry.load_registry(r)) for r in repos)
    lines.append(f"fleet: {len(repos)} repo(s), {total} source(s)")
    if not findings:
        lines.append("no findings -- every repo is capturing, tracked, and on one engine")
        return "\n".join(lines)

    for sev in SEVERITY:
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines.append("")
        lines.append(f"{sev.upper()}  ({len(group)})")
        for f in group:
            # The kind is printed because it is half the ledger key: closing an
            # incident means writing down {kind, entity}, and a report that only
            # prose-describes the problem makes that a guessing game.
            lines.append(f"  [{f.kind}]  {f.repo}  {f.entity}")
            lines.append(f"      {f.detail}")
            lines.append(f"      -> {f.decision}")
    return "\n".join(lines)


def run_scan(root: Path | str, *, as_json: bool = False, fail_on: str | None = None,
             ledger: Path | str | None = None,
             workflow_states: Path | str | None = None) -> tuple[str, int]:
    root = Path(root)
    repos = find_repos(root)
    states = {}
    if workflow_states:
        path = Path(workflow_states)
        if not path.is_file():
            raise FileNotFoundError(
                f"workflow states not found: {path}. A sweep told to check "
                f"whether workflows can fire, that silently checks nothing, "
                f"reports a clean fleet it never looked at.")
        states = json.loads(path.read_text())
    findings = scan(repos, states)
    if ledger:
        findings = apply_incidents(findings, load_incidents(ledger))
        findings.sort(key=lambda f: (SEVERITY.index(f.severity), f.repo, f.kind))
    if as_json:
        out = json.dumps([asdict(f) for f in findings], indent=2)
    else:
        out = render(findings, repos)
    code = 0
    if fail_on:
        limit = SEVERITY.index(fail_on)
        if any(SEVERITY.index(f.severity) <= limit for f in findings):
            code = 1
    return out, code
