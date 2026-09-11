"""Publishers that mint a new address for every version.

gov.uk does it for every publication: the UK sponsor register sits at
/media/<objectid>/SP_..._-_<date>.csv and BOTH parts move on every upload --
the id is an ObjectID whose first four bytes are the upload time, so
6aa3bcb05f6e942efe37f156 decodes to 2026-09-11 08:32:48 UTC, the morning it was
published. No date token can construct that.

`url_from` points the registry at the stable address and says where inside it
the real one lives. The manifest keeps the stable url as the endpoint's
identity, so a daily-minted address still produces ONE continuous history
rather than a new series every upload.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from wss import capture, cli, manifest
from tests.conftest import write_source_yaml
from tests.fixture_server import FixtureServer

DATA = "name,town,rating\nAcme Ltd,London,A\nBeta Ltd,Leeds,B\n" + "x" * 200


CSV_GATES = textwrap.dedent("""\
    gates:
      expect_status: 200
      min_bytes: 10
      content_type_any: [csv]
      must_not_contain: ["Access Denied"]
    """)


def write_indirect_source(root, discovery_url, source_id="gov.sponsors.workers"):
    """The gates apply to the DATA response, not the pointer -- csv, not json."""
    path = write_source_yaml(root, source_id, discovery_url, gates=CSV_GATES)
    text = path.read_text().replace(
        f"- url: {discovery_url}",
        f"- url: {discovery_url}\n    url_from: details.attachments[0].url")
    assert "url_from" in text, "fixture yaml shape changed"
    path.write_text(text)
    return path


@pytest.mark.usefixtures("contact_env")
def test_the_data_is_captured_not_the_pointer(tmp_path):
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/api", json.dumps(
            {"details": {"attachments": [
                {"url": server.url + "/media/aaa/Register_2026-09-11.csv"}]}}))
        server.set("/media/aaa/Register_2026-09-11.csv", DATA, content_type="text/csv")
        write_indirect_source(tmp_path, server.url + "/api")
        assert cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"]) == 0
        paths = [p for p, _ in server.requests]

    # Both were fetched, and in order.
    assert "/api" in paths and "/media/aaa/Register_2026-09-11.csv" in paths
    assert paths.index("/api") < paths.index("/media/aaa/Register_2026-09-11.csv")

    rows = manifest.iter_rows(tmp_path, "gov.sponsors.workers")
    row = list(rows)[0]
    assert row["outcome"] == "first_capture"
    # The identity stays the STABLE url, so tomorrow's minted address does not
    # start a second series...
    assert row["url"].endswith("/api")
    # ...but which file we actually took is recorded.
    assert "discovered=Register_2026-09-11.csv" in row["warnings"]
    # And the ARCHIVE holds the data, not the pointer.
    archived = (tmp_path / row["raw_ref"]).read_bytes()
    import gzip
    if row["raw_ref"].endswith(".gz"):
        archived = gzip.decompress(archived)
    assert b"Acme Ltd" in archived and b"attachments" not in archived


@pytest.mark.usefixtures("contact_env")
def test_a_moved_address_is_still_one_series(tmp_path):
    """The whole point: a new url every day must not fork the history."""
    with FixtureServer() as server:
        server.allow_all_robots()
        write_indirect_source(tmp_path, server.url + "/api")
        for day, body in (("11", DATA), ("12", DATA + "Gamma Ltd,Hull,A\n")):
            # A fresh address every upload, exactly as gov.uk mints them.
            server.set("/api", json.dumps({"details": {"attachments": [
                {"url": f"{server.url}/media/x{day}/Register_2026-09-{day}.csv"}]}}))
            server.set(f"/media/x{day}/Register_2026-09-{day}.csv", body,
                       content_type="text/csv")
            assert cli.main(["--root", str(tmp_path), "capture",
                             "--cadence", "weekly"]) == 0

    rows = list(manifest.iter_rows(tmp_path, "gov.sponsors.workers"))
    assert len(rows) == 2, rows
    assert len({r["url"] for r in rows}) == 1, "a moved address forked the series"
    assert [r["outcome"] for r in rows] == ["first_capture", "changed"]
    # Each row still records WHICH file it actually took.
    assert "Register_2026-09-11.csv" in rows[0]["warnings"]
    assert "Register_2026-09-12.csv" in rows[1]["warnings"]


@pytest.mark.usefixtures("contact_env")
def test_a_broken_pointer_is_an_error_not_a_capture(tmp_path):
    with FixtureServer() as server:
        server.allow_all_robots()
        server.set("/api", json.dumps({"details": {"attachments": []}}))
        write_indirect_source(tmp_path, server.url + "/api")
        cli.main(["--root", str(tmp_path), "capture", "--cadence", "weekly"])

    row = list(manifest.iter_rows(tmp_path, "gov.sponsors.workers"))[0]
    assert row["outcome"] == "error"
    assert row["reason"].startswith("url_from_missing")
    assert not row["raw_ref"], "a broken pointer must not archive anything"


def test_follow_refuses_what_it_cannot_trust():
    """Following an address out of a response is a request someone else chose."""
    good = b'{"details":{"attachments":[{"url":"https://a.test/f.csv"}]}}'
    assert capture.follow_url_from(good, "details.attachments[0].url", "s") \
        == "https://a.test/f.csv"
    for body, path, why in (
            (b"not json", "a.b", "not json"),
            (b'{"a":{"b":"/relative"}}', "a.b", "relative url"),
            (b'{"a":{"b":5}}', "a.b", "not a string"),
            (good, "details.missing[0].url", "absent path")):
        with pytest.raises(capture.FetchError):
            capture.follow_url_from(body, path, "s")
