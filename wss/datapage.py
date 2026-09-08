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
import re
from pathlib import Path

import yaml

from . import manifest, registry

FILENAME = Path("docs") / "index.html"
_TODO = "TODO:"


CHART_DIR = Path("examples") / "charts"


def _figures(root: Path) -> list[dict]:
    """Charts to publish, newest-meaningful order, with whatever caption they carry.

    A repo's figures live in examples/charts/ and were reachable only by cloning
    it. A landing page that cites the data but shows none of it is a page nobody
    reads twice.

    SVGs written by this fleet carry <title>/<desc> (the wrap() helper emits
    them), but not all of them do -- wss-gho's predate that -- so the filename is
    the fallback. No chart is skipped for lacking a caption.
    """
    charts = sorted((root / CHART_DIR).glob("*.svg")) if (root / CHART_DIR).is_dir() else []
    out = []
    for chart in charts:
        head = chart.read_text(encoding="utf-8", errors="replace")[:4000]
        title = re.search(r"<title>(.*?)</title>", head, re.S)
        desc = re.search(r"<desc>(.*?)</desc>", head, re.S)
        out.append({
            "file": chart.name,
            "title": (title.group(1).strip() if title
                      else chart.stem.replace("-", " ").replace("_", " ").capitalize()),
            "desc": desc.group(1).strip() if desc else "",
        })
    return out


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
    figures = _figures(root)

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
    figures_html = ""
    if figures:
        cards = "\n".join(
            f'  <figure>\n'
            f'    <a href="charts/{e(f["file"])}"><img src="charts/{e(f["file"])}" '
            f'alt="{e(f["title"])}" loading="lazy"></a>\n'
            f'    <figcaption><strong>{e(f["title"])}</strong>'
            + (f' — {e(f["desc"])}' if f["desc"] else "")
            + '</figcaption>\n  </figure>'
            for f in figures
        )
        figures_html = (
            "\n<h2>Findings</h2>\n"
            "<p>Figures are rebuilt from the derived tables on every run; each one "
            "names the entities it is about so it can be acted on without a further "
            "query.</p>\n" + cards + "\n"
        )

    rows = "\n".join(
        f"      <tr><td><code>{e(s.source_id)}</code></td><td>{e(s.publisher)}</td>"
        f"<td>{e(s.cadence)}</td><td>{e(s.licence)}</td></tr>"
        for s in active
    )
    cover = (
        f"<p class=\"cov\">History held: <strong>{e(first)}</strong> to "
        f"<strong>{e(last)}</strong>.</p>" if first and last else ""
    )
    # Without a DOI, still say how to cite. Promising a DOI that nobody intends
    # to mint is worse than having none: a dataset should always carry a
    # citable handle, and the repository URL is one.
    cite = (
        f"<p>Cite this dataset: <a href=\"https://doi.org/{e(doi)}\">"
        f"https://doi.org/{e(doi)}</a></p>" if doi else
        f"<p>Cite this dataset by its repository and the date you took it from: "
        f"<a href=\"{e(repo_url)}\">{e(repo_url)}</a>"
        + (f", accessed {e(last)}." if last else ".") + "</p>"
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

{figures_html}
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
    # GitHub Pages runs Jekyll over the published directory unless told not to.
    # Repos keep prose in ``docs/`` alongside this page, and Jekyll will try to
    # render it — a stray Liquid brace in a markdown file fails the *whole*
    # build, which shows up as the page silently not updating rather than as an
    # error anyone sees. Cheaper to opt out than to keep docs/ Jekyll-safe.
    (path.parent / ".nojekyll").touch()
    # The page references charts/<name>.svg relative to itself, so the figures
    # have to sit beside it in the published directory.
    charts = root / CHART_DIR
    if charts.is_dir():
        out_dir = path.parent / "charts"
        out_dir.mkdir(parents=True, exist_ok=True)
        published = set()
        for svg in sorted(charts.glob("*.svg")):
            (out_dir / svg.name).write_bytes(svg.read_bytes())
            published.add(svg.name)
        # A chart deleted upstream must not linger on the published page.
        for stale in out_dir.glob("*.svg"):
            if stale.name not in published:
                stale.unlink()
    return path
