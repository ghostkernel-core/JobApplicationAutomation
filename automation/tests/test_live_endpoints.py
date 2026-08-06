"""Opt-in checks against the real job boards.

    pytest automation/tests -m live                       # every source family
    pytest automation/tests -m "live and not live_browser"  # HTTP only

Deselected by default and never part of a PR gate: CI must not go red because a
third party is having a bad morning, and the offline suite already pins every
payload mapping against recorded fixtures.

What these answer that the offline suite cannot: **did the endpoint move?** A
provider that changes a field name, a path, or a response envelope breaks the
watcher silently — the fetch succeeds, returns nothing or returns rows with
empty titles, and the source reads as "no openings this week" for as long as
nobody looks. That failure mode is why every source family gets a probe here,
not just the one that has parked itself before.

**Boards are probed several-per-provider and pass if any one works.** A single
pinned tenant makes the check hostage to that one company: they switch ATS,
close their board, or go quiet over a holiday, and the scheduled run goes red
about something that is not our problem. Several boards answer the question
that actually matters — is the provider's API still shaped the way the fetcher
parses it — and stay green through any one company's move.

`live_browser` is a second tier for the two portals that need a real browser
and sit behind bot management. They are checked the same way, but a failure
there from a datacenter IP means "this IP is not welcome", which says nothing
about whether the source works from the watcher's own machine. CI runs them for
the signal and ignores the verdict; a human debugging a parked source runs them
locally, where the answer is worth something.
"""

from __future__ import annotations

from typing import Any

import pytest

from watcher.fetchers import arbeitsagentur as aa
from watcher.fetchers import ats, hiringcafe, stepstone
from watcher.normalize import Posting

pytestmark = pytest.mark.live

TIMEOUT = 45

# Public boards, several per provider. Any one of them working proves the
# provider's shape; see the module docstring for why it is not just one.
BOARDS: dict[str, list[dict[str, Any]]] = {
    "greenhouse": [{"token": t} for t in
                   ("gitlab", "stripe", "airbnb", "figma")],
    "lever": [{"token": t} for t in
              ("palantir", "gopuff", "spotify", "matchgroup")],
    "ashby": [{"token": t} for t in
              ("ramp", "notion", "cohere", "ashby")],
    "smartrecruiters": [{"token": t} for t in
                        ("BoschGroup", "Ubisoft2", "SmartRecruiters")],
    # Personio rate-limits hard, so the pool is small on purpose — a wide
    # sweep of subdomains earns 429s that look exactly like a broken feed.
    "personio": [{"token": t} for t in
                 ("personio-gmbh", "wandelbots", "personio")],
    "workday": [
        {"host": "https://nvidia.wd5.myworkdayjobs.com",
         "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"},
        {"host": "https://salesforce.wd12.myworkdayjobs.com",
         "tenant": "salesforce", "site": "External_Career_Site"},
    ],
}

# The two providers whose list endpoint carries no body, so the description
# arrives through a second call that has its own shape to break.
HYDRATED = ("smartrecruiters", "workday")

DUSSELDORF = {"location": "Düsseldorf", "radius_km": 100,
              "queries": ["Data Scientist"]}


def probe(provider: str) -> tuple[dict[str, Any], list[Posting]]:
    """The first board of `provider` that comes back with postings.

    An empty board is not a pass — a moved endpoint and a company with no
    openings both return zero rows, and only one of them is worth a scheduled
    run's attention.
    """
    trouble = []
    for board in BOARDS[provider]:
        entry = dict(board, provider=provider, kind="ats", company="probe")
        try:
            found = ats.fetch(entry, TIMEOUT)
        except Exception as exc:               # noqa: BLE001 — reported below
            trouble.append(f"{board}: {type(exc).__name__}: {exc}")
            continue
        if found:
            return entry, found
        trouble.append(f"{board}: reachable but empty")
    pytest.fail(
        f"no {provider} board returned any postings — either the provider's "
        f"API has moved, or every board pinned here has. Swap a token in "
        f"BOARDS and re-run before touching the fetcher.\n  "
        + "\n  ".join(trouble))


@pytest.fixture(scope="module")
def boards() -> dict[str, tuple[dict[str, Any], list[Posting]]]:
    """One probe per provider, shared across the tests below."""
    return {}


def postings_for(boards, provider: str) -> tuple[dict[str, Any], list[Posting]]:
    if provider not in boards:
        boards[provider] = probe(provider)
    return boards[provider]


# ===========================================================================
# the reliable tier: documented JSON and XML, no browser, no bot management
# ===========================================================================

@pytest.mark.parametrize("provider", sorted(BOARDS))
def test_the_board_still_answers_with_postings(boards, provider) -> None:
    _, found = postings_for(boards, provider)
    assert len(found) > 0


@pytest.mark.parametrize("provider", sorted(BOARDS))
def test_every_posting_carries_what_the_pipeline_needs(boards,
                                                       provider) -> None:
    """A row missing any of these is worse than no row: it reaches the
    prefilter, gets scored, and lands in Telegram as a posting with no title."""
    _, found = postings_for(boards, provider)
    for posting in found:
        assert posting.company, f"no company: {posting.source_job_id}"
        assert posting.title, f"no title: {posting.source_job_id}"
        assert posting.source_job_id, f"no id: {posting.title}"
        assert posting.url.startswith("http"), f"bad url: {posting.url!r}"
        assert posting.provider == provider


