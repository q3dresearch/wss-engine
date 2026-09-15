"""Describe the shape of an archive so nobody has to download it to find out.

`manifest/` records TRANSPORT -- status, bytes, sha256, etag. It says nothing
about what is inside. Without this, the only way to learn that an archive keys
on a product slug, that its date column starts in 1987, or that it holds 285
entities rather than 613,146, is to clone the repo, find the right partition and
gunzip it. That is the largest single thing standing between a dataset and
someone using it, and it is free to fix.

Inferred from the derived rows, never hand-written: a hand-maintained schema
drifts from the data silently and then lies.

PER SERIES, NOT PER SOURCE. One source can carry more than one grain -- a
product series and a fetch-shape series, say -- and a single entity count over
both is wrong in a way that looks fine. The first repo to run this reported 291
entities for a 285-row list, which is how six page-level rows were found
sharing the product entity namespace.
"""
from __future__ import annotations

import collections
import csv
import gzip
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SAMPLES = 3
DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}")
REG_KEYS = ("source_id", "schema_id", "cadence", "licence", "personal_data",
            "storage", "publisher", "publisher_tier")


def _kind(values: list[str]) -> str:
    """int | date | bool | text — inferred, and labelled as inferred."""
    v = [x for x in values if x != ""]
    if not v:
        return "empty"
    if all(x in ("0", "1") for x in v):
        return "bool"
    if all(x.lstrip("-").replace(".", "", 1).isdigit() for x in v):
        return "number"
    if all(DATEISH.match(x) for x in v):
        return "date"
    return "text"


def _open(path: str):
    return gzip.open(path, "rt", newline="") if path.endswith(".gz") else open(path, newline="")


def build(root: Path | str) -> dict:
    root = Path(root)
    parts = sorted(str(p) for p in (root / "derived" / "observations").glob("*.csv*"))
    rows: list[dict] = []
    for p in parts:
        with _open(p) as fh:
            rows.extend(csv.DictReader(fh))
    if not rows:
        return {}

    series = {}
    for sid in sorted({r.get("series_id", "") for r in rows}):
        rs = [r for r in rows if r.get("series_id") == sid]
        ents = sorted({r["entity_id"] for r in rs})
        series[sid] = {
            "rows": len(rs),
            "entities": len(ents),
            "entity_id_samples": ents[:SAMPLES],
            "metrics": sorted({r["metric"] for r in rs}),
        }

    metrics = {}
    for m in sorted({r["metric"] for r in rows}):
        rs = [r for r in rows if r["metric"] == m]
        vals = [r["value"] for r in rs]
        distinct = sorted(set(vals))
        k = _kind(vals)
        e = {"rows": len(rs), "unit": rs[0].get("unit", ""), "type": k,
             "series": sorted({r.get("series_id", "") for r in rs}),
             "entities": len({r["entity_id"] for r in rs}),
             "distinct_values": len(distinct),
             "empty": sum(1 for v in vals if v == "")}
        if k in ("number", "date") and distinct:
            nums = distinct
            if k == "number":
                nums = sorted(distinct, key=lambda x: float(x))
            e["min"], e["max"] = nums[0], nums[-1]
        e["samples"] = distinct[:SAMPLES]
        metrics[m] = e

    raws = [p for p in (root / "raw").rglob("*")
            if p.is_file() and p.name != ".gitkeep"]
    man: list[dict] = []
    for p in sorted((root / "manifest").rglob("*.csv")):
        with open(p, newline="") as fh:
            man.extend(csv.DictReader(fh))

    regs = {}
    for p in sorted((root / "registry").glob("*.yml")):
        t = p.read_text()
        d = {}
        for key in REG_KEYS:
            mm = re.search(rf"^{key}:\s*(.+)$", t, re.M)
            if mm:
                d[key] = mm.group(1).split("#")[0].strip().strip('"')
        d["endpoints"] = len(re.findall(r"^\s+- url:", t, re.M))
        regs[d.get("source_id", p.stem)] = d

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": ("Inferred from the derived rows by `wss schema`, not hand-written. "
                 "Regenerate after any derive."),
        "sources": regs,
        "observations": {
            "rows": len(rows),
            "columns": list(rows[0].keys()),
            "partitions": [Path(p).name for p in parts],
            "series": series,
            "parser_versions": sorted({r.get("parser_version", "") for r in rows}),
        },
        "metrics": metrics,
        "raw": {
            "files": len(raws),
            "bytes_on_disk": sum(os.path.getsize(p) for p in raws),
            "capture_dates": len({r["fetched_at"][:10] for r in man if r.get("fetched_at")}),
            "first_capture": min((r["fetched_at"] for r in man if r.get("fetched_at")), default=None),
            "last_capture": max((r["fetched_at"] for r in man if r.get("fetched_at")), default=None),
        },
    }


def _markdown(doc: dict) -> str:
    o = doc["observations"]
    L = ["# Data shape", "",
         f"*Generated {doc['generated_at']} by `wss schema` from the derived "
         f"rows. Do not hand-edit — regenerate after any derive.*", "",
         "**You should not need to download anything to read this.**", "",
         f"- **{o['rows']:,} observations** across {len(o['partitions'])} "
         f"partition(s), in **{len(o['series'])} series**"]
    for sid, sv in o["series"].items():
        L.append(f"  - `{sid}` — {sv['rows']:,} rows, **{sv['entities']} entities**")
    r = doc["raw"]
    L += [f"- Raw: {r['files']} file(s), {r['bytes_on_disk']:,} bytes on disk, "
          f"{r['capture_dates']} capture date(s)"
          + (f", {r['first_capture'][:10]} → {r['last_capture'][:10]}"
             if r["first_capture"] else ""),
          ""]
    if doc["sources"]:
        L += ["## Sources", "",
              "| source | cadence | endpoints | storage | personal data | licence |",
              "| --- | --- | ---: | --- | --- | --- |"]
        for sid, d in doc["sources"].items():
            L.append(f"| `{sid}` | {d.get('cadence','')} | {d.get('endpoints',0)} | "
                     f"{d.get('storage','')} | {d.get('personal_data','')} | "
                     f"{d.get('licence','')[:60]} |")
        L.append("")
    L += ["## Columns", "", "```", ", ".join(o["columns"]), "```", "",
          "`entity_id` looks like: " +
          "; ".join(f"**{sid}** " + ", ".join(f"`{e}`" for e in sv["entity_id_samples"])
                    for sid, sv in o["series"].items()), "",
          "## Metrics", "",
          "| metric | series | rows | entities | type | unit | distinct | range / samples |",
          "| --- | --- | ---: | ---: | --- | --- | ---: | --- |"]
    for m, e in doc["metrics"].items():
        rng = (f"`{e['min']}` … `{e['max']}`" if "min" in e
               else ", ".join(f"`{str(s)[:24]}`" for s in e["samples"]))
        L.append(f"| `{m}` | {', '.join(e['series'])} | {e['rows']:,} | "
                 f"{e['entities']} | {e['type']} | {e['unit']} | "
                 f"{e['distinct_values']} | {rng} |")
    L += ["", "## Partitions", ""] + [f"- `derived/observations/{p}`" for p in o["partitions"]]
    return "\n".join(L) + "\n"


def write(root: Path | str) -> tuple[Path | None, dict]:
    root = Path(root)
    doc = build(root)
    if not doc:
        return None, {}
    (root / "schema").mkdir(exist_ok=True)
    (root / "schema" / "shape.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    out = root / "SCHEMA.md"
    out.write_text(_markdown(doc))
    return out, doc
