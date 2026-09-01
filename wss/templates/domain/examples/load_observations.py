#!/usr/bin/env python3
"""Load derived/observations/*.csv into sqlite and run examples/queries.sql.

    python examples/load_observations.py [--db observations.db]

Stdlib only. For heavier analysis, DuckDB reads the partitions directly:

    SELECT * FROM read_csv_auto('derived/observations/*.csv');
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COLUMNS = [
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


def load(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("DROP TABLE IF EXISTS observations")
    con.execute(f"CREATE TABLE observations ({', '.join(c + ' TEXT' for c in COLUMNS)})")
    placeholders = ", ".join("?" for _ in COLUMNS)
    total = 0
    for partition in sorted((REPO / "derived" / "observations").glob("*.csv")):
        with partition.open(encoding="utf-8", newline="") as fh:
            rows = [[r[c] for c in COLUMNS] for r in csv.DictReader(fh)]
        con.executemany(f"INSERT INTO observations VALUES ({placeholders})", rows)
        total += len(rows)
        print(f"loaded {partition.name}: {len(rows)} rows")
    con.execute("CREATE INDEX idx_obs ON observations (metric, entity_id, observed_at)")
    con.commit()
    print(f"total: {total} observations")
    return con


def run_queries(con: sqlite3.Connection) -> None:
    sql = (REPO / "examples" / "queries.sql").read_text(encoding="utf-8")
    # Strip comments BEFORE splitting: a semicolon inside a comment would
    # otherwise cut a statement in half.
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    for chunk in body.split(";"):
        statement = chunk.strip()
        if not statement:
            continue
        cur = con.execute(statement)
        headers = [d[0] for d in cur.description]
        print("\n" + " | ".join(headers))
        for row in cur.fetchmany(15):
            print(" | ".join(str(v) for v in row))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=":memory:")
    args = ap.parse_args()
    run_queries(load(args.db))
