from __future__ import annotations

from wss.gates import run_gates

GOOD = dict(
    status_code=200,
    content_type="application/json; charset=utf-8",
    body=b'{"downloads": 42, "padding": "xxxxxxxxxxxxxxxx"}',
)
GATES = {
    "expect_status": 200,
    "min_bytes": 10,
    "content_type_any": ["json"],
    "must_contain": ["downloads"],
    "must_not_contain": ["Access Denied"],
    "max_shrink_pct": 50,
}


def test_all_gates_pass():
    assert run_gates(**GOOD, gates=GATES, prev_content_length=50).ok


def test_bad_status():
    result = run_gates(**{**GOOD, "status_code": 503}, gates=GATES)
    assert not result.ok and result.reason == "bad_status_503"


def test_expect_status_list_allows_member_death():
    # A cohort member that dies starts 404ing; [200, 404] archives the death
    # as evidence instead of failing the run forever.
    gates = {"expect_status": [200, 404], "content_type_any": ["json"]}
    result = run_gates(status_code=404, content_type="application/json", body=b'{"error":"model gone"}', gates=gates)
    assert result.ok


def test_min_bytes():
    result = run_gates(**{**GOOD, "body": b"{}"}, gates=GATES)
    assert not result.ok and result.reason == "too_small_2_bytes"


def test_content_type():
    result = run_gates(**{**GOOD, "content_type": "text/html"}, gates=GATES)
    assert not result.ok and result.reason == "content_type_text/html"


def test_must_contain():
    result = run_gates(**{**GOOD, "body": b'{"nothing_useful": "xxxxxxxxxxxx"}'}, gates=GATES)
    assert not result.ok and result.reason == "missing_required_text"


def test_must_not_contain():
    body = b'{"downloads": 0, "err": "Access Denied"}'
    result = run_gates(**{**GOOD, "body": body}, gates=GATES)
    assert not result.ok and result.reason == "contains_forbidden_text"


def test_max_shrink_pct():
    result = run_gates(**GOOD, gates=GATES, prev_content_length=10_000)
    assert not result.ok and result.reason.startswith("shrunk_")
    # no baseline → shrink gate cannot fire
    assert run_gates(**GOOD, gates=GATES, prev_content_length=None).ok


def test_defaults_expect_200():
    assert run_gates(status_code=200, content_type="", body=b"x", gates={}).ok
    assert not run_gates(status_code=404, content_type="", body=b"x", gates={}).ok


def test_missing_content_type_is_declarable():
    """A publisher that sends no Content-Type must still be capturable.

    CloudFront serves the WDPA monthly zip with Content-Length and
    Last-Modified and nothing else. Before `none` was accepted there was no
    way to declare that: content_type_any is required and must be a non-empty
    list of non-empty strings, so every possible value failed.
    """
    body = b"PK\x03\x04" + b"\0" * 100
    # Without the declaration it is still a failure -- the gate is not bypassed.
    strict = {"content_type_any": ["zip"]}
    result = run_gates(status_code=200, content_type="", body=body, gates=strict)
    assert not result.ok and result.reason == "content_type_missing"

    # With it, the missing header is accepted.
    lenient = {"content_type_any": ["none"]}
    assert run_gates(status_code=200, content_type="", body=body, gates=lenient).ok

    # And `none` does NOT become a wildcard: a real content-type still has to
    # match one of the other tokens.
    assert not run_gates(status_code=200, content_type="text/html",
                         body=body, gates=lenient).ok
    assert run_gates(status_code=200, content_type="application/zip", body=body,
                     gates={"content_type_any": ["none", "zip"]}).ok
