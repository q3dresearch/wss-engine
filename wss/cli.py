"""wss CLI — the whole interface.

    wss explore <url>                             case a site before writing a registry entry
    wss init <dir> --owner <gh-owner>      scaffold a new domain repo
    wss validate                          registry schema check; CI gate
    wss plan --cadence weekly --shards 20  JSON shard array for the Actions matrix
    wss capture --cadence weekly --shard 3/20
    wss health                            rebuild health table, apply auto-disable
    wss derive --since 2026-08            raw → observation tables
    wss doctor <source_id>               dry-run one source, print raw response
    wss sources                          write SOURCES.md: every URL, licence and last capture
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

from . import (
    __version__, capture, datapage, derive, explore, fleet, health, init,
    registry, sources,
)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_explore(args: argparse.Namespace, root: Path) -> int:
    return explore.explore(args.url, source_id=args.source_id, head=args.head)


def cmd_init(args: argparse.Namespace, root: Path) -> int:
    target = Path(args.directory).resolve()
    written = init.scaffold(
        target, owner=args.owner, title=args.title, name=args.name, force=args.force
    )
    print(f"scaffolded {len(written)} files into {target}")
    print("\nnext:")
    print(f"  cd {target}")
    print("  git init && git add -A && git commit -m 'scaffold'   # .gitattributes lands before any CSV")
    print("  # edit registry/<source_id>.yml and parsers/, then:")
    print("  export WSS_CONTACT='you@example.com'")
    print("  wss validate && wss doctor <source_id>")
    return 0


def cmd_validate(args: argparse.Namespace, root: Path) -> int:
    problems = registry.validate_registry(root)
    if problems:
        for p in problems:
            _err(f"INVALID  {p}")
        _err(f"registry invalid: {len(problems)} problem(s)")
        return 1
    sources = registry.load_registry(root)
    active = registry.select(sources)
    print(f"registry OK: {len(sources)} source(s), {len(active)} active")
    return 0


def cmd_plan(args: argparse.Namespace, root: Path) -> int:
    sources = registry.load_registry(root)
    # Fail here, once, rather than identically in every shard. A missing
    # contact took out four repos after an org move and surfaced as N
    # indistinguishable shard failures at the bottom of the run; plan is the
    # first job, so one line at the top is the whole diagnosis.
    capture.contact_from_env()
    shards = registry.plan(sources, args.cadence, args.shards)
    count = len(registry.select(sources, cadence=args.cadence))
    _err(f"# {args.cadence}: {count} active source(s) across {len(shards)} non-empty shard(s) of {args.shards}")
    if args.count:
        print(count)
        return 0
    # An empty plan is the failure that hides. It produces an empty job matrix,
    # so no capture job runs, the commit job still writes its heartbeat, and the
    # run is green -- which is how two repos captured nothing for days. There is
    # no workflow worth scheduling that legitimately captures nothing, so say so
    # here rather than letting the matrix swallow it.
    if count == 0 and not args.allow_empty:
        have = collections.Counter(
            s.cadence for s in sources if s.status == "active"
        )
        declared = ", ".join(f"{c}={n}" for c, n in sorted(have.items())) or "no active sources at all"
        _err(
            f"plan selected 0 active source(s) at cadence {args.cadence!r}.\n"
            f"  the registry declares: {declared}\n"
            f"  fix CADENCE in this workflow, or the cadence on those sources.\n"
            f"  (--allow-empty if this repo really has none at this cadence yet)"
        )
        return 1
    print(registry.plan_json(sources, args.cadence, args.shards))
    return 0


def cmd_capture(args: argparse.Namespace, root: Path) -> int:
    report = capture.run_capture(root, cadence=args.cadence, shard_spec=args.shard, log=print)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items())) or "no sources in shard"
    print(f"shard {args.shard} [{args.cadence}]: {report.sources} source(s) — {summary}")
    # `plan` only emits non-empty shards, so an empty one here means the registry
    # moved under the run or CADENCE disagrees with it. Either way this job just
    # spent a runner doing nothing and would otherwise report success.
    if report.sources == 0 and not args.allow_empty:
        _err(
            f"shard {args.shard} [{args.cadence}] holds 0 source(s) — plan only emits "
            f"non-empty shards, so CADENCE is wrong or the registry changed mid-run"
        )
        return 1
    # Name them even when the run stays green: the log is the first place
    # anyone looks, and health/fleet-sift are the escalation, not the alarm.
    if report.failures:
        _err(f"could not see {report.failures} endpoint(s): {', '.join(report.failing_sources())}")
        _err("  recorded for health; auto-disable fires after 5 consecutive failures")
    if config := report.config_failures():
        _err(f"misconfigured, not rotted: {', '.join(config)} — this will not heal on its own")
        return 1
    if not report.ok:
        _err(
            f"nothing captured: {report.failures} failure(s), 0 success(es) — "
            "that is the network, the credentials or the engine, not one source"
        )
        return 1
    if args.strict and report.failures:
        _err("--strict: failing on per-source rot")
        return 1
    return 0


def cmd_health(args: argparse.Namespace, root: Path) -> int:
    sources = registry.load_registry(root)
    disabled = health.run_health(
        root, sources, threshold=args.threshold, dry_run=args.dry_run, log=print
    )
    print(f"health/health.csv rebuilt for {len(sources)} source(s); {len(disabled)} newly auto-disabled")
    return 0


def cmd_sources(args: argparse.Namespace, root: Path) -> int:
    path = sources.write(root)
    n = len(registry.load_registry(root))
    print(f"{path.name} written for {n} source(s)")
    return 0


def cmd_datapage(args: argparse.Namespace, root: Path) -> int:
    path = datapage.write(root, args.repo_url or "")
    print(f"{path} written — enable Pages (deploy from main, /docs) to publish it")
    return 0


def cmd_derive(args: argparse.Namespace, root: Path) -> int:
    stats = derive.derive(root, since=args.since, parser_modules=args.parsers, log=print)
    print(f"derived {stats['rows']} observation(s) into {len(stats['partitions'])} partition(s)")
    return 0


def cmd_fleet_scan(args: argparse.Namespace, root: Path) -> int:
    out, code = fleet.run_scan(args.dir or root, as_json=args.json,
                               fail_on=args.fail_on, ledger=args.ledger,
                               workflow_states=args.workflow_states)
    print(out)
    return code


def cmd_doctor(args: argparse.Namespace, root: Path) -> int:
    return capture.doctor(root, args.source_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wss", description=__doc__)
    parser.add_argument("--root", default=".", help="data root (default: current directory)")
    parser.add_argument("--version", action="version", version=f"wss {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("explore", help="case a URL before writing a registry entry (writes nothing)")
    p.add_argument("--head", type=int, default=0, metavar="N",
                   help="print the first N lines of the payload verbatim. The only way "
                        "to read a source that answers a runner but not your laptop. "
                        "Text only, capped at 64 KB -- never use it on a source that "
                        "carries personal data, because a workflow log is as public as "
                        "its repo")
    p.add_argument("url")
    p.add_argument(
        "--source-id",
        default="publisher.domain.series",
        help="id to use in the suggested registry entry",
    )

    p = sub.add_parser("init", help="scaffold a new domain repo from the engine's templates")
    p.add_argument("directory", help="where to create the repo, e.g. ../wss-arxiv")
    p.add_argument("--owner", required=True, help="GitHub owner that will host this repo and the engine")
    p.add_argument("--title", help="human title, e.g. 'arXiv Listing History' (default: derived from the name)")
    p.add_argument("--name", help="repo name (default: the directory's basename)")
    p.add_argument("--force", action="store_true", help="write into a non-empty directory")

    sub.add_parser("validate", help="validate the registry; non-zero exit on any problem")

    p = sub.add_parser("plan", help="print the JSON shard array for the workflow matrix")
    p.add_argument("--cadence", required=True, choices=sorted(registry.CADENCE_HOURS))
    p.add_argument("--count", action="store_true", help="print how many sources match, nothing else")
    p.add_argument("--allow-empty", action="store_true",
                   help="succeed even if no active source matches (default: fail)")
    p.add_argument("--shards", type=int, default=20)

    p = sub.add_parser("capture", help="capture one shard of the active sources")
    p.add_argument("--cadence", required=True, choices=sorted(registry.CADENCE_HOURS))
    p.add_argument("--allow-empty", action="store_true",
                   help="succeed even if the shard holds no sources (default: fail)")
    p.add_argument("--strict", action="store_true",
                   help="fail on any quarantined/error source (pre-0.6.8 behaviour)")
    p.add_argument("--shard", default="1/1", help="e.g. 3/20 (default 1/1 = everything)")

    p = sub.add_parser("health", help="rebuild the health table and apply auto-disable")
    p.add_argument("--threshold", type=int, default=health.AUTO_DISABLE_THRESHOLD)
    p.add_argument("--dry-run", action="store_true", help="compute health but do not flip statuses")

    sub.add_parser("sources", help="write SOURCES.md — every source URL, licence and last capture")

    p = sub.add_parser(
        "datapage",
        help="write docs/index.html with schema.org Dataset markup, so the "
             "dataset is discoverable by Google Dataset Search",
    )
    p.add_argument(
        "--repo-url",
        help="canonical repository URL, e.g. https://github.com/<owner>/<repo>",
    )

    p = sub.add_parser("derive", help="rebuild observation tables from the raw archive")
    p.add_argument("--since", help="only rebuild partitions from this month on, e.g. 2026-08")
    p.add_argument(
        "--parsers",
        action="append",
        default=[],
        metavar="MODULE",
        help="override parser discovery with an explicit module (repeatable); "
        "by default every module in the repo's parsers/ package is loaded",
    )

    p = sub.add_parser("doctor", help="dry-run one source and print the raw response")
    p.add_argument("source_id")

    p = sub.add_parser(
        "fleet-scan",
        help="inspect every repo under a directory and print the decisions waiting",
    )
    p.add_argument("--dir", help="directory holding the repo checkouts (default: --root)")
    p.add_argument("--json", action="store_true", help="machine-readable findings")
    p.add_argument("--workflow-states",
                   help="JSON of {repo: {workflow: state}} from the GitHub API. "
                        "Without it the scan cannot tell a workflow GitHub has "
                        "switched off from one that is simply quiet.")
    p.add_argument(
        "--fail-on",
        choices=fleet.SEVERITY,
        help="exit 1 if any finding is at this severity or worse",
    )
    p.add_argument(
        "--ledger",
        help="incidents.jsonl — escalates anything that has been fixed before, and "
             "reports every incident closed without a recorded cause",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    # Local convenience: <root>/.env.local supplies credentials for local runs.
    # Real environment variables always win, so CI secrets are never shadowed.
    capture.load_env_file(root)
    handlers = {
        "explore": cmd_explore,
        "init": cmd_init,
        "validate": cmd_validate,
        "plan": cmd_plan,
        "capture": cmd_capture,
        "health": cmd_health,
        "derive": cmd_derive,
        "doctor": cmd_doctor,
        "fleet-scan": cmd_fleet_scan,
        "sources": cmd_sources,
        "datapage": cmd_datapage,
    }
    try:
        return handlers[args.cmd](args, root)
    except registry.RegistryError as exc:
        _err(f"registry invalid:\n{exc}")
        return 1
    except capture.ContactMissing as exc:
        _err(str(exc))
        return 2
    except derive.DeriveError as exc:
        _err(str(exc))
        return 1
    except init.InitError as exc:
        _err(str(exc))
        return 1


def run() -> None:  # console_scripts entry point
    sys.exit(main())


if __name__ == "__main__":
    run()
