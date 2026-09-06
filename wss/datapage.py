"""Generate docs/index.html — the one page a machine can read.

A README is not enough, for a mechanical reason rather than an editorial one:
GitHub sanitises ``<script>`` out of rendered markdown, and Google Dataset
Search discovers datasets *only* through a ``schema.org/Dataset`` JSON-LD
block. A repository without such a page is structurally invisible to it, no
matter how good the README is.

So this writes a page that is generated, never maintained. Everything on it
comes from files already in the repo — ``CITATION.cff`` for identity and
licence, the registry for what is captured, the manifest for how far back the
history actually runs. It fetches nothing and it invents nothing: a field that
is missing stays missing rather than being guessed.

Publishing is a repo setting (Pages → deploy from ``main``/``docs``), done
once per repo and not by this command.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import yaml

from . import manifest, registry

FILENAME = Path("docs") / "index.html"
_TODO = "TODO:"


def _citation(root: Path) -> dict:
    path = root / "CITATION.cff"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _coverage(root: Path, sources) -> tuple[str, str]:
    """Earliest and latest stored capture, as an ISO date pair."""
    first = last = ""
    for s in sources:
        for row in manifest.iter_rows(root, s.source_id):
            if not row.get("content_sha256"):
                continue
            at = (row.get("fetched_at") or "")[:10]
            if not at:
                continue
            first = at if not first or at < first else first
            last = at if not last or at > last else last
    return first, last


def _creators(cff: dict) -> list[dict]:
    """Authors as schema.org, using the public profile and never an email."""
    out = []
    for a in cff.get("authors") or []:
        name = a.get("name") or " ".join(
            x for x in (a.get("given-names"), a.get("family-names")) if x
        )
        if not name:
            continue
        person = {"@type": "Person", "name": name}
        site = a.get("website")
        if site:
            person["url"] = site
        out.append(person)
    return out


def build(root: Path | str, repo_url: str = "") -> str:
    root = Path(root)
    cff = _citation(root)
    sources = sorted(registry.load_registry(root), key=lambda s: s.source_id)
    active = [s for s in sources if s.status == "active"]
    first, last = _coverage(root, sources)

    name = cff.get("title") or root.name
    abstract = (cff.get("abstract") or "").strip()
    if abstract.startswith(_TODO):
        abstract = ""
    description = abstract or (
        f"Point-in-time captures from {len(active)} "
        f"source{'s' if len(active) != 1 else ''} that do not keep their own "
        f"history. Each capture stores the bytes as served, with a manifest "
        f"row recording the URL, fetch time and SHA-256."
    )
    licence = cff.get("license") or "CC-BY-4.0"
    doi = cff.get("doi") or ""
    repo_url = repo_url or f"https://github.com/{root.name}"

    ld: dict = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "name": name,
        "description": description,
        "url": repo_url,
        "license": licence,
        "isAccessibleForFree": True,
        "keywords": list(cff.get("keywords") or []),
        "creator": _creators(cff),
        "distribution": [{
            "@type": "DataDownload",
            "encodingFormat": "text/csv",
            "contentUrl": f"{repo_url.rstrip('/')}/tree/main/data",
        }],
    }
    if doi:
        ld["identifier"] = doi if doi.startswith("http") else f"https://doi.org/{doi}"
    if first and last:
        ld["temporalCoverage"] = f"{first}/{last}"
    if active:
        ld["isBasedOn"] = [
            {"@type": "Dataset", "name": s.publisher, "url": s.endpoints[0].url}
            for s in active if s.endpoints
        ]
    ld = {k: v for k, v in ld.items() if v not in ([], "", None)}

    e = html.escape
    rows = "\n".join(
        f"      <tr><td><code>{e(s.source_id)}</code></td><td>{e(s.publisher)}</td>"
        f"<td>{e(s.cadence)}</td><td>{e(s.licence)}</td></tr>"
        for s in active
    )
    cover = (
        f"<p class=\"cov\">History held: <strong>{e(first)}</strong> to "
        f"<strong>{e(last)}</strong>.</p>" if first and last else ""
    )
    cite = (
        f"<p>Cite this dataset: <a href=\"https://doi.org/{e(doi)}\">"
        f"https://doi.org/{e(doi)}</a></p>" if doi else
        "<p>A DOI is minted per release once the repository is connected to "
        "Zenodo.</p>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)}</title>
<meta name="description" content="{e(description[:300])}">
<script type="application/ld+json">
{json.dumps(ld, indent=2, ensure_ascii=False)}
</script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
         Helvetica, Arial, sans-serif; max-width: 46rem; margin: 0 auto;
         padding: 2.5rem 1.25rem; }}
  h1 {{ font-size: 1.6rem; margin-bottom: .25rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ text-align: left; padding: .4rem .6rem;
            border-bottom: 1px solid rgba(128,128,128,.3); font-size: .92rem; }}
  code {{ font-size: .9em; }}
  .cov {{ font-size: .95rem; }}
  footer {{ margin-top: 2.5rem; font-size: .85rem; opacity: .75; }}
</style>
</head>
<body>
<h1>{e(name)}</h1>
<p>{e(description)}</p>
{cover}

<h2>What is captured</h2>
<table>
  <thead><tr><th>source</th><th>publisher</th><th>cadence</th><th>terms</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>

<h2>Using it</h2>
<p>Derived tables are CSV under <code>data/</code> in
<a href="{e(repo_url)}">the repository</a>; the bytes they came from are under
<code>raw/</code>, and <code>SOURCES.md</code> lists every source URL.</p>
<p>Every observation carries <code>source_id</code> and <code>raw_ref</code>. The
matching manifest row holds that <code>raw_ref</code> with the URL, the fetch
timestamp and a SHA-256 of exactly what came back, so any number here traces to
the bytes it came from.</p>
{cite}

<footer>
<p>Repository code and captured data are licensed separately. This dataset is
offered under {e(licence)}; each publisher's own terms govern their material and
are listed per source above. Generated by <code>wss datapage</code> — do not edit
by hand.</p>
</footer>
</body>
</html>
"""


def write(root: Path | str, repo_url: str = "") -> Path:
    root = Path(root)
    path = root / FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(root, repo_url), encoding="utf-8")
    return path
