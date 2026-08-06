"""Company job-board fetchers.

These are the reliable tier: documented-enough public JSON endpoints, no
anti-bot, no proxies, no browser. Each provider function takes the source entry
from sources.toml and returns normalised Postings.

Descriptions come back inline for most providers. SmartRecruiters and Workday
need a second request per posting, so those set `detail_url` instead and are
hydrated later — only for postings that survive the free prefilter.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

import requests

from ..normalize import Posting, parse_date, to_text
from .base import country_of, first, get_json, get_text, is_remote, post_json, session


def _entry_key(entry: dict[str, Any]) -> str:
    return f"ats:{entry.get('company', entry.get('token', '?'))}"


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

def greenhouse_feed(entry: dict[str, Any]) -> str:
    return (f"https://boards-api.greenhouse.io/v1/boards"
            f"/{quote(entry['token'])}/jobs?content=true")


def fetch_greenhouse(entry: dict[str, Any], timeout: int) -> list[Posting]:
    token = entry["token"]
    data = get_json(session(), greenhouse_feed(entry), timeout)
    out = []
    for job in data.get("jobs", []):
        location = (job.get("location") or {}).get("name", "")
        # Greenhouse double-encodes the body: HTML entities wrapping HTML tags.
        body = to_text(job.get("content", ""))
        out.append(Posting(
            source=_entry_key(entry), provider="greenhouse",
            source_job_id=str(job.get("id", "")),
            url=job.get("absolute_url", ""),
            company=entry.get("company", token),
            title=job.get("title", ""),
            location=location,
            country=country_of(location, job.get("offices")),
            remote=is_remote(location, job.get("title")),
            posted_at=parse_date(first(job, "first_published", "updated_at")),
            description=body,
            raw=job,
        ))
    return out


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------

def lever_feed(entry: dict[str, Any]) -> str:
    return f"https://api.lever.co/v0/postings/{quote(entry['token'])}?mode=json"


def fetch_lever(entry: dict[str, Any], timeout: int) -> list[Posting]:
    token = entry["token"]
    data = get_json(session(), lever_feed(entry), timeout)
    if not isinstance(data, list):
        raise RuntimeError(f"Lever board {token!r} returned {type(data).__name__}, not a job list")
    out = []
    for job in data:
        cats = job.get("categories") or {}
        location = cats.get("location", "")
        lists = "\n\n".join(
            f"{item.get('text', '')}\n{to_text(item.get('content', ''))}"
            for item in job.get("lists", [])
        )
        body = "\n\n".join(filter(None, [
            job.get("descriptionPlain") or to_text(job.get("description", "")),
            lists,
            to_text(job.get("additionalPlain") or job.get("additional", "")),
        ]))
        out.append(Posting(
            source=_entry_key(entry), provider="lever",
            source_job_id=str(job.get("id", "")),
            url=job.get("hostedUrl", "") or job.get("applyUrl", ""),
            company=entry.get("company", token),
            title=job.get("text", ""),
            location=location,
            country=country_of(location, cats.get("allLocations")),
            remote=is_remote(location, cats.get("commitment"), job.get("workplaceType")),
            posted_at=parse_date(job.get("createdAt")),
            description=body,
            raw=job,
        ))
    return out


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------

def ashby_feed(entry: dict[str, Any]) -> str:
    return (f"https://api.ashbyhq.com/posting-api/job-board"
            f"/{quote(entry['token'])}?includeCompensation=true")


def fetch_ashby(entry: dict[str, Any], timeout: int) -> list[Posting]:
    token = entry["token"]
    data = get_json(session(), ashby_feed(entry), timeout)
    out = []
    for job in data.get("jobs", []):
        if job.get("isListed") is False:
            continue
        location = job.get("location", "")
        out.append(Posting(
            source=_entry_key(entry), provider="ashby",
            source_job_id=str(job.get("id", "")),
            url=job.get("jobUrl", "") or job.get("applyUrl", ""),
            company=entry.get("company", token),
            title=job.get("title", ""),
            location=location,
            country=country_of(location, job.get("secondaryLocations")),
            remote=bool(job.get("isRemote")) or is_remote(location),
            posted_at=parse_date(first(job, "publishedAt", "updatedAt")),
            description=job.get("descriptionPlain") or to_text(job.get("descriptionHtml", "")),
            raw=job,
        ))
    return out


# --------------------------------------------------------------------------
# SmartRecruiters — list is cheap, body needs a per-posting call
# --------------------------------------------------------------------------

def smartrecruiters_feed(entry: dict[str, Any]) -> str:
    return (f"https://api.smartrecruiters.com/v1/companies"
            f"/{quote(entry['token'])}/postings")


def fetch_smartrecruiters(entry: dict[str, Any], timeout: int) -> list[Posting]:
    token = entry["token"]
    sess = session()
    out: list[Posting] = []
    offset = 0
    while True:
        url = f"{smartrecruiters_feed(entry)}?limit=100&offset={offset}"
        data = get_json(sess, url, timeout)
        items = data.get("content", [])
        for job in items:
            loc = job.get("location") or {}
            location = ", ".join(filter(None, [loc.get("city"), loc.get("region")]))
            out.append(Posting(
                source=_entry_key(entry), provider="smartrecruiters",
                source_job_id=str(job.get("id", "")),
                url=(job.get("applyUrl")
                     or f"https://jobs.smartrecruiters.com/{token}/{job.get('id')}"),
                company=entry.get("company", token),
                title=job.get("name", ""),
                location=location,
                country=(loc.get("country") or "").upper() or country_of(location),
                remote=bool(loc.get("remote")) or is_remote(location),
                posted_at=parse_date(first(job, "releasedDate", "createdOn")),
                # Body lives behind a second call; hydrate on demand.
                detail_url=f"https://api.smartrecruiters.com/v1/companies/{quote(token)}/postings/{job.get('id')}",
                raw=job,
            ))
        offset += len(items)
        if len(items) < 100 or offset >= int(data.get("totalFound", 0)):
            break
    return out


def hydrate_smartrecruiters(posting: Posting, timeout: int) -> str:
    data = get_json(session(), posting.detail_url, timeout)
    ad = data.get("jobAd", {}).get("sections", {})
    parts = [
        ad.get(name, {}).get("text", "")
        for name in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
    ]
    return to_text("\n\n".join(p for p in parts if p))


# --------------------------------------------------------------------------
# Personio — XML feed
# --------------------------------------------------------------------------

def personio_feed(entry: dict[str, Any]) -> str:
    return f"https://{quote(entry['token'])}.jobs.personio.de/xml"


def fetch_personio(entry: dict[str, Any], timeout: int) -> list[Posting]:
    import xml.etree.ElementTree as ET

    token = entry["token"]
    text = get_text(session(), personio_feed(entry), timeout)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"Personio feed for {token!r} is not XML — check the subdomain "
            f"(<token>.jobs.personio.de): {text[:120]!r}"
        ) from exc
    # A wrong subdomain serves an HTML error page, and a tidy one parses as
    # XML perfectly well — root `html`, no `position` elements, so the fetch
    # would return an empty list and the source would read as "no openings"
    # for as long as nobody checked. An empty board is `<workzag-jobs/>`, so
    # the root tag tells the two apart without guessing.
    if root.tag.lower() in {"html", "head", "body"}:
        raise RuntimeError(
            f"Personio feed for {token!r} is HTML, not a job feed — check the "
            f"subdomain (<token>.jobs.personio.de): {text[:120]!r}"
        )
    out = []
    for pos in root.iter("position"):
        def field(name: str) -> str:
            node = pos.find(name)
            return (node.text or "").strip() if node is not None and node.text else ""

        job_id = field("id")
        office = field("office")
        body = "\n\n".join(
            f"{(jd.findtext('name') or '').strip()}\n{to_text(jd.findtext('value') or '')}"
            for jd in pos.iter("jobDescription")
        )
        out.append(Posting(
            source=_entry_key(entry), provider="personio",
            source_job_id=job_id,
            url=f"https://{token}.jobs.personio.de/job/{job_id}",
            company=entry.get("company", token),
            title=field("name"),
            location=office,
            country=country_of(office),
            remote=is_remote(office, field("employmentType")),
            posted_at=parse_date(field("createdAt")),
            description=body,
            raw={"id": job_id, "office": office},
        ))
    return out


# --------------------------------------------------------------------------
# Workday — POST search, body behind a second call
# --------------------------------------------------------------------------

def workday_feed(entry: dict[str, Any]) -> str:
    return (f"{entry['host'].rstrip('/')}/wday/cxs"
            f"/{entry['tenant']}/{entry['site']}/jobs")


def fetch_workday(entry: dict[str, Any], timeout: int) -> list[Posting]:
    host = entry["host"].rstrip("/")
    tenant, site = entry["tenant"], entry["site"]
    api = workday_feed(entry)
    sess = session()
    sess.headers["Content-Type"] = "application/json"
    out: list[Posting] = []
    offset = 0
    while offset < 200:  # a company board deeper than this is not worth paging
        payload = {"appliedFacets": {}, "limit": 20, "offset": offset,
                   "searchText": entry.get("search", "")}
        data = post_json(sess, api, timeout, json=payload)
        items = data.get("jobPostings", [])
        for job in items:
            path = job.get("externalPath", "")
            location = job.get("locationsText", "")
            # `bulletFields` carries the requisition id, but a board configured
            # without them returns `[]` rather than omitting the key — and
            # indexing that raised IndexError out of the middle of the loop,
            # failing the whole source over one posting. The path is a fine
            # fallback id: it is unique per posting and stable.
            bullets = job.get("bulletFields") or [""]
            out.append(Posting(
                source=_entry_key(entry), provider="workday",
                source_job_id=str(bullets[0] or path),
                url=f"{host}/{site}{path}",
                company=entry.get("company", tenant),
                title=job.get("title", ""),
                location=location,
                country=country_of(location, path),
                remote=is_remote(location, job.get("title")),
                posted_at=parse_date(job.get("postedOn")),
                detail_url=f"{host}/wday/cxs/{tenant}/{site}{path}",
                raw=job,
            ))
        offset += len(items)
        if len(items) < 20 or offset >= int(data.get("total", 0)):
            break
    return out


def hydrate_workday(posting: Posting, timeout: int) -> str:
    data = get_json(session(), posting.detail_url, timeout)
    info = data.get("jobPostingInfo", {})
    return to_text(info.get("jobDescription", ""))


# --------------------------------------------------------------------------

FETCHERS: dict[str, Callable[[dict[str, Any], int], list[Posting]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "personio": fetch_personio,
    "workday": fetch_workday,
}

HYDRATORS: dict[str, Callable[[Posting, int], str]] = {
    "smartrecruiters": hydrate_smartrecruiters,
    "workday": hydrate_workday,
}

# The endpoint each provider is actually polled on. Same functions the fetchers
# call, so `watcherctl status` can never show a URL the poller does not use.
FEED_URLS: dict[str, Callable[[dict[str, Any]], str]] = {
    "greenhouse": greenhouse_feed,
    "lever": lever_feed,
    "ashby": ashby_feed,
    "smartrecruiters": smartrecruiters_feed,
    "personio": personio_feed,
    "workday": workday_feed,
}

# ...and the page a person would open to see the same jobs. Not used for
# fetching — these are for the human reading a status report, so they point at
# the rendered board rather than at its JSON.
BOARD_URLS: dict[str, Callable[[dict[str, Any]], str]] = {
    "greenhouse": lambda e: f"https://job-boards.greenhouse.io/{e['token']}",
    "lever": lambda e: f"https://jobs.lever.co/{e['token']}",
    "ashby": lambda e: f"https://jobs.ashbyhq.com/{e['token']}",
    "smartrecruiters": lambda e: f"https://jobs.smartrecruiters.com/{e['token']}",
    "personio": lambda e: f"https://{e['token']}.jobs.personio.de",
    "workday": lambda e: f"{e['host'].rstrip('/')}/{e['site']}",
}


def urls(entry: dict[str, Any]) -> tuple[str, str]:
    """(board page, polled endpoint) for one `[[ats]]` entry.

    Returns empty strings for anything it cannot build — a status report must
    survive a half-written sources.toml entry that the poller would reject.
    """
    provider = entry.get("provider", "")
    out = []
    for table in (BOARD_URLS, FEED_URLS):
        builder = table.get(provider)
        try:
            out.append(builder(entry) if builder else "")
        except (KeyError, AttributeError, TypeError):
            out.append("")
    return out[0], out[1]


def fetch(entry: dict[str, Any], timeout: int) -> list[Posting]:
    provider = entry.get("provider", "")
    if provider not in FETCHERS:
        raise ValueError(
            f"unknown ATS provider {provider!r} for {entry.get('company')} "
            f"(expected one of {', '.join(sorted(FETCHERS))})"
        )
    try:
        return FETCHERS[provider](entry, timeout)
    except requests.HTTPError as exc:
        raise RuntimeError(f"{provider} HTTP {exc.response.status_code}") from exc
