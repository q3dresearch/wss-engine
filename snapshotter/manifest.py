"""Append-only manifest — every fetch leaves a row, including unchanged ones.

"We looked and it was identical" is what makes a revision date defensible
later. Content-hash dedupe skips the file write, never the observation.

Layout: manifest/<source_id>/<YYYY-MM>.csv (monthly partitions, small diffs,
lexicographic order == chronological order). The manifest always stays in
git; its commit history is the provenance record.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

COLUMNS = [
    "source_id",
    "url",
    "fetched_at",
    "http_status",
    "content_type",
    "content_length",
    "content_sha256",
    "etag",
    "last_modified",
    "outcome",
    "raw_ref",
    "reason",
    "warnings",
]

OUTCOMES = ("first_capture", "changed", "unchanged", "quarantined", "error", "skipped")
SUCCESS_OUTCOMES = ("first_capture", "changed", "unchanged")
FAILURE_OUTCOMES = ("quarantined", "error")


def manifest_dir(root: Path | str, source_id: str) -> Path:
    return Path(root) / "manifest" / source_id


def manifest_path(root: Path | str, source_id: str, fetched_at: str) -> Path:
    return manifest_dir(root, source_id) / f"{fetched_at[:7]}.csv"


def append_row(root: Path | str, row: dict) -> None:
    unknown = set(row) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown manifest columns: {sorted(unknown)}")
    if row.get("outcome") not in OUTCOMES:
        raise ValueError(f"invalid outcome: {row.get('outcome')!r}")
    path = manifest_path(root, row["source_id"], row["fetched_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        if is_new:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in COLUMNS})


def source_files(root: Path | str, source_id: str) -> list[Path]:
    d = manifest_dir(root, source_id)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix == ".csv")


def _read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def iter_rows(root: Path | str, source_id: str) -> Iterator[dict]:
    """All rows for a source in chronological (append) order."""
    for path in source_files(root, source_id):
        yield from _read_rows(path)


def last_capture(root: Path | str, source_id: str, url: str) -> dict | None:
    """Most recent successful row for this URL — the dedupe/shrink baseline."""
    for path in reversed(source_files(root, source_id)):
        for row in reversed(_read_rows(path)):
            if row.get("url") == url and row.get("outcome") in SUCCESS_OUTCOMES:
                return row
    return None
