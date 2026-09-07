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
GATE_ROT_RATE = 0.50      # half the fetches quarantined = the page has moved on


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
            "swallows_plan": bool(re.search(r'echo\s+"[^"]*\$\(\s*wss\s+plan', text)),
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

    paused = [s.source_id for s in sources if s.status == "paused"]
    for sid in paused:
        found.append(Finding("drift", "paused", name, sid, "paused, not retired",
                             "resume it or retire it -- a permanent pause is a decision nobody made"))
    return found


def scan(repos: list[Path]) -> list[Finding]:
    found: list[Finding] = []
    for repo in repos:
        found.extend(scan_repo(repo))

    # Pin skew is the one thing only visible across repos.
    pins = {r.name: _pin(r) for r in repos if _pin(r)}
    if len(set(pins.values())) > 1:
        newest = max(set(pins.values()), key=lambda v: [int(x) for x in v.lstrip("v").split(".")])
        for repo_name, pin in sorted(pins.items()):
            if pin != newest:
                found.append(Finding("drift", "pin_skew", repo_name, repo_name,
                                     f"engine {pin}, fleet is on {newest}",
                                     f"re-pin to {newest} so a fix reaches every repo"))
    found.sort(key=lambda f: (SEVERITY.index(f.severity), f.repo, f.kind))
    return found


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
            lines.append(f"  {f.repo}  {f.entity}")
            lines.append(f"      {f.detail}")
            lines.append(f"      -> {f.decision}")
    return "\n".join(lines)


def run_scan(root: Path | str, *, as_json: bool = False, fail_on: str | None = None) -> tuple[str, int]:
    root = Path(root)
    repos = find_repos(root)
    findings = scan(repos)
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
