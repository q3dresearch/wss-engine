"""Sandbox driver: simulate → derive → analyse → assert planted ground truth.

    python sandbox/run.py [--days 120] [--out sandbox/out]

Exits non-zero if the analysis fails to recover what simulate.py planted —
so the analysis layer is proven before a single real byte is captured.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SANDBOX_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SANDBOX_DIR.parent))
sys.path.insert(0, str(SANDBOX_DIR))

from wss import csvio, derive, manifest  # noqa: E402
from wss.cohort import CohortCriteria, effective_members, select_vintage, write_vintage  # noqa: E402
from simulate import SOURCE_ID, simulate  # noqa: E402


def load_queries(sql_path: Path) -> dict[str, str]:
    queries: dict[str, str] = {}
    name = None
    buf: list[str] = []
    for line in sql_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- query: "):
            if name:
                queries[name] = "\n".join(buf).strip()
            name = line.removeprefix("-- query: ").strip()
            buf = []
        elif name is not None:
            buf.append(line)
    if name:
        queries[name] = "\n".join(buf).strip()
    return queries


def derived_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in csvio.partition_paths(root / "derived" / "observations"):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def build_db(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    cols = ", ".join(f"{c} TEXT" for c in derive.OBS_COLUMNS)
    con.execute(f"CREATE TABLE observations ({cols})")
    placeholders = ", ".join("?" for _ in derive.OBS_COLUMNS)
    for path in csvio.partition_paths(root / "derived" / "observations"):
        rows = [[r[c] for c in derive.OBS_COLUMNS] for r in csvio.read_csv(path)]
        con.executemany(f"INSERT INTO observations VALUES ({placeholders})", rows)
    con.commit()
    return con


def print_rows(title: str, headers: list[str], rows: list[tuple]) -> None:
    print(f"\n== {title} ==")
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    print("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def check(label: str, condition: bool, failures: list[str]) -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--out", default=str(SANDBOX_DIR / "out"))
    args = ap.parse_args()
    if args.days < 110:
        ap.error("--days must be >= 110 so every planted trajectory has time to play out")

    root = Path(args.out)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    truth = simulate(root, days=args.days)
    print(f"simulated {args.days} days of {truth['source_id']} ({truth['start_date']} → {truth['end_date']})")

    derive.derive(root, parser_modules=["parser_sandbox"])
    first_digest = derived_digest(root)
    derive.derive(root, parser_modules=["parser_sandbox"])
    second_digest = derived_digest(root)

    queries = load_queries(SANDBOX_DIR / "analysis.sql")
    con = build_db(root)
    results: dict[str, list[tuple]] = {}
    for name, sql in queries.items():
        cur = con.execute(sql)
        results[name] = cur.fetchall()
        print_rows(name, [d[0] for d in cur.description], results[name])

    print("\n== ground truth checks ==")
    failures: list[str] = []
    growth = {r[0]: r[3] for r in results["growth_28d"]}
    lifespan = {r[0]: (r[1], r[2], r[3]) for r in results["lifespan"]}
    overtake_at = results["overtake"][0][0]
    end_ts = f"{truth['end_date']}T22:10:03Z"

    check("derived/ rebuilds byte-identically", first_digest == second_digest, failures)
    check("challenger accelerating: nova-lm 28d growth > +100%", (growth.get("sandbox/nova-lm") or 0) > 100, failures)
    check("incumbent decaying: legacy-gpt 28d growth < -10%", (growth.get("sandbox/legacy-gpt") or 0) < -10, failures)
    check("plateau: steady-diffuser 28d growth within ±10%", abs(growth.get("sandbox/steady-diffuser") or 99) < 10, failures)
    check("faded: flash-1b collapsed (28d growth < -50%)", (growth.get("sandbox/flash-1b") or 0) < -50, failures)
    check("overtake happened before series end", overtake_at is not None and overtake_at < end_ts, failures)
    check("faded: flash-1b gone before series end", lifespan["sandbox/flash-1b"][2] == "gone", failures)
    check(
        "survivors alive at series end",
        all(lifespan[e][2] == "alive" for e in ("sandbox/legacy-gpt", "sandbox/nova-lm", "sandbox/steady-diffuser")),
        failures,
    )
    check(
        "new entrant: nova-lm first seen on its creation day",
        lifespan["sandbox/nova-lm"][0].startswith(truth["nova_first_date"]),
        failures,
    )

    rows = list(manifest.iter_rows(root, SOURCE_ID))
    unchanged = [r for r in rows if r["outcome"] == "unchanged"]
    check("dedupe: at least one `unchanged` manifest row", len(unchanged) >= 1, failures)
    obs_dates = {r[0][:10] for r in con.execute("SELECT observed_at FROM observations")}
    check(
        "unchanged day still produced dated observations",
        truth["unchanged_date"] in obs_dates,
        failures,
    )
    check(
        "quarantined day left a gap (never entered the archive)",
        truth["quarantine_date"] not in obs_dates,
        failures,
    )

    # Cohort demo: freeze a vintage mid-series, another near the end; the
    # faded member stays in the effective cohort after it dies.
    start = date.fromisoformat(truth["start_date"])
    cohort_dir = root / "cohorts" / "sandbox-demo"

    def candidates_on(day_iso: str) -> list[dict]:
        rows = con.execute(
            "SELECT entity_id, CAST(value AS REAL) FROM observations "
            "WHERE metric='downloads_30d' AND substr(observed_at, 1, 10) = ?",
            (day_iso,),
        ).fetchall()
        created = {"sandbox/nova-lm": truth["nova_first_date"]}
        return [{"id": e, "downloads": v, "createdAt": created.get(e, "2024-01-01")} for e, v in rows]

    criteria = CohortCriteria(
        metric_key="downloads",
        created_key="createdAt",
        established_top_n=2,
        established_floor=100_000,
        new_entrant_since=truth["nova_first_date"],
    )
    for day in (30, args.days - 10):
        day_iso = (start + timedelta(days=day)).isoformat()
        write_vintage(cohort_dir, select_vintage(candidates_on(day_iso), criteria, day_iso))
    cohort = effective_members(cohort_dir)
    check("cohort: new entrant qualified while tiny", "new_entrant" in cohort["sandbox/nova-lm"]["paths"], failures)
    check("cohort: faded member frozen in forever", "sandbox/flash-1b" in cohort, failures)

    if failures:
        print(f"\nSANDBOX FAILED — {len(failures)} check(s) did not recover the planted truth")
        return 1
    print("\nSANDBOX OK — analysis recovered every planted ground truth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
