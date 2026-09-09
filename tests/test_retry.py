"""Retry strategy: a throttle is not a failure, and Retry-After beats guessing.

These tests exist because the fleet had none, and the gap cost peeringdb three
sources: under a 2/4/8-second ladder every retry landed inside the same window
that had just refused it, so eight refusals in one evening read as a dying
source. Nothing in the suite would have noticed.
"""

from __future__ import annotations

import pytest
import requests
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from wss import capture
from wss.registry import Endpoint


class FakeResponse:
    def __init__(self, status, headers=None, body=b"ok"):
        self.status_code = status
        self.headers = headers or {}
        self.content = body


class FakeSession:
    """Answers a scripted list of responses and records the request count."""

    def __init__(self, responses):
        self.headers: dict[str, str] = {}
        self._responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def slept(monkeypatch):
    """Capture every sleep instead of taking it."""
    waits: list[float] = []
    monkeypatch.setattr(capture.time, "sleep", waits.append)
    return waits


def make_fetcher(session, **kw):
    kw.setdefault("throttle_base", 30.0)
    return capture.Fetcher("https://example.com/contact", session=session,
                           retry_base=2.0, **kw)


ENDPOINT = Endpoint(url="https://example.com/api", delay_seconds=0)


# --- Retry-After ----------------------------------------------------------

def test_retry_after_seconds_beats_the_ladder(slept):
    session = FakeSession([FakeResponse(429, {"Retry-After": "45"}),
                           FakeResponse(200)])
    result = make_fetcher(session).fetch(ENDPOINT)
    assert result.status == 200
    # 45, as asked -- not the 30 our own ladder would have guessed.
    assert slept == [45.0]


def test_retry_after_http_date_is_understood(slept):
    when = datetime.now(timezone.utc) + timedelta(seconds=90)
    session = FakeSession([FakeResponse(503, {"Retry-After": format_datetime(when)}),
                           FakeResponse(200)])
    make_fetcher(session).fetch(ENDPOINT)
    assert 80 <= slept[0] <= 95


def test_retry_after_is_capped():
    """A publisher may say 'in six hours'. A scheduled run may not obey."""
    assert capture.parse_retry_after("21600") == 21600.0
    session = FakeSession([FakeResponse(429, {"Retry-After": "21600"}),
                           FakeResponse(200)])
    waits: list[float] = []
    f = make_fetcher(session)
    f._retry_sleep = lambda s, t: waits.append(min(s, capture.RETRY_AFTER_CAP)) or True
    f.fetch(ENDPOINT)
    assert waits == [capture.RETRY_AFTER_CAP]


@pytest.mark.parametrize("value", ["", "  ", "soon", "not-a-date"])
def test_unparseable_retry_after_falls_back(value, slept):
    session = FakeSession([FakeResponse(429, {"Retry-After": value}),
                           FakeResponse(200)])
    make_fetcher(session).fetch(ENDPOINT)
    assert slept == [30.0]          # the throttle ladder, not zero


# --- the two ladders ------------------------------------------------------

def test_throttle_waits_far_longer_than_a_500(slept):
    session = FakeSession([FakeResponse(429)])
    with pytest.raises(capture.FetchError) as exc:
        make_fetcher(session).fetch(ENDPOINT)
    assert slept == [30.0, 60.0, 120.0]
    assert exc.value.args[0] == "throttled_status_429"

    slept.clear()
    session = FakeSession([FakeResponse(500)])
    with pytest.raises(capture.FetchError) as exc:
        make_fetcher(session).fetch(ENDPOINT)
    assert slept == [2.0, 4.0, 8.0]
    assert exc.value.args[0] == "retries_exhausted_status_500"


def test_throttle_budget_stops_the_run_early(slept):
    """The per-wait cap alone still lets a fleet-wide throttle sleep for hours."""
    session = FakeSession([FakeResponse(429)])
    with pytest.raises(capture.FetchError) as exc:
        make_fetcher(session, throttle_budget=100.0).fetch(ENDPOINT)
    assert slept == [30.0, 60.0]     # 120 would have crossed 100 remaining
    assert exc.value.args[0].endswith("_budget_exhausted")


def test_budget_is_shared_across_endpoints(slept):
    """One Fetcher serves a whole run, so the budget has to be per-run."""
    fetcher = make_fetcher(FakeSession([FakeResponse(429)]), throttle_budget=100.0)
    for _ in range(2):
        with pytest.raises(capture.FetchError):
            fetcher.fetch(ENDPOINT)
    assert slept == [30.0, 60.0]     # the second endpoint gets nothing left


def test_ordinary_backoff_never_draws_on_the_budget(slept):
    """A 5xx ladder is seconds; rationing it would only make outages worse."""
    fetcher = make_fetcher(FakeSession([FakeResponse(500)]), throttle_budget=1.0)
    for _ in range(3):
        with pytest.raises(capture.FetchError):
            fetcher.fetch(ENDPOINT)
    assert slept == [2.0, 4.0, 8.0] * 3


def test_retry_base_zero_means_never_sleep(slept):
    """The switch the test suite sets has to reach the throttle ladder too."""
    f = capture.Fetcher("https://example.com/contact",
                        session=FakeSession([FakeResponse(429)]), retry_base=0)
    assert f.throttle_base == 0.0
    with pytest.raises(capture.FetchError):
        f.fetch(ENDPOINT)
    assert slept == []


def test_connection_error_still_retries_on_the_short_ladder(slept):
    session = FakeSession([requests.ConnectionError("boom"), FakeResponse(200)])
    assert make_fetcher(session).fetch(ENDPOINT).status == 200
    assert slept == [2.0]
