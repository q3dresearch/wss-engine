from __future__ import annotations

from wss import manifest, sources

from .conftest import write_source_yaml


def test_sources_lists_every_url_and_licence(tmp_path):
    write_source_yaml(tmp_path, "test.alpha.one", "https://example.test/one.json")
    write_source_yaml(tmp_path, "test.beta.two", "https://example.test/two.json", status="auto_disabled")

    text = sources.build(tmp_path)

    # every endpoint is reachable as a link, not just a source_id
    assert "<https://example.test/one.json>" in text
    assert "<https://example.test/two.json>" in text
    # the terms a reuser needs are on the page
    assert "test data" in text
    assert "Test Fixtures Inc" in text
    # status is carried through so a dead source is visibly dead
    assert "auto_disabled" in text
    # a source that destroys its own history says so
    assert "destroys its own history" in text


def test_sources_reports_the_last_stored_capture(tmp_path):
    write_source_yaml(tmp_path, "test.alpha.one", "https://example.test/one.json")
    manifest.append_row(
        tmp_path,
        {
            "source_id": "test.alpha.one",
            "url": "https://example.test/one.json",
            "fetched_at": "2026-09-02T00:00:00Z",
            "http_status": "200",
            "content_type": "application/json",
            "content_length": "42",
            "content_sha256": "abcdef0123456789",
            "etag": "",
            "last_modified": "",
            "outcome": "changed",
            "raw_ref": "raw/test.alpha.one/2026/09/x.json",
            "reason": "",
            "warnings": "",
        },
    )

    text = sources.build(tmp_path)

    assert "raw/test.alpha.one/2026/09/x.json" in text
    assert "abcdef012345" in text          # sha is shown, truncated
    assert "2026-09-02T00:00:00Z" in text


def test_sources_says_so_when_nothing_captured_yet(tmp_path):
    write_source_yaml(tmp_path, "test.alpha.one", "https://example.test/one.json")
    assert "none yet" in sources.build(tmp_path)


def test_write_creates_the_file(tmp_path):
    write_source_yaml(tmp_path, "test.alpha.one", "https://example.test/one.json")
    path = sources.write(tmp_path)
    assert path.name == "SOURCES.md"
    assert path.read_text(encoding="utf-8").startswith("# Sources")
