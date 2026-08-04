"""Shared helpers for fetchers: HTTP session, location parsing, remote detection.

Failures are typed, because the two kinds want opposite handling. A 503, a
timeout, or a dropped connection is *transient*: the source is fine and will
answer the next probe, so retrying is the whole cure. A 404 or a 401 is
*structural*: an endpoint moved or a key was withdrawn, and no amount of
retrying brings it back — that needs a person to change the fetcher. Collapsing
both into one `RuntimeError` is what left the arbeitsagentur source probing a
dead v4 endpoint once an hour, silently, indefinitely.

`get_json`/`get_text` also absorb the short-lived case themselves, retrying a
transient failure a few times with exponential backoff before it ever reaches
source health. A single blip should not spend one of a source's three lives.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

import requests

log = logging.getLogger("watcher.fetch")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_REMOTE = re.compile(
    r"\b(fully[- ]remote|remote[- ]first|100%\s*remote|work from home|homeoffice|"
    r"home[- ]office|telearbeit|remote)\b", re.I
)

# "Hybrid" and "remote-friendly" postings still require presence, so the remote
# flag must not be set by the word "remote" alone when hybrid is also stated.
_HYBRID = re.compile(r"\bhybrid\b|\bteilweise vor ort\b", re.I)


class FetchError(RuntimeError):
    """Base for every fetch failure, carrying the status code when there is one."""

    status_code: int | None = None

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransientError(FetchError):
    """The source is momentarily unavailable: 5xx, 429, timeout, connection drop.

    Worth retrying, both inside the cycle and on the health cooldown afterwards.
    """


class StructuralError(FetchError):
    """The request itself is wrong: 4xx other than 429, or a non-JSON body.

    An endpoint that moved, a key that was withdrawn, a board token that is no
    longer valid. Retrying reproduces it exactly, so a source that fails this
    way is parked for a human rather than probed forever.
    """


# 429 is a 4xx but means "later", not "wrong" — the one 4xx that is transient.
_RATE_LIMITED = 429

# Three attempts total. Enough to ride out a blip or a single rate-limit, few
# enough that a genuinely dead source still fails the cycle promptly instead of
# holding the poll open while every source exhausts a long ladder.
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1.0


def classify(exc: Exception) -> FetchError:
    """Map any fetch exception onto the transient/structural split.

    Anything unrecognised is treated as transient. That is the safe default: the
    cost of retrying a structural failure is a wasted probe, while the cost of
    parking a transient one is a source switched off for a person to notice.
    """
    if isinstance(exc, FetchError):
        return exc
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return TransientError(f"{type(exc).__name__}: {exc}")
    return TransientError(f"{type(exc).__name__}: {exc}")


def _with_retries(call, url: str, attempts: int = RETRY_ATTEMPTS):
    """Run `call`, retrying transient failures with exponential backoff.

    Structural failures return immediately — repeating a 404 only delays the
    report. Jitter keeps several sources that broke at the same moment from
    retrying in lockstep.
    """
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — classified immediately below
            error = classify(exc)
            if isinstance(error, StructuralError) or attempt == attempts:
                raise error from exc
            delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, delay / 2)
            log.debug("%s failed (%s) — attempt %d/%d, retrying in %.1fs",
                      url, error, attempt, attempts, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    })
    return s


def get_json(sess: requests.Session, url: str, timeout: int, **kwargs: Any) -> Any:
    """GET and decode JSON, failing loudly on anything that is not JSON.

    Without this, a wrong board token returns a 404 HTML page which decodes to
    a parse error deep inside a fetcher — or worse, an empty list that looks
    exactly like "this company has no openings right now". A misconfigured
    source must be obviously broken, not quietly silent.
    """
    return _with_retries(
        lambda: _decode(sess.get(url, timeout=timeout, **kwargs), url), url)


def post_json(sess: requests.Session, url: str, timeout: int, **kwargs: Any) -> Any:
    return _with_retries(
        lambda: _decode(sess.post(url, timeout=timeout, **kwargs), url), url)


def _raise_for_status(response: requests.Response, url: str) -> None:
    status = response.status_code
    if status < 400:
        return
    message = f"HTTP {status} from {url}"
    if status >= 500 or status == _RATE_LIMITED:
        raise TransientError(message, status)
    raise StructuralError(message, status)


def _decode(response: requests.Response, url: str) -> Any:
    """Decode JSON, failing loudly on anything that is not JSON.

    Without this, a wrong board token returns a 404 HTML page which decodes to
    a parse error deep inside a fetcher — or worse, an empty list that looks
    exactly like "this company has no openings right now". A misconfigured
    source must be obviously broken, not quietly silent.
    """
    _raise_for_status(response, url)
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        snippet = response.text[:120].replace("\n", " ")
        # A 200 that is not JSON means the endpoint is serving something else
        # now — a login wall, an HTML error page. That is structural.
        raise StructuralError(
            f"expected JSON from {url}, got {content_type or 'no content-type'}: {snippet}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise StructuralError(f"malformed JSON from {url}: {exc}") from exc


def get_text(sess: requests.Session, url: str, timeout: int) -> str:
    def call() -> str:
        response = sess.get(url, timeout=timeout)
        _raise_for_status(response, url)
        return response.text

    return _with_retries(call, url)


def country_of(*fragments: Any) -> str:
    """Best-effort ISO country code from free-text location fragments.

    Delegates to `watcher.geo`, which holds the one city and country table the
    filters also read. This used to keep its own smaller copy; the two drifted,
    and a city known to the fetcher but not to the filter is exactly the kind of
    disagreement that shows up as a posting mysteriously never arriving.

    Still best-effort, not an authority: an unresolved country stays blank and
    the prefilter treats blank as "do not exclude".
    """
    from .. import geo

    return geo.country_of(*fragments)


def is_remote(*fragments: Any) -> bool:
    text = " ".join(str(f) for f in fragments if f)
    if not text:
        return False
    return bool(_REMOTE.search(text)) and not _HYBRID.search(text)


def first(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """First present, non-empty value among `keys`."""
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return default
