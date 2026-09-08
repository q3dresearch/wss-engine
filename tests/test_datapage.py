"""The generated page must be machine-readable and must not invent metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from wss import datapage

CFF = """\
cff-version: 1.2.0
type: dataset
title: "Test History (wss-test)"
abstract: >-
  Captures a queue that the publisher overwrites nightly.
authors:
  - name: "someone"
    website: "https://github.com/someone"
license: CC-BY-4.0
keywords:
  - open-data
doi: 10.5281/zenodo.1234567
"""

SRC = """\
source_id: demo.agency.queue
status: active
cadence: weekly
schema_id: demo_v1
publisher: Demo Agency
publisher_tier: primary
destroys_own_history: true
licence: CC-BY-4.0
personal_data: none
storage: git
endpoints:
  - url: https://example.gov/queue.json
    delay_seconds: 0
gates:
  expect_status: 200
  min_bytes: 10
  content_type_any: [json]
  must_not_contain: ["Access Denied"]
"""


def _repo(tmp_path: Path, cff: str = CFF) -> Path:
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "demo.agency.queue.yml").write_text(SRC, encoding="utf-8")
    if cff:
        (tmp_path / "CITATION.cff").write_text(cff, encoding="utf-8")
    return tmp_path


def _ld(page: str) -> dict:
    m = re.search(r'<script type="application/ld\+json">\n(.*?)\n</script>', page, re.S)
    assert m, "no JSON-LD block on the page"
    return json.loads(m.group(1))


def test_emits_parsable_dataset_markup(tmp_path):
    ld = _ld(datapage.build(_repo(tmp_path), "https://github.com/someone/wss-test"))
    assert ld["@type"] == "Dataset"
    assert ld["name"] == "Test History (wss-test)"
    assert ld["description"].startswith("Captures a queue")
    assert ld["identifier"] == "https://doi.org/10.5281/zenodo.1234567"
    assert ld["license"] == "CC-BY-4.0"


def test_creator_never_carries_an_email(tmp_path):
    cff = CFF.replace('website: "https://github.com/someone"',
                      'email: "private@example.com"')
    page = datapage.build(_repo(tmp_path, cff), "https://github.com/someone/wss-test")
    assert "private@example.com" not in page
    assert _ld(page)["creator"] == [{"@type": "Person", "name": "someone"}]


def test_placeholder_abstract_is_not_published(tmp_path):
    """The scaffold ships a TODO abstract; it must never reach the markup."""
    cff = CFF.replace("Captures a queue that the publisher overwrites nightly.",
                      "TODO: one paragraph on what this dataset captures.")
    ld = _ld(datapage.build(_repo(tmp_path, cff), "https://github.com/x/y"))
    assert "TODO" not in ld["description"]
    assert "do not keep their own history" in ld["description"]


def test_missing_fields_are_omitted_not_guessed(tmp_path):
    ld = _ld(datapage.build(_repo(tmp_path, cff=""), "https://github.com/x/y"))
    assert "identifier" not in ld          # no CITATION.cff, so no DOI
    assert "creator" not in ld             # rather than an invented author
    assert "temporalCoverage" not in ld    # no manifest rows yet


def test_only_active_sources_are_listed(tmp_path):
    root = _repo(tmp_path)
    (root / "registry" / "gone.agency.queue.yml").write_text(
        SRC.replace("demo.agency.queue", "gone.agency.queue").replace(
            "status: active", "status: retired"), encoding="utf-8")
    page = datapage.build(root, "https://github.com/x/y")
    assert "demo.agency.queue" in page
    assert "gone.agency.queue" not in page


def test_html_is_escaped(tmp_path):
    root = _repo(tmp_path)
    (root / "registry" / "demo.agency.queue.yml").write_text(
        SRC.replace("Demo Agency", "A & B <script>"), encoding="utf-8")
    page = datapage.build(root, "https://github.com/x/y")
    assert "A &amp; B &lt;script&gt;" in page


def test_write_creates_the_docs_directory(tmp_path):
    path = datapage.write(_repo(tmp_path), "https://github.com/x/y")
    assert path == tmp_path / "docs" / "index.html"
    assert path.exists()


def test_write_opts_out_of_jekyll(tmp_path):
    """Prose in docs/ must not be able to fail the Pages build."""
    _repo(tmp_path)
    path = datapage.write(tmp_path, "https://github.com/o/r")
    assert (path.parent / ".nojekyll").exists()


def test_figures_are_published_beside_the_page(tmp_path):
    """A landing page that cites the data but shows none of it is a page nobody
    reads twice. 45 charts across the fleet were reachable only by cloning."""
    root = _repo(tmp_path)
    charts = root / "examples" / "charts"
    charts.mkdir(parents=True)
    (charts / "with-meta.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><title>Volume against rate</title>'
        '<desc>Inspections fell; the OAI share rose.</desc></svg>')
    (charts / "no-meta.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

    path = datapage.write(root, repo_url="https://example.com/r")
    html = path.read_text()

    assert "<h2>Findings</h2>" in html
    assert 'src="charts/with-meta.svg"' in html
    assert "Volume against rate" in html
    assert "Inspections fell; the OAI share rose." in html
    # a chart lacking <title>/<desc> is still published, captioned from its name
    assert 'src="charts/no-meta.svg"' in html
    assert "No meta" in html
    # and the files land beside the page, since the src is relative
    assert (path.parent / "charts" / "with-meta.svg").is_file()
    assert (path.parent / "charts" / "no-meta.svg").is_file()

    # a chart deleted upstream must not linger on the published page
    (charts / "no-meta.svg").unlink()
    datapage.write(root, repo_url="https://example.com/r")
    assert not (path.parent / "charts" / "no-meta.svg").exists()
