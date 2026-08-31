"""snapshotter CLI — the whole interface.

    snapshotter validate                          registry schema check; CI gate
    snapshotter plan --cadence daily --shards 20  JSON shard array for the Actions matrix
    snapshotter capture --cadence daily --shard 3/20
    snapshotter health                            rebuild health table, apply auto-disable
    snapshotter derive --since 2026-08            raw → observation tables
    snapshotter doctor <source_id>               dry-run one source, print raw response
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, capture, derive, health, registry


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


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
    shards = registry.plan(sources, args.cadence, args.shards)
    count = len(registry.select(sources, cadence=args.cadence))
    _err(f"# {args.cadence}: {count} active source(s) across {len(shards)} non-empty shard(s) of {args.shards}")
    print(registry.plan_json(sources, args.cadence, args.shards))
    return 0


def cmd_capture(args: argparse.Namespace, root: Path) -> int:
    report = capture.run_capture(root, cadence=args.cadence, shard_spec=args.shard, log=print)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items())) or "no sources in shard"
    print(f"shard {args.shard} [{args.cadence}]: {report.sources} source(s) — {summary}")
    if not report.ok:
        _err("capture had error/quarantined outcomes — failing loudly")
        return 1
    return 0


def cmd_health(args: argparse.Namespace, root: Path) -> int:
    sources = registry.load_registry(root)
    disabled = health.run_health(
        root, sources, threshold=args.threshold, dry_run=args.dry_run, log=print
    )
    print(f"health/health.csv rebuilt for {len(sources)} source(s); {len(disabled)} newly auto-disabled")
    return 0


def cmd_derive(args: argparse.Namespace, root: Path) -> int:
    stats = derive.derive(root, since=args.since, parser_modules=args.parsers, log=print)
    print(f"derived {stats['rows']} observation(s) into {len(stats['partitions'])} partition(s)")
    return 0


def cmd_doctor(args: argparse.Namespace, root: Path) -> int:
    return capture.doctor(root, args.source_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snapshotter", description=__doc__)
    parser.add_argument("--root", default=".", help="data root (default: current directory)")
    parser.add_argument("--version", action="version", version=f"snapshotter {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="validate the registry; non-zero exit on any problem")

    p = sub.add_parser("plan", help="print the JSON shard array for the workflow matrix")
    p.add_argument("--cadence", required=True, choices=sorted(registry.CADENCE_HOURS))
    p.add_argument("--shards", type=int, default=20)

    p = sub.add_parser("capture", help="capture one shard of the active sources")
    p.add_argument("--cadence", required=True, choices=sorted(registry.CADENCE_HOURS))
    p.add_argument("--shard", default="1/1", help="e.g. 3/20 (default 1/1 = everything)")

    p = sub.add_parser("health", help="rebuild the health table and apply auto-disable")
    p.add_argument("--threshold", type=int, default=health.AUTO_DISABLE_THRESHOLD)
    p.add_argument("--dry-run", action="store_true", help="compute health but do not flip statuses")

    p = sub.add_parser("derive", help="rebuild observation tables from the raw archive")
    p.add_argument("--since", help="only rebuild partitions from this month on, e.g. 2026-08")
    p.add_argument(
        "--parsers",
        action="append",
        default=[],
        metavar="MODULE",
        help="importable module that registers parsers (repeatable), e.g. parsers.adoption_v1",
    )

    p = sub.add_parser("doctor", help="dry-run one source and print the raw response")
    p.add_argument("source_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    handlers = {
        "validate": cmd_validate,
        "plan": cmd_plan,
        "capture": cmd_capture,
        "health": cmd_health,
        "derive": cmd_derive,
        "doctor": cmd_doctor,
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


def run() -> None:  # console_scripts entry point
    sys.exit(main())


if __name__ == "__main__":
    run()
