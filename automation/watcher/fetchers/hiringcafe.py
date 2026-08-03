"""hiring.cafe — aggregator, scraped.

No public API. The site is a Next.js app, so its search results arrive through
the framework's own data endpoint:

    /_next/data/<buildId>/index.json?searchState=<url-encoded json>&page=<n>

`buildId` changes on every deploy, so it is read from `window.__NEXT_DATA__` on
each run rather than pinned. That is the single most likely thing to break here,
and it breaks loudly: a stale buildId returns 404, the source is marked
unhealthy, and after `failures_before_disable` polls one notification is sent.
`fragile = true` in sources.toml is the honest label.

Their own location filters are not used. Every hit carries
`workplace_countries` and `formatted_workplace_location`, so the same geo layer
the rest of the watcher uses does the filtering — one place to reason about,
and no dependency on how they happen to spell their filter keys this month.

Descriptions are not in the payload; `apply_url` points at the original board,
so `detail_url` is set and the body is rendered later for survivors only.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

from ..normalize import Posting, parse_date
from . import browser
from .base import country_of

log = logging.getLogger("watcher.fetch.hiringcafe")

SITE = "https://hiringcafe.com/"
MAX_PAGES = 3  # ~40 hits/page; past this the results stop being on-topic


def _search_url(build_id: str, query: str, page: int) -> str:
    state = urllib.parse.quote(json.dumps({"searchQuery": query}))
    return f"{SITE}_next/data/{build_id}/index.json?searchState={state}&page={page}"


def _posting(hit: dict[str, Any], source: str) -> Posting | None:
    data = hit.get("v5_processed_job_data") or {}
    info = hit.get("job_information") or {}
    company_data = hit.get("enriched_company_data") or {}

    title = (info.get("title") or data.get("core_job_title") or "").strip()
    url = (hit.get("apply_url") or "").strip()
    if not title or not url:
        return None

    countries = data.get("workplace_countries") or []
    location = data.get("formatted_workplace_location") or ""

    # The requirements summary is theirs, not the employer's, and it is all
    # that arrives at search time. It is enough for the title/hard-blocker
    # rules; the real body is hydrated for whatever survives them.
    summary_parts = [
        data.get("requirements_summary") or "",
        ", ".join(data.get("technical_tools") or []),
    ]

    return Posting(
        source=source, provider="hiringcafe",
        source_job_id=str(hit.get("id") or hit.get("objectID") or ""),
        url=url,
        company=(data.get("company_name") or company_data.get("name") or "").strip(),
        title=title,
        location=location,
        country=(countries[0] if countries else country_of(location)) or "",
        remote=(data.get("workplace_type") or "").strip().casefold() == "remote",
        posted_at=parse_date(data.get("estimated_publish_date")),
        description="\n\n".join(p for p in summary_parts if p),
        detail_url=url,
        raw={k: v for k, v in hit.items() if k != "job_information"},
    )


def fetch(entry: dict[str, Any], timeout: int) -> list[Posting]:
    queries = entry.get("queries") or []
    source = f"portal:{entry.get('name', 'hiringcafe')}"

    build_id = _build_id(timeout)
    seen: set[str] = set()
    out: list[Posting] = []

    for query in queries:
        for page in range(MAX_PAGES):
            props = _page(build_id, query, page, timeout)
            hits = props.get("ssrHits") or []
            for hit in hits:
                posting = _posting(hit, source)
                if posting is None or posting.source_job_id in seen:
                    continue
                seen.add(posting.source_job_id)
                out.append(posting)
            if props.get("ssrIsLastPage") or not hits:
                break
    return out


def _build_id(timeout: int) -> str:
    """Read the deploy's build id out of the live page."""
    build_id = browser.evaluate(
        SITE,
        "() => window.__NEXT_DATA__ && window.__NEXT_DATA__.buildId",
        timeout=timeout,
    )
    if not build_id:
        raise RuntimeError(
            "hiring.cafe: no __NEXT_DATA__.buildId — the page shape changed "
            "or a challenge was served"
        )
    return str(build_id)


def _page(build_id: str, query: str, page: int, timeout: int) -> dict[str, Any]:
    data = browser.json_api(
        SITE, _search_url(build_id, query, page),
        method="GET", timeout=timeout,
    )
    props = (data or {}).get("pageProps")
    if not isinstance(props, dict):
        raise RuntimeError("hiring.cafe: response had no pageProps")
    return props


def hydrate(posting: Posting, timeout: int) -> str:
    """Render the original board page for the body.

    `apply_url` is the employer's own posting on Greenhouse, Ashby, Lever and
    the like, so this reaches the real text rather than an aggregator summary.
    A failure returns what we already have: the posting has passed the
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
