"""HTTP failure classification and in-cycle retry.

The split between transient and structural decides whether a broken source is
probed hourly or parked for a person, so getting a status code on the wrong side
of it is expensive in both directions: a parked 503 is a source switched off for
no reason, and a retried 404 is the arbeitsagentur bug this replaced.
"""

from __future__ import annotations

import pytest
import requests

from watcher.fetchers import base


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="",
                 content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


class FakeSession:
    """Replays a scripted sequence of responses/exceptions, counting calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, timeout=None, **kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    post = get


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Backoff is real time; the tests should not pay for it."""
    monkeypatch.setattr(base.time, "sleep", lambda _: None)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_server_errors_and_rate_limits_are_transient(status):
    sess = FakeSession(FakeResponse(status_code=status))
    with pytest.raises(base.TransientError) as excinfo:
        base.get_json(sess, "http://x/api", 5)
    assert excinfo.value.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_errors_are_structural(status):
    sess = FakeSession(FakeResponse(status_code=status))
    with pytest.raises(base.StructuralError) as excinfo:
        base.get_json(sess, "http://x/api", 5)
    assert excinfo.value.status_code == status


def test_429_is_not_treated_as_structural():
    """The one 4xx that means "later" rather than "wrong"."""
    sess = FakeSession(FakeResponse(status_code=429))
    with pytest.raises(base.TransientError):
        base.get_json(sess, "http://x/api", 5)


def test_html_body_on_200_is_structural():
    """A login wall or an error page served with a 200 is not a retry case."""
    sess = FakeSession(FakeResponse(text="<html>nope</html>",
                                    content_type="text/html"))
    with pytest.raises(base.StructuralError):
        base.get_json(sess, "http://x/api", 5)


def test_malformed_json_is_structural():
    sess = FakeSession(FakeResponse(payload=None))
    with pytest.raises(base.StructuralError):
        base.get_json(sess, "http://x/api", 5)


@pytest.mark.parametrize("exc", [
    requests.Timeout("timed out"),
    requests.ConnectionError("connection reset"),
])
def test_network_failures_are_transient(exc):
    sess = FakeSession(exc)
    with pytest.raises(base.TransientError):
        base.get_json(sess, "http://x/api", 5)


def test_unknown_exception_defaults_to_transient():
    """Safer default: a wasted probe beats a source switched off by surprise."""
    assert isinstance(base.classify(ValueError("who knows")), base.TransientError)


# --------------------------------------------------------------------------
# retry behaviour
# --------------------------------------------------------------------------

def test_transient_failure_is_retried_then_succeeds():
    sess = FakeSession(
        FakeResponse(status_code=503),
        FakeResponse(status_code=503),
        FakeResponse(payload={"ok": True}),
    )
    assert base.get_json(sess, "http://x/api", 5) == {"ok": True}
    assert sess.calls == 3


def test_transient_failure_gives_up_after_the_attempt_budget():
    sess = FakeSession(FakeResponse(status_code=503))
    with pytest.raises(base.TransientError):
        base.get_json(sess, "http://x/api", 5)
    assert sess.calls == base.RETRY_ATTEMPTS


def test_structural_failure_is_not_retried():
    """Repeating a 404 only delays the report — it cannot change the answer."""
    sess = FakeSession(FakeResponse(status_code=404))
    with pytest.raises(base.StructuralError):
        base.get_json(sess, "http://x/api", 5)
    assert sess.calls == 1


def test_success_makes_exactly_one_request():
    sess = FakeSession(FakeResponse(payload={"ok": True}))
    base.get_json(sess, "http://x/api", 5)
    assert sess.calls == 1


def test_get_text_classifies_and_retries_too():
    sess = FakeSession(
        FakeResponse(status_code=502),
        FakeResponse(text="hello", content_type="text/html"),
    )
    assert base.get_text(sess, "http://x/page", 5) == "hello"
    assert sess.calls == 2


def test_get_text_does_not_retry_structural():
    sess = FakeSession(FakeResponse(status_code=403))
    with pytest.raises(base.StructuralError):
        base.get_text(sess, "http://x/page", 5)
    assert sess.calls == 1