@pytest.mark.parametrize("provider", sorted(BOARDS))
def test_most_postings_carry_a_location(boards, provider) -> None:
    """The geo filter runs before scoring, so a provider that stops sending
    locations quietly drops everything it returns."""
    _, found = postings_for(boards, provider)
    located = [p for p in found if p.location or p.remote]
    assert len(located) > len(found) * 0.7


@pytest.mark.parametrize("provider", sorted(BOARDS))
def test_a_body_is_available_one_way_or_the_other(boards, provider) -> None:
    """Either inline or behind `detail_url` — a posting with neither cannot be
    scored, and a fetcher that silently stops returning bodies would send every
    posting to the model with nothing to judge."""
    entry, found = postings_for(boards, provider)
    if provider in HYDRATED:
        assert all(p.detail_url for p in found[:20])
        bodies = [ats.HYDRATORS[provider](p, TIMEOUT) for p in found[:3]]
        assert any(len(b) > 200 for b in bodies), \
            f"{provider} detail endpoint returned no body — it may have moved"
    else:
        bodies = [p.description for p in found if p.description]
        assert len(bodies) > len(found) * 0.5
        assert max(len(b) for b in bodies) > 200


@pytest.mark.parametrize("provider", sorted(BOARDS))
def test_the_polled_url_is_the_one_status_reports(boards, provider) -> None:
    """`watcherctl status` prints these, and a URL that is not the one the
    poller calls sends whoever is debugging to the wrong place."""
    entry, _ = postings_for(boards, provider)
    board, feed = ats.urls(entry)
    assert board.startswith("http") and feed.startswith("http")


# --------------------------------------------------------------------------
# Arbeitsagentur — a public API rather than a company board
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def jobsuche() -> list[Posting]:
    found = aa.fetch(dict(DUSSELDORF, name="arbeitsagentur"), TIMEOUT)
    if not found:
        pytest.fail("v6 search returned nothing — the endpoint or the query "
                    "shape has changed again")
    return found


def test_jobsuche_returns_postings(jobsuche) -> None:
    assert len(jobsuche) > 5


def test_every_jobsuche_posting_has_the_fields_the_pipeline_needs(
        jobsuche) -> None:
    for posting in jobsuche:
        assert posting.company, f"no company: {posting.source_job_id}"
        assert posting.title, f"no title: {posting.source_job_id}"
        assert posting.source_job_id
        assert posting.url.startswith("http")


def test_most_jobsuche_postings_carry_a_location(jobsuche) -> None:
    located = [p for p in jobsuche if p.location]
    assert len(located) > len(jobsuche) * 0.8


def test_jobsuche_dates_are_parsed(jobsuche) -> None:
    dated = [p for p in jobsuche if p.posted_at]
    assert len(dated) > len(jobsuche) * 0.8


def test_jobsuche_hydrate_returns_a_body(jobsuche) -> None:
    bodies = [aa.hydrate(p, TIMEOUT) for p in jobsuche[:3]]
    assert any(len(b) > 200 for b in bodies), \
        "no posting body came back — the v4 jobdetails endpoint may have moved"


# ===========================================================================
# the fragile tier: a real browser, and bot management on the other side
# ===========================================================================

@pytest.fixture(scope="module")
def cafe() -> list[Posting]:
    return hiringcafe.fetch({"name": "hiringcafe",
                             "queries": ["Data Scientist"]}, TIMEOUT)


@pytest.fixture(scope="module")
def steps() -> list[Posting]:
    return stepstone.fetch(dict(DUSSELDORF, name="stepstone"), TIMEOUT)


@pytest.mark.live_browser
def test_hiring_cafe_still_serves_its_next_data_endpoint(cafe) -> None:
    """The buildId in that URL changes on every deploy and is read fresh each
    run. If this is the thing that broke, it broke there."""
    assert len(cafe) > 5
    for posting in cafe[:20]:
        assert posting.title and posting.company
        assert posting.url.startswith("http")


@pytest.mark.live_browser
def test_hiring_cafe_postings_can_be_filtered_and_read(cafe) -> None:
    located = [p for p in cafe if p.location or p.remote]
    assert len(located) > len(cafe) * 0.7
    assert any(p.description or p.detail_url for p in cafe)


@pytest.mark.live_browser
def test_stepstone_still_preloads_its_results_into_the_page(steps) -> None:
    """Reading `__PRELOADED_STATE__` is the most fragile thing the watcher
    does. The store key and the field names are internal and can change
    without notice, which is what this is here to catch."""
    assert len(steps) > 5
    for posting in steps[:20]:
        assert posting.title and posting.company
        assert posting.url.startswith("http")


@pytest.mark.live_browser
def test_stepstone_postings_carry_a_location(steps) -> None:
    located = [p for p in steps if p.location or p.remote]
    assert len(located) > len(steps) * 0.7
