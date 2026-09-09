"""Derive: raw archive → long-format observation tables.

    series_id, entity_id, observed_at, captured_at,
    metric, value, unit, source_id, raw_ref, parser_version

observed_at is when the fact was true; captured_at is when we saw it. That
separation is the point-in-time guarantee (bitemporal modelling / SCD Type 2).

Parsers are plugins keyed by schema_id. A parser must be a pure function of
the response bytes — a parser bug is fixed by re-parsing the archive, never
by re-fetching. Leave observed_at None unless the payload itself carries the
observation time; derive fills it with the fetch time of each manifest row
(so an `unchanged` fetch still yields its own dated observation).
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import manifest, storage
from .csvio import format_value, partition_paths, write_csv
from .registry import Source, load_registry

OBS_COLUMNS = [
    "series_id",
    "entity_id",
    "observed_at",
    "captured_at",
    "metric",
    "value",
    "unit",
    "source_id",
    "raw_ref",
    "parser_version",
]


class DeriveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    entity_id: str
    metric: str
    value: object
    unit: str = ""
    series_id: str | None = None  # defaults to the source_id
    observed_at: str | None = None  # defaults to the manifest row's fetched_at


@dataclass(frozen=True)
class ParseContext:
    source: Source
    url: str
    raw_ref: str


Parser = Callable[[bytes, ParseContext], Iterable[Observation]]

_PARSERS: dict[str, tuple[Parser, str]] = {}


def register(schema_id: str, fn: Parser, version: str = "1") -> None:
    _PARSERS[schema_id] = (fn, str(version))


ARCHIVE_SCHEMA = "archive.v1"


def _archive_parser(body: bytes, ctx: ParseContext):
    """Built-in parser for `schema_id: archive.v1` — document tracking.

    Some sources exist to be archived and watched for change, not measured:
    court opinions, investor-relations decks, terms of service. Their value
    is the raw bytes plus the manifest's changed/unchanged trail.

    One observation per capture keeps them visible in the observation table:
    a jump in `content_bytes` between two dates is a silent revision, with
    both versions already in the archive.
    """
    yield Observation(
        entity_id=ctx.url,
        metric="content_bytes",
        value=len(body),
        unit="bytes",
    )


register(ARCHIVE_SCHEMA, _archive_parser, "1")


def _reset_builtin_parsers() -> None:
    """Restore the built-ins after clear_parsers() (tests)."""
    register(ARCHIVE_SCHEMA, _archive_parser, "1")


def registered() -> dict[str, str]:
    return {schema: version for schema, (_, version) in _PARSERS.items()}


def discover_parsers(root: Path | str) -> list[str]:
    """Every module in the repo's `parsers/` package, as importable names.

    Adding a parser should need no more ceremony than adding a source, so
    derive finds them itself. `--parsers` overrides this for the odd case of
    parsers living somewhere else.
    """
    parsers_dir = Path(root) / "parsers"
    if not parsers_dir.is_dir():
        return []
    return [
        f"parsers.{p.stem}"
        for p in sorted(parsers_dir.glob("*.py"))
        if p.stem != "__init__"
    ]


def clear_parsers(keep_builtins: bool = True) -> None:
    _PARSERS.clear()
    if keep_builtins:
        _reset_builtin_parsers()


def derive(
    root: Path | str,
    since: str | None = None,
    parser_modules: Iterable[str] = (),
    log: Callable[[str], None] = lambda s: None,
) -> dict:
    """Rebuild derived/observations/<YYYY-MM>.csv from raw + manifest.

    Deterministic: sorted rows, canonical formatting — a rebuild over the
    same archive is byte-identical. `since` limits the rebuild to partitions
    from that month on (a full rebuild also prunes stale partitions).
    """
    root = Path(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    modules = list(parser_modules) or discover_parsers(root)
    for module in modules:
        importlib.import_module(module)

    sources = load_registry(root)
    missing: set[str] = set()
    obs_rows: list[dict] = []
    parse_cache: dict[str, list[Observation]] = {}

    for source in sources:
        entry = _PARSERS.get(source.schema_id)
        store = storage.store_for(source, root)
        for row in manifest.iter_rows(root, source.source_id):
            if row.get("outcome") not in manifest.SUCCESS_OUTCOMES:
                continue
            # `unchanged` means the bytes were identical, so re-parsing them
            # produces observations that differ only in observed_at. For a
            # membership series that restatement IS the signal ("still short on
            # the 7th"). For a slow register it is pure volume -- 35% of
            # wss-mining-pipeline's partition was the same fact on a later date.
            # The manifest still records that we looked and it was the same.
            if source.restate == "on_change" and row["outcome"] == "unchanged":
                continue
            if since and row["fetched_at"][:7] < since:
                continue
            if entry is None:
                missing.add(source.schema_id)
                continue
            fn, version = entry
            raw_ref = row["raw_ref"]
            if raw_ref not in parse_cache:
                body = store.read(raw_ref)
                ctx = ParseContext(source=source, url=row["url"], raw_ref=raw_ref)
                parse_cache[raw_ref] = list(fn(body, ctx))
            for obs in parse_cache[raw_ref]:
                obs_rows.append(
                    {
                        "series_id": obs.series_id or source.source_id,
                        "entity_id": obs.entity_id,
                        "observed_at": obs.observed_at or row["fetched_at"],
                        "captured_at": row["fetched_at"],
                        "metric": obs.metric,
                        "value": format_value(obs.value),
                        "unit": obs.unit,
                        "source_id": source.source_id,
                        "raw_ref": raw_ref,
                        "parser_version": version,
                    }
                )

    if missing:
        raise DeriveError(
            f"no parser registered for schema_id(s): {', '.join(sorted(missing))} "
            f"— pass --parsers <module> (registered: {sorted(registered()) or 'none'})"
        )

    # A source may be looked at many times and stay unchanged, which writes one
    # manifest row per look against the same raw_ref. Where the parser takes
    # observed_at from the payload rather than the fetch time — which the screen
    # asks it to — those looks produce byte-identical observations. Collapse
    # them to the earliest capture that saw the state.
    #
    # Only exact repeats collapse. Two rows sharing a key but disagreeing on
    # value are a parser problem and are left in place to be noticed.
    deduped: dict[tuple, dict] = {}
    for row in obs_rows:
        key = tuple(row[c] for c in OBS_COLUMNS if c != "captured_at")
        prior = deduped.get(key)
        if prior is None or row["captured_at"] < prior["captured_at"]:
            deduped[key] = row
    obs_rows = list(deduped.values())

    by_month: dict[str, list[dict]] = {}
    for row in obs_rows:
        by_month.setdefault(row["observed_at"][:7], []).append(row)

    out_dir = root / "derived" / "observations"
    written: list[str] = []
    for month in sorted(by_month):
        rows = sorted(by_month[month], key=lambda r: tuple(r[c] for c in OBS_COLUMNS))
        path = out_dir / f"{month}.csv.gz"
        write_csv(path, OBS_COLUMNS, rows)
        written.append(path.name)
        # The plain .csv from before the changeover. Leaving it would mean two
        # copies of one month, and any reader globbing "*.csv" would quietly
        # pick the stale one.
        legacy = out_dir / f"{month}.csv"
        if legacy.is_file():
            legacy.unlink()
            log(f"replaced {legacy.relative_to(root)} with {path.name}")
        log(f"wrote {path.relative_to(root)} ({len(rows)} rows)")

    if since is None and out_dir.is_dir():
        for path in partition_paths(out_dir):
            if path.name not in written:
                path.unlink()
                log(f"pruned stale partition {path.relative_to(root)}")

    return {"rows": len(obs_rows), "partitions": written}
