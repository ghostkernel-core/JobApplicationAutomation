"""StepStone — no API, read out of the page's own preloaded state.

The other two portals hand back JSON: Arbeitsagentur has a public API and
hiring.cafe has a Next.js data endpoint. StepStone has neither. Its search is
server-rendered behind Akamai Bot Manager (the `sensor_data` POSTs), and the
XHRs it does make are telemetry, not results — there is no clean search
endpoint to call, and this was checked rather than assumed.

What there is: the rendered page ships its results as a preloaded Redux store,

    window.__PRELOADED_STATE__["app-unifiedResultlist"].searchResults.items

which is the same data the result tiles are built from. So the shared browser
context loads the ordinary search URL a person would open and reads that object
out. No hidden endpoint, no reconstructed request signature.

Search URLs are the site's own public shape:

    /jobs/<slugified query>                  all of Germany
    /jobs/<slugified query>/in-<slug>        with a [[portal]] `location`

and paging is `?page=N`, per the `unifiedPagination.template` the page itself
publishes.

`items` mixes two kinds of hit — `section == "main"` is the literal query match,
`"semantic"` is StepStone's own loose expansion, and a broad query is mostly
the latter (57 main vs 316 semantic on one real run). Both are kept: the title
allow-list is the thing that decides relevance here, and it is stricter than
their notion of "related".

This is the most fragile source in the watcher and `fragile = true` is the
honest label. The store key, the field names, and the URL shape are all
internal and can change without notice. Failure is loud — a missing key raises,
the source is marked unhealthy, and one notification follows after
`failures_before_disable` polls.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from ..normalize import Posting, parse_date
from . import browser
from .base import is_remote

log = logging.getLogger("watcher.fetch.stepstone")

SITE = "https://www.stepstone.de"
MAX_PAGES = 3  # 25 hits/page; past this it is almost entirely semantic drift

# The store key the result list registers itself under.
STATE_KEY = "app-unifiedResultlist"

_READ_ITEMS = """
() => {
  const s = window.__PRELOADED_STATE__ || {};
  const r = (s[%r] || {}).searchResults;
  if (!r) return null;
  return {items: r.items || [], pagination: r.unifiedPagination || {}};
}
""" % STATE_KEY

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# Transliterate before NFKD, or "ü" decomposes to a bare "u" and Düsseldorf
# slugs as "dusseldorf", which is not the spelling StepStone routes on.
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                          "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def _slug(text: str) -> str:
    text = (text or "").translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return _SLUG_STRIP.sub("-", text.casefold()).strip("-")


def _search_url(query: str, where: str, page: int) -> str:
    path = f"{SITE}/jobs/{_slug(query)}"
    if where:
        path += f"/in-{_slug(where)}"
    return path if page <= 1 else f"{path}?page={page}"


def search_urls(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """(query, first-page search URL) for every query this portal is polled on.

    The same builder the fetcher uses, so a status report shows the page that
    is really being read rather than a hand-written approximation of it.
    """
    where = entry.get("location") or ""
    return [(query, _search_url(query, where, 1))
            for query in entry.get("queries") or []]


def _posting(item: dict[str, Any], source: str) -> Posting | None:
    title = (item.get("title") or "").strip()
    href = (item.get("url") or "").strip()
    if not title or not href:
        return None
    url = href if href.startswith("http") else f"{SITE}{href}"
    location = (item.get("location") or "").strip()

    return Posting(
        source=source, provider="stepstone",
        source_job_id=str(item.get("id") or ""),
        url=url,
        company=(item.get("companyName") or "").strip(),
        title=title,
        location=location,
        # stepstone.de lists German vacancies; the location field is a list of
        # German towns, not a country, so there is nothing to resolve here.
        country="DE",
        # `workFromHome` is an undocumented enum ("0", "2", …) whose middle
        # values are hybrid, not remote. Guessing it wrong would hand a posting
        # the out-of-region escape hatch it has not earned, so the text is read
        # instead — and for a DE-only board remote changes nothing anyway.
        remote=is_remote(location, title),
        posted_at=parse_date(item.get("datePosted")),
        description=(item.get("textSnippet") or "").strip(),
        detail_url=url,
        raw=item,
    )


def _page(query: str, where: str, page: int, timeout: int) -> dict[str, Any]:
    url = _search_url(query, where, page)
    data = browser.evaluate(url, _READ_ITEMS, timeout=timeout)
    if not isinstance(data, dict):
        raise RuntimeError(
            f"stepstone: no {STATE_KEY}.searchResults at {url} — the page "
            "shape changed or a challenge was served"
        )
    return data


def fetch(entry: dict[str, Any], timeout: int) -> list[Posting]:
    queries = entry.get("queries") or []
    where = entry.get("location") or ""
    source = f"portal:{entry.get('name', 'stepstone')}"

    seen: set[str] = set()
    out: list[Posting] = []
    failures: list[str] = []

    for query in queries:
        for page in range(1, MAX_PAGES + 1):
            try:
                data = _page(query, where, page, timeout)
            except Exception as exc:  # noqa: BLE001
                # One query failing must not cost the others; health is only
                # marked bad below if every one of them failed.
                failures.append(f"{query!r} p{page}: {exc}")
                break

            items = data.get("items") or []
            for item in items:
                posting = _posting(item, source)
                if posting is None or not posting.source_job_id:
                    continue
                if posting.source_job_id in seen:
                    continue
                seen.add(posting.source_job_id)
                out.append(posting)

            pagination = data.get("pagination") or {}
            if not items or page >= int(pagination.get("pageCount") or 0):
                break

    if failures and not out:
        raise RuntimeError("; ".join(failures[:3]))
    if failures:
        log.warning("stepstone: %d partial failures (%s)", len(failures), failures[0])
    return out


def hydrate(posting: Posting, timeout: int) -> str:
    """Render the job page for the full body.

    The search tile carries only `textSnippet`, roughly the first line of the
    ad. Failure returns what we already have: the posting has passed the
    prefilter and is going to the matcher either way.
    """
    if not posting.detail_url:
        return posting.description
    try:
        text = browser.page_text(posting.detail_url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("hydrate failed for %s: %s", posting.url, exc)
        return posting.description
    return text.strip() or posting.description
