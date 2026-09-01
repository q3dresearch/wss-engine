"""Recon runs against a local fixture server — no network."""

from __future__ import annotations

import json

import pytest

from wss import cli
from tests.fixture_server import FixtureServer


def run(server: FixtureServer, path: str, capsys, source_id: str = "demo.web.thing") -> str:
    assert cli.main(["explore", server.url + path, "--source-id", source_id]) == 0
    return capsys.readouterr().out


@pytest.mark.usefixtures("contact_env")
def test_json_api_maps_onto_the_observation_schema(capsys):
    payload = {
        "results": [
            {"ticker": "AAA", "as_of": "2026-08-31", "weight": 5.25, "shares": 1000, "zip": 90210},
            {"ticker": "BBB", "as_of": "2026-08-31", "weight": 1.5, "shares": 42, "zip": 10001},
        ]
    }
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/api", json.dumps(payload))
        out = run(server, "/api", capsys)

    assert "treated as json" in out
    assert "records      : 2" in out
    assert "entity_id  ← ticker" in out
    assert "as_of" in out.split("observed_at ←")[1].splitlines()[0]
    metrics = out.split("metric/value ←")[1].splitlines()[0]
    assert "weight" in metrics and "shares" in metrics
    assert "zip" not in metrics  # id-ish numbers are not metrics
    # gates are inferred from what was actually seen
    assert "content_type_any: [json]" in out
    assert "must_contain:" in out
    assert "status: paused" in out  # never suggests capturing straight away


@pytest.mark.usefixtures("contact_env")
def test_html_surfaces_the_api_behind_the_page(capsys):
    html = """
    <html><head>
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
    </head><body>
      <p>Holdings are updated daily.</p>
      <time datetime="2026-08-30">Aug 30</time>
      <script>var cfg = {"endpoint": "/api/v2/holdings.json?fund=X"};</script>
    </body></html>
    """
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/page", html, content_type="text/html")
        out = run(server, "/page", capsys)

    assert "treated as html" in out
    assert "/api/v2/holdings.json?fund=X" in out
    assert "feed (application/rss+xml)" in out
    assert "candidate observed_at" in out


@pytest.mark.usefixtures("contact_env")
def test_javascript_rendered_page_is_flagged(capsys):
    html = "<html><body><div id=root></div>" + "<script src='/a.js'></script>" * 5 + "</body></html>"
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/spa", html, content_type="text/html")
        out = run(server, "/spa", capsys)

    assert "likely JavaScript-rendered" in out
    assert "the engine does not run JS" in out


@pytest.mark.usefixtures("contact_env")
def test_post_form_is_flagged(capsys):
    html = "<html><body><form method='post' action='/search'><input name=q></form>" + "x" * 600 + "</body></html>"
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/form", html, content_type="text/html")
        out = run(server, "/form", capsys)

    assert "only issues GET" in out


@pytest.mark.usefixtures("contact_env")
def test_atom_feed_finds_the_record_element_not_the_leaves(capsys):
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <link rel="self" href="/f"/>
      <entry>
        <id>0000320193-26-000001</id>
        <link href="/doc1"/>
        <updated>2026-08-01T00:00:00-04:00</updated>
        <content><filing-date>2026-08-01</filing-date><film-number>261234</film-number></content>
      </entry>
      <entry>
        <id>0000320193-26-000002</id>
        <link href="/doc2"/>
        <updated>2026-05-01T00:00:00-04:00</updated>
        <content><filing-date>2026-05-01</filing-date><film-number>261111</film-number></content>
      </entry>
    </feed>
    """
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/atom", atom, content_type="application/atom+xml")
        out = run(server, "/atom", capsys)

    # <link> repeats more often than <entry> but carries no fields
    assert "repeating element <entry>" in out
    assert "filing-date" in out.split("observed_at ←")[1].splitlines()[0]
    assert "id" in out.split("entity_id  ←")[1].splitlines()[0]


@pytest.mark.usefixtures("contact_env")
def test_document_feed_suggests_archive_schema(capsys):
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>a</id><title>Opinion A</title><updated>2026-08-01</updated></entry>
      <entry><id>b</id><title>Opinion B</title><updated>2026-08-02</updated></entry>
    </feed>
    """
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/opinions", atom, content_type="application/xml")
        out = run(server, "/opinions", capsys)

    assert "no numeric fields" in out
    assert "archive.v1" in out


@pytest.mark.usefixtures("contact_env")
def test_robots_disallow_stops_recon(capsys):
    with FixtureServer() as server:
        server.set("/robots.txt", "User-agent: *\nDisallow: /private\n", content_type="text/plain")
        server.set("/private", "{}")
        assert cli.main(["explore", server.url + "/private"]) == 1
    out = capsys.readouterr().out
    assert "DISALLOWS" in out and "Stop here" in out


@pytest.mark.usefixtures("contact_env")
def test_csv_header_becomes_field_candidates(capsys):
    body = "Ticker,Name,As Of,Weight (%)\nAAA,Alpha,2026-08-31,5.25\n"
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/holdings.csv", body, content_type="text/csv")
        out = run(server, "/holdings.csv", capsys)

    assert "treated as csv" in out
    assert "CSV header: Ticker, Name, As Of, Weight (%)" in out
    assert "content_type_any: [csv]" in out
