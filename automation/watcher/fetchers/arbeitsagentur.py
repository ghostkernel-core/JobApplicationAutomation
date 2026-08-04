"""Bundesagentur für Arbeit — the Jobsuche API.

The reliable portal. Public JSON, a static client key, no anti-bot, no browser:
it belongs to the same tier as the company boards rather than to the fragile
scraper tier, which is why it is the one aggregator marked `fragile = false`.

Two endpoints, and they are deliberately on different versions:

    pc/v6/jobs                    search, one page of results
    pc/v4/jobdetails/<base64ref>  one posting's body

Search moved to v6 — v4 and v5 both answer 404 now, which is what silently
switched this source off. Details are still served by v4 (v3 is 404, v6 is 403),
so the two versions here are not a mistake to tidy up later.

The v6 search payload renamed nearly every field this fetcher reads:

    stellenangebote                -> ergebnisliste
    refnr                          -> referenznummer
    titel / beruf                  -> stellenangebotsTitel / hauptberuf
    arbeitgeber                    -> firma
    arbeitsort {ort,region,land}   -> stellenlokationen[].adresse
    aktuelleVeroeffentlichungsdatum-> datumErsteVeroeffentlichung
    externeUrl                     -> externeURL   (note the capitalisation)

That last one is the trap: `externeUrl` still parses, still returns None, and
the only symptom is every posting quietly pointing at the Jobbörse mirror
instead of the employer's own page. `_job_url` reads both spellings.

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
from .base import StructuralError, get_json, is_remote, session

log = logging.getLogger("watcher.fetch.arbeitsagentur")

BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
SEARCH_API = f"{BASE}/pc/v6"
DETAIL_API = f"{BASE}/pc/v4"
SITE = "https://www.arbeitsagentur.de/jobsuche/"

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

# The API returns Bundesländer as screaming-snake enums. Underscores map onto
# the hyphens in the real names; these three also lose an umlaut on the way out.
_REGION_FIXES = {
    "WUERTTEMBERG": "Württemberg",
    "THUERINGEN": "Thüringen",
    "MUENCHEN": "München",
}


def _session():
    sess = session()
    sess.headers.update({"X-API-Key": API_KEY})
    return sess


def _humanise(value: str) -> str:
    """NORDRHEIN_WESTFALEN -> Nordrhein-Westfalen."""
    if not value or not value.isupper():
        return value
    parts = [_REGION_FIXES.get(part, part.capitalize())
             for part in value.split("_")]
    return "-".join(parts)


def _job_url(job: dict[str, Any], ref: str) -> str:
    """Prefer the employer's own posting; fall back to the Jobbörse page.

    `externeURL` carries Bundesagentur tracking parameters, but `canonical_url`
    strips those, so the same posting reached directly and via this portal
    collapses to one fingerprint. Both the v6 and the legacy v4 spelling are
    accepted so a future rename back does not silently lose employer links.
    """
    for key in ("externeURL", "externeUrl"):
        external = (job.get(key) or "").strip()
        if external:
            return external
    return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{quote(ref)}"


def search_urls(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """(query, human search URL) for every query this portal is polled on.

    The fetcher calls the JSON API; this is the Jobbörse page carrying the same
    search, with the same location, radius and offer-kind restrictions applied.
    """
    where = entry.get("location") or ""
    radius = int(entry.get("radius_km", 0) or 0)
    out = []
    for query in entry.get("queries") or []:
        params = [f"was={quote(query)}", f"angebotsart={OFFER_KIND}"]
        if where:
            params.append(f"wo={quote(where)}")
            if radius:
                params.append(f"umkreis={radius}")
        out.append((query, "https://www.arbeitsagentur.de/jobsuche/suche?"
                           + "&".join(params)))
    return out


def _location(job: dict[str, Any]) -> str:
    """City and Bundesland of the first listed work location.

    v6 replaced the single `arbeitsort` object with a `stellenlokationen` list.
    Only the first entry is used: it is the one the search ranked on, and the
    city is what the geography filter reads.
    """
    locations = job.get("stellenlokationen") or []
    if not locations:
        return ""
    address = (locations[0] or {}).get("adresse") or {}
    parts = [address.get("ort"), _humanise(address.get("region") or "")]
    return ", ".join(p for p in parts if p and p != "null")


def _ref_token(ref: str) -> str:
    """The detail endpoint keys on the base64 of the reference number."""
    return base64.b64encode(ref.encode("utf-8")).decode("ascii")


def _posting(job: dict[str, Any], source: str) -> Posting | None:
    ref = str(job.get("referenznummer") or "")
    if not ref:
        return None
    location = _location(job)
    title = job.get("stellenangebotsTitel") or job.get("hauptberuf") or ""
    # v6 carries the home-office flag in the search payload, so a remote role is
    # known before hydration rather than only after it.
    remote = bool(job.get("homeofficemoeglich")) or is_remote(location, title)
    return Posting(
        source=source, provider="arbeitsagentur",
        source_job_id=ref,
        url=_job_url(job, ref),
        company=job.get("firma") or "",
        title=title,
        location=location,
        country="DE",  # the Bundesagentur lists German vacancies
        remote=remote,
        posted_at=parse_date(job.get("datumErsteVeroeffentlichung")
                             or (job.get("veroeffentlichungszeitraum")
                                 or {}).get("von")),
        description="",
        detail_url=f"{DETAIL_API}/jobdetails/{_ref_token(ref)}",
        raw=job,
    )


def fetch(entry: dict[str, Any], timeout: int) -> list[Posting]:
    queries = entry.get("queries") or []
    where = entry.get("location") or ""
    radius = int(entry.get("radius_km", 0) or 0)
    sess = _session()
    source = f"portal:{entry.get('name', 'arbeitsagentur')}"

    seen: set[str] = set()
    out: list[Posting] = []
    failures: list[str] = []
    structural: list[StructuralError] = []

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
                data = get_json(sess, f"{SEARCH_API}/jobs", timeout, params=params)
            except StructuralError as exc:
                # The endpoint itself is wrong — moved, withdrawn, or no longer
                # serving JSON. Every other query will fail identically, so
                # record it and let the summary below re-raise it unchanged:
                # the poller parks the source instead of probing it hourly.
                failures.append(f"{query!r} p{page}: {exc}")
                structural.append(exc)
                break
            except Exception as exc:  # noqa: BLE001
                # One bad query must not cost the other three. The failure is
                # collected and re-raised only if every query failed, so source
                # health reflects "the API is down", not "one search 500ed".
                failures.append(f"{query!r} p{page}: {exc}")
                break

            jobs = data.get("ergebnisliste") or []
            for job in jobs:
                posting = _posting(job, source)
                if posting is None or posting.source_job_id in seen:
                    continue
                seen.add(posting.source_job_id)
                out.append(posting)
            if len(jobs) < PAGE_SIZE:
                break

    if failures and not out:
        # Preserve the class of the failure. A structural error here is what
        # tells the poller this needs a code change, not another retry.
        summary = "; ".join(failures[:3])
        if structural:
            raise StructuralError(summary, structural[0].status_code)
        raise RuntimeError(summary)
    if failures:
        log.warning("arbeitsagentur: %d partial failures (%s)",
                    len(failures), failures[0])
    return out


def hydrate(posting: Posting, timeout: int) -> str:
    """Fetch one posting's body.

    Returns the existing description on failure rather than raising: by this
    point the posting has already passed the prefilter and is going to the
    matcher regardless, and a missing body is a weaker score, not a lost job.
    A 404 here is routine — Jobbörse postings expire faster than a poll cycle.
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
