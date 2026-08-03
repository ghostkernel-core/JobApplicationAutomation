"""Shared helpers for fetchers: HTTP session, location parsing, remote detection."""

from __future__ import annotations

import re
from typing import Any

import requests

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
    response = sess.get(url, timeout=timeout, **kwargs)
    return _decode(response, url)


def post_json(sess: requests.Session, url: str, timeout: int, **kwargs: Any) -> Any:
    response = sess.post(url, timeout=timeout, **kwargs)
    return _decode(response, url)


def _decode(response: requests.Response, url: str) -> Any:
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} from {url}")
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        snippet = response.text[:120].replace("\n", " ")
        raise RuntimeError(
            f"expected JSON from {url}, got {content_type or 'no content-type'}: {snippet}"
        )
    return response.json()


def get_text(sess: requests.Session, url: str, timeout: int) -> str:
    response = sess.get(url, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} from {url}")
    return response.text


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
