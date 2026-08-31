"""Deterministic CSV writing — the conventions every committed table follows.

LF line endings, UTF-8, explicit header, stable column order, sorted rows,
trailing newline, no locale formatting. A rewritten file must be
byte-identical when its inputs are unchanged.
"""

from __future__ import annotations

import csv
from pathlib import Path


def write_csv(path: Path | str, columns: list[str], rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def format_value(value) -> str:
    """Canonical, locale-free scalar formatting."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)
