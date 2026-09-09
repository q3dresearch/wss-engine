"""Deterministic CSV writing — the conventions every committed table follows.

LF line endings, UTF-8, explicit header, stable column order, sorted rows,
trailing newline, no locale formatting. A rewritten file must be
byte-identical when its inputs are unchanged.
"""

from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path


def write_csv(path: Path | str, columns: list[str], rows: list[dict]) -> None:
    """Write a CSV, gzipped when the path says .gz.

    The observation table is long-format and hugely repetitive -- the same
    entity_id, metric and timestamp strings over and over -- so it compresses
    about 27x. One partition measured 55.0 MB plain and 2.0 MB gzipped, past
    GitHub's 50 MB advisory and heading for its 100 MB hard refusal.

    What this costs: `head file.csv` no longer works (use `zcat`), and git can
    no longer diff the file line by line. Neither matters here -- the bot
    commits these and nobody reviews the diff -- and `raw/` has always been
    gzipped, so this makes derived consistent with it rather than novel.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    def opener():
        if path.suffix != ".gz":
            return path.open("w", encoding="utf-8", newline="")
        # mtime=0 is not a detail. gzip stores a timestamp in its header, so
        # two writes of identical content produce different BYTES -- and a
        # re-derive would rewrite every partition, churn every commit, and
        # destroy the "unchanged means unchanged" property the whole archive
        # rests on. io.TextIOWrapper because GzipFile is binary-only.
        raw = gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"),
                            compresslevel=6, mtime=0)
        return io.TextIOWrapper(raw, encoding="utf-8", newline="")
    with opener() as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def read_csv(path: Path | str):
    """Rows from a CSV, gzipped or not. One reader so callers need not care."""
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def partition_paths(directory: Path | str) -> list[Path]:
    """Every observation partition, gzipped or not, sorted by month.

    Both suffixes are matched during the changeover: a repo that has not
    re-derived yet still holds plain .csv, and a glob for one suffix would
    silently read half a fleet.
    """
    directory = Path(directory)
    seen: dict[str, Path] = {}
    for pattern in ("*.csv", "*.csv.gz"):
        for f in directory.glob(pattern):
            month = f.name.split(".")[0]
            # Prefer the gzipped copy where both exist.
            if month not in seen or f.suffix == ".gz":
                seen[month] = f
    return [seen[m] for m in sorted(seen)]


def format_value(value) -> str:
    """Canonical, locale-free scalar formatting."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)
