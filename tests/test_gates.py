from __future__ import annotations

from snapshotter.gates import run_gates

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
