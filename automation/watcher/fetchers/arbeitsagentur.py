"""Bundesagentur für Arbeit — the Jobsuche API.

The reliable portal. Public JSON, a static client key, no anti-bot, no browser:
it belongs to the same tier as the company boards rather than to the fragile
scraper tier, which is why it is the one aggregator marked `fragile = false`.

Two endpoints:

    pc/v4/jobs                    search, one page of ~25 results
    pc/v4/jobdetails/<base64ref>  one posting's body

Descriptions are not in the search response, so postings set `detail_url` and
are hydrated later — only for the ones that survive the free prefilter. A broad
query returns hundreds of results and almost none of them are worth a request.

Note `angebotsart = 1`: without it the feed also carries Ausbildung, Praktika,
and Selbstständigkeit, which are not what this watcher is for.
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import quote

from ..normalize import Posting, parse_date
from .base import get_json, is_remote, session

log = logging.getLogger("watcher.fetch.arbeitsagentur")

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4"

# Published client key for the public Jobsuche app — the same value the website
# and the mobile app send. Not a secret and not tied to an account, so it stays
# here rather than in .env, where it would imply a credential that can be
# revoked or rotated.
API_KEY = "jobboerse-jobsuche"

# Only regular employment. 1=Arbeit, 2=Selbstständigkeit, 4=Ausbildung/Duales
# Studium, 34=Praktikum/Trainee.
OFFER_KIND = 1

PAGE_SIZE = 50
MAX_PAGES = 4  # 200 postings per query is already far past what survives


def _session():
    sess = session()
    sess.headers.update({"X-API-Key": API_KEY})
    return sess


def _job_url(job: dict[str, Any], ref: str) -> str:
    """Prefer the employer's own posting; fall back to the Jobbörse page.

    `externeUrl` carries Bundesagentur tracking parameters, but `canonical_url`
    strips those, so the same posting reached directly and via this portal
    collapses to one fingerprint.
    """
    external = (job.get("externeUrl") or "").strip()
    if external:
        return external
    return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{quote(ref)}"


def _location(job: dict[str, Any]) -> str:
    ort = job.get("arbeitsort") or {}
    parts = [ort.get("ort"), ort.get("region"), ort.get("land")]
    return ", ".join(p for p in parts if p and p != "null")


def _ref_token(ref: str) -> str:
    """The detail endpoint keys on the base64 of the reference number."""
    return base64.b64encode(ref.encode("utf-8")).decode("ascii")


def fetch(entry: dict[str, Any], timeout: int) -> list[Posting]:
    queries = entry.get("queries") or []
    where = entry.get("location") or ""
    radius = int(entry.get("radius_km", 0) or 0)
    sess = _session()
    source = f"portal:{entry.get('name', 'arbeitsagentur')}"

    seen: set[str] = set()
    out: list[Posting] = []
    failures: list[str] = []

    for query in queries:
        for page in range(1, MAX_PAGES + 1):
            params: dict[str, Any] = {
                "was": query, "page": page, "size": PAGE_SIZE,
                "angebotsart": OFFER_KIND,
            }
            if where:
                params["wo"] = where
                if radius:
                    params["umkreis"] = radius
            try:
                data = get_json(sess, f"{API}/jobs", timeout, params=params)
            except Exception as exc:  # noqa: BLE001
                # One bad query must not cost the other three. The failure is
                # collected and re-raised only if every query failed, so source
                # health reflects "the API is down", not "one search 500ed".
                failures.append(f"{query!r} p{page}: {exc}")
                break

            jobs = data.get("stellenangebote") or []
            for job in jobs:
                ref = str(job.get("refnr") or "")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                location = _location(job)
                title = job.get("titel") or job.get("beruf") or ""
                out.append(Posting(
                    source=source, provider="arbeitsagentur",
                    source_job_id=ref,
                    url=_job_url(job, ref),
                    company=job.get("arbeitgeber") or "",
                    title=title,
                    location=location,
                    country="DE",  # the Bundesagentur lists German vacancies
                    remote=is_remote(location, title),
                    posted_at=parse_date(job.get("aktuelleVeroeffentlichungsdatum")),
                    description="",
                    detail_url=f"{API}/jobdetails/{_ref_token(ref)}",
                    raw=job,
                ))
            if len(jobs) < PAGE_SIZE:
                break

    if failures and not out:
        raise RuntimeError("; ".join(failures[:3]))
    if failures:
        log.warning("arbeitsagentur: %d partial failures (%s)",
                    len(failures), failures[0])
    return out


def hydrate(posting: Posting, timeout: int) -> str:
    """Fetch one posting's body.

    Returns the existing description on failure rather than raising: by this
    point the posting has already passed the prefilter and is going to the
    matcher regardless, and a missing body is a weaker score, not a lost job.
    """
    if not posting.detail_url:
        return posting.description
    try:
        data = get_json(_session(), posting.detail_url, timeout)
    except Exception as exc:  # noqa: BLE001
        log.warning("hydrate failed for %s: %s", posting.source_job_id, exc)
        return posting.description
    body = data.get("stellenangebotsBeschreibung") or ""
    if data.get("homeofficemoeglich"):
        posting.remote = True
    return str(body).strip()
