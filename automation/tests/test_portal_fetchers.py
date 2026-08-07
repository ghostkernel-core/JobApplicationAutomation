"""The two browser-backed portals, and the registry that dispatches to them.

hiring.cafe and StepStone read internal, undocumented structures — a Next.js
data endpoint and a Redux preload — so they will break, and `fragile = true`
says so. What must not happen is breaking *quietly*: a shape change that yields
an empty list looks exactly like a search with no results, and a source can sit
in that state for weeks. Every parse failure here has to raise.

The other half is the opposite obligation. A hit missing a title, one query out
of four failing, a page of duplicates — none of those is a source failure, and
treating them as one costs a poll for nothing.

The browser is stubbed at `evaluate`/`json_api`/`page_text`, which is the seam
between "drive a real Chromium" and "parse what it returned". Everything below
that line is Playwright's problem and is checked by the live workflow.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse

import pytest

from watcher.fetchers import SourceNotImplemented, fetch_source, hydrate, source_urls
from watcher.fetchers import browser, hiringcafe, stepstone
from watcher.normalize import Posting


# --------------------------------------------------------------------------
# hiring.cafe
# --------------------------------------------------------------------------

HC_HIT = {
    "id": "hc-9912",
    "apply_url": "https://boards.greenhouse.io/qogita/jobs/4412",
    "v5_processed_job_data": {
        "core_job_title": "Data Scientist",
        "company_name": "Qogita",
        "workplace_countries": ["NL"],
        "formatted_workplace_location": "Amsterdam, Netherlands",
        "workplace_type": "Hybrid",
        "estimated_publish_date": "2026-07-18",
        "requirements_summary": "Three years of Python.",
        "technical_tools": ["Python", "dbt", "Snowflake"],
    },
    "job_information": {"title": "Data Scientist (Growth)"},
    "enriched_company_data": {"name": "Qogita B.V."},
}

HC_ENTRY = {"name": "hiringcafe", "queries": ["data scientist"]}


@pytest.fixture
def hc(monkeypatch):
    """Stub the browser and let a test script the pages hiring.cafe returns."""
    state = {"build_id": "BUILD123", "title": "Hiring Cafe", "pages": [],
             "asked": [], "texts": {}, "raises": None}

    # The fetcher reads the title alongside the build id, so that a page with
    # neither can say which of the two reasons it had.
    monkeypatch.setattr(browser, "evaluate", lambda *a, **k: {
        "buildId": state["build_id"], "title": state["title"]})

    def json_api(site, url, payload=None, **kwargs):
        state["asked"].append(url)
        index = min(len(state["asked"]) - 1, len(state["pages"]) - 1)
        page = state["pages"][index]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(browser, "json_api", json_api)

    def page_text(url, **kwargs):
        if state["raises"]:
            raise state["raises"]
        return state["texts"].get(url, "")

    monkeypatch.setattr(browser, "page_text", page_text)
    return state


def _hc_page(hits, last=True) -> dict:
    return {"pageProps": {"ssrHits": hits, "ssrIsLastPage": last}}


def test_hiringcafe_reads_the_build_id_from_the_live_page(hc) -> None:
    """Pinning it would break on every deploy; this breaks only on a redesign."""
    hc["pages"] = [_hc_page([HC_HIT])]

    hiringcafe.fetch(HC_ENTRY, 10)

    assert "/_next/data/BUILD123/index.json" in hc["asked"][0]


def test_hiringcafe_encodes_the_query_into_the_search_state(hc) -> None:
    hc["pages"] = [_hc_page([])]

    hiringcafe.fetch(HC_ENTRY, 10)

    raw = urllib.parse.parse_qs(urllib.parse.urlsplit(hc["asked"][0]).query)
    assert json.loads(raw["searchState"][0]) == {"searchQuery": "data scientist"}


def test_hiringcafe_says_so_when_the_page_shape_changed(hc) -> None:
    """A missing build id must raise, not silently poll a broken URL."""
    hc["build_id"] = None

    with pytest.raises(RuntimeError, match="page shape has changed"):
        hiringcafe.fetch(HC_ENTRY, 10)


@pytest.mark.parametrize("title", [
    "Vercel Security Checkpoint",
    "Vercel Sicherheitskontrollpunkt",   # the site is localised
    "Just a moment...",
])
def test_hiringcafe_tells_a_bot_challenge_from_a_redesign(hc, title) -> None:
    """The two look identical from here and want opposite things.

    A challenge clears on its own; a redesign is a code change in the fetcher.
    Naming both and committing to neither meant opening the site by hand at the
    moment it was failing to find out which.
    """
    hc["build_id"] = None
    hc["title"] = title

    with pytest.raises(RuntimeError, match="bot challenge served") as caught:
        hiringcafe.fetch(HC_ENTRY, 10)
    assert "page shape" not in str(caught.value)


def test_hiringcafe_says_so_when_the_payload_lost_its_props(hc) -> None:
    hc["pages"] = [{"nothing": "useful"}]

    with pytest.raises(RuntimeError, match="pageProps"):
        hiringcafe.fetch(HC_ENTRY, 10)


def test_hiringcafe_maps_every_field(hc) -> None:
    hc["pages"] = [_hc_page([HC_HIT])]

    [post] = hiringcafe.fetch(HC_ENTRY, 10)

    assert post.source == "portal:hiringcafe"
    assert post.source_job_id == "hc-9912"
    # `job_information.title` is the employer's own wording and wins.
    assert post.title == "Data Scientist (Growth)"
    assert post.company == "Qogita"
    assert post.location == "Amsterdam, Netherlands"
    assert post.country == "NL"
    assert post.posted_at == dt.date(2026, 7, 18)


def test_hiringcafe_does_not_call_hybrid_remote(hc) -> None:
    """Hybrid still requires presence; the flag decides an out-of-region pass."""
    hc["pages"] = [_hc_page([HC_HIT])]

    [post] = hiringcafe.fetch(HC_ENTRY, 10)

    assert post.remote is False


def test_hiringcafe_reads_its_own_remote_wording(hc) -> None:
    hit = json.loads(json.dumps(HC_HIT))
    hit["v5_processed_job_data"]["workplace_type"] = "Remote"
    hc["pages"] = [_hc_page([hit])]

    [post] = hiringcafe.fetch(HC_ENTRY, 10)

    assert post.remote is True


def test_hiringcafe_carries_a_teaser_and_marks_the_body_for_hydration(hc) -> None:
    """Search time has their summary, not the ad. That distinction is the source
    of the whole teaser-scoring problem, so the teaser must not look like a body."""
    hc["pages"] = [_hc_page([HC_HIT])]

    [post] = hiringcafe.fetch(HC_ENTRY, 10)

    assert "Three years of Python." in post.description
    assert "Snowflake" in post.description
    assert post.detail_url == "https://boards.greenhouse.io/qogita/jobs/4412"


def test_hiringcafe_falls_back_to_the_geo_layer_when_no_country_is_given(
        hc) -> None:
    hit = json.loads(json.dumps(HC_HIT))
    hit["v5_processed_job_data"]["workplace_countries"] = []
    hit["v5_processed_job_data"]["formatted_workplace_location"] = "Berlin, Germany"
    hc["pages"] = [_hc_page([hit])]

    [post] = hiringcafe.fetch(HC_ENTRY, 10)

    assert post.country == "DE"


def test_hiringcafe_skips_a_hit_with_no_title_or_no_link(hc) -> None:
    """Neither is a source failure — they are drafts and adverts."""
    hc["pages"] = [_hc_page([
        HC_HIT,
        {"id": "x", "apply_url": "https://x", "v5_processed_job_data": {}},
        {"id": "y", "v5_processed_job_data": {"core_job_title": "Analyst"}},
    ])]

    postings = hiringcafe.fetch(HC_ENTRY, 10)

    assert [p.source_job_id for p in postings] == ["hc-9912"]


def test_hiringcafe_keeps_one_copy_of_a_hit_repeated_across_pages(hc) -> None:
    hc["pages"] = [_hc_page([HC_HIT], last=False), _hc_page([HC_HIT])]

    assert len(hiringcafe.fetch(HC_ENTRY, 10)) == 1


def test_hiringcafe_stops_at_the_last_page(hc) -> None:
    hc["pages"] = [_hc_page([HC_HIT])]

    hiringcafe.fetch(HC_ENTRY, 10)

    assert len(hc["asked"]) == 1


def test_hiringcafe_stops_when_a_page_comes_back_empty(hc) -> None:
    hc["pages"] = [_hc_page([], last=False)]

    assert hiringcafe.fetch(HC_ENTRY, 10) == []
    assert len(hc["asked"]) == 1


def test_hiringcafe_never_pages_past_its_own_ceiling(hc) -> None:
    """Past this the hits stop being on-topic and the cost stops paying."""
    hc["pages"] = [_hc_page([dict(HC_HIT, id=f"p{i}")], last=False)
                   for i in range(10)]

    hiringcafe.fetch(HC_ENTRY, 10)

    assert len(hc["asked"]) == hiringcafe.MAX_PAGES


HC_TWO_QUERIES = {"name": "hiringcafe",
                  "queries": ["data scientist", "ml engineer"]}


def test_hiringcafe_keeps_the_queries_that_worked_when_one_fails(hc) -> None:
    """Four queries run per poll. Letting the first bad one abort the rest
    throws away three working searches and reports the source as down."""
    hc["pages"] = [RuntimeError("HTTP 502"), _hc_page([HC_HIT])]

    postings = hiringcafe.fetch(HC_TWO_QUERIES, 10)

    assert len(postings) == 1


def test_hiringcafe_fails_only_when_every_query_failed(hc) -> None:
    """Nothing came back at all, so the source really is unhealthy."""
    hc["pages"] = [RuntimeError("HTTP 502"), RuntimeError("HTTP 502")]

    with pytest.raises(RuntimeError, match="HTTP 502"):
        hiringcafe.fetch(HC_TWO_QUERIES, 10)


def test_hiringcafe_search_urls_are_the_pages_a_person_can_open(hc) -> None:
    [(query, url)] = hiringcafe.search_urls(HC_ENTRY)

    assert query == "data scientist"
    assert url.startswith("https://hiringcafe.com/?searchState=")


def test_hiringcafe_search_urls_of_an_unqueried_entry_are_empty() -> None:
    assert hiringcafe.search_urls({"name": "hiringcafe"}) == []


# --------------------------------------------------------------------------
# hiring.cafe hydration — the source that meets employers' own ATS pages
# --------------------------------------------------------------------------

def test_hiringcafe_hydration_returns_the_rendered_body(hc) -> None:
    hc["texts"] = {"https://x/job/1": "The full advert text." * 20}
    posting = Posting(source="portal:hiringcafe", provider="hiringcafe",
                      source_job_id="1", url="https://x/job/1", company="c",
                      title="t", description="teaser",
                      detail_url="https://x/job/1")

    assert "The full advert text." in hiringcafe.hydrate(posting, 10)


def test_hiringcafe_hydration_keeps_the_teaser_when_the_render_fails(hc) -> None:
    """The posting has already passed the prefilter; it is scored either way."""
    hc["raises"] = RuntimeError("net::ERR_NAME_NOT_RESOLVED")
    posting = Posting(source="portal:hiringcafe", provider="hiringcafe",
                      source_job_id="1", url="https://x/job/1", company="c",
                      title="t", description="teaser",
                      detail_url="https://x/job/1")

    assert hiringcafe.hydrate(posting, 10) == "teaser"


def test_hiringcafe_hydration_keeps_the_teaser_when_the_page_is_blank(hc) -> None:
    hc["texts"] = {"https://x/job/1": "   "}
    posting = Posting(source="portal:hiringcafe", provider="hiringcafe",
                      source_job_id="1", url="https://x/job/1", company="c",
                      title="t", description="teaser",
                      detail_url="https://x/job/1")

    assert hiringcafe.hydrate(posting, 10) == "teaser"


def test_hiringcafe_hydration_of_a_posting_with_no_link_does_nothing(hc) -> None:
    posting = Posting(source="portal:hiringcafe", provider="hiringcafe",
                      source_job_id="1", url="https://x", company="c",
                      title="t", description="teaser")

    assert hiringcafe.hydrate(posting, 10) == "teaser"


# --------------------------------------------------------------------------
# StepStone
# --------------------------------------------------------------------------

SS_ITEM = {
    "id": "12345678",
    "title": "Data Scientist (m/w/d)",
    "url": "/stellenangebote--Data-Scientist-Berlin--12345678-inline.html",
    "companyName": "Zalando SE",
    "location": "Berlin",
    "datePosted": "2026-07-22T00:00:00Z",
    "textSnippet": "Wir suchen eine Data Scientist.",
}

SS_ENTRY = {"name": "stepstone", "queries": ["data scientist"],
            "location": "Düsseldorf"}


@pytest.fixture
def ss(monkeypatch):
    state = {"pages": [], "asked": [], "texts": {}, "raises": None}

    def evaluate(url, script, timeout=30):
        state["asked"].append(url)
        index = min(len(state["asked"]) - 1, len(state["pages"]) - 1)
        page = state["pages"][index]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(browser, "evaluate", evaluate)

    def page_text(url, **kwargs):
        if state["raises"]:
            raise state["raises"]
        return state["texts"].get(url, "")

    monkeypatch.setattr(browser, "page_text", page_text)
    return state


def _ss_page(items, page_count=1) -> dict:
    return {"items": items, "pagination": {"pageCount": page_count}}


@pytest.mark.parametrize("text,slug", [
    ("Data Scientist", "data-scientist"),
    ("Düsseldorf", "duesseldorf"),
    ("München", "muenchen"),
    ("Köln", "koeln"),
    ("Groß-Gerau", "gross-gerau"),
    ("  spaced  out  ", "spaced-out"),
])
def test_stepstone_slugs_match_the_spellings_the_site_routes_on(text, slug) -> None:
    """NFKD alone turns Düsseldorf into `dusseldorf`, which 404s."""
    assert stepstone._slug(text) == slug


def test_stepstone_builds_the_public_search_url(ss) -> None:
    ss["pages"] = [_ss_page([])]

    stepstone.fetch(SS_ENTRY, 10)

    assert ss["asked"][0] == ("https://www.stepstone.de/jobs/data-scientist"
                              "/in-duesseldorf")


def test_stepstone_omits_the_place_when_none_is_configured(ss) -> None:
    ss["pages"] = [_ss_page([])]

    stepstone.fetch({"name": "stepstone", "queries": ["data scientist"]}, 10)

    assert ss["asked"][0] == "https://www.stepstone.de/jobs/data-scientist"


def test_stepstone_pages_with_the_sites_own_query_parameter(ss) -> None:
    ss["pages"] = [_ss_page([SS_ITEM], page_count=3),
                   _ss_page([dict(SS_ITEM, id="2")], page_count=3),
                   _ss_page([dict(SS_ITEM, id="3")], page_count=3)]

    stepstone.fetch(SS_ENTRY, 10)

    assert ss["asked"][1].endswith("?page=2")
    assert ss["asked"][2].endswith("?page=3")


def test_stepstone_maps_every_field(ss) -> None:
    ss["pages"] = [_ss_page([SS_ITEM])]

    [post] = stepstone.fetch(SS_ENTRY, 10)

    assert post.source == "portal:stepstone"
    assert post.source_job_id == "12345678"
    assert post.company == "Zalando SE"
    assert post.location == "Berlin"
    assert post.country == "DE"
    assert post.posted_at == dt.date(2026, 7, 22)
    assert post.description == "Wir suchen eine Data Scientist."


def test_stepstone_makes_a_relative_link_absolute(ss) -> None:
    ss["pages"] = [_ss_page([SS_ITEM])]

    [post] = stepstone.fetch(SS_ENTRY, 10)

    assert post.url.startswith("https://www.stepstone.de/stellenangebote--")


def test_stepstone_leaves_an_absolute_link_alone(ss) -> None:
    ss["pages"] = [_ss_page([dict(SS_ITEM, url="https://www.stepstone.de/x")])]

    [post] = stepstone.fetch(SS_ENTRY, 10)

    assert post.url == "https://www.stepstone.de/x"


def test_stepstone_says_so_when_the_preloaded_state_is_gone(ss) -> None:
    """A challenge page and a redesign both land here, and both need a person."""
    ss["pages"] = [None]

    with pytest.raises(RuntimeError, match="page shape changed"):
        stepstone.fetch(SS_ENTRY, 10)


def test_stepstone_skips_hits_with_no_title_link_or_id(ss) -> None:
    ss["pages"] = [_ss_page([
        SS_ITEM,
        {"id": "2", "url": "/x"},                     # no title
        {"id": "3", "title": "Analyst"},              # no link
        {"title": "Analyst", "url": "/y"},            # no id
    ])]

    postings = stepstone.fetch(SS_ENTRY, 10)

    assert [p.source_job_id for p in postings] == ["12345678"]


def test_stepstone_keeps_one_copy_of_a_hit_repeated_across_pages(ss) -> None:
    ss["pages"] = [_ss_page([SS_ITEM], page_count=2),
                   _ss_page([SS_ITEM], page_count=2)]

    assert len(stepstone.fetch(SS_ENTRY, 10)) == 1


def test_stepstone_stops_at_the_last_page_the_site_reports(ss) -> None:
    ss["pages"] = [_ss_page([SS_ITEM], page_count=1)]

    stepstone.fetch(SS_ENTRY, 10)

    assert len(ss["asked"]) == 1


def test_stepstone_never_pages_past_its_own_ceiling(ss) -> None:
    ss["pages"] = [_ss_page([dict(SS_ITEM, id=f"p{i}")], page_count=99)
                   for i in range(10)]

    stepstone.fetch(SS_ENTRY, 10)

    assert len(ss["asked"]) == stepstone.MAX_PAGES


def test_stepstone_lets_the_other_queries_finish_when_one_fails(ss) -> None:
    """One query breaking must not cost the rest of the poll."""
    ss["pages"] = [RuntimeError("challenge served"), _ss_page([SS_ITEM])]

    postings = stepstone.fetch(
        dict(SS_ENTRY, queries=["broken", "data scientist"]), 10)

    assert [p.source_job_id for p in postings] == ["12345678"]


def test_stepstone_fails_the_source_when_every_query_failed(ss) -> None:
    """Nothing salvaged means the scraper is broken, and health must hear it."""
    ss["pages"] = [RuntimeError("challenge served")]

    with pytest.raises(RuntimeError, match="challenge served"):
        stepstone.fetch(dict(SS_ENTRY, queries=["a", "b"]), 10)


def test_stepstone_search_urls_are_the_pages_being_read(ss) -> None:
    assert stepstone.search_urls(SS_ENTRY) == [
        ("data scientist",
         "https://www.stepstone.de/jobs/data-scientist/in-duesseldorf")]


def test_stepstone_hydration_returns_the_rendered_body(ss) -> None:
    ss["texts"] = {"https://www.stepstone.de/x": "Die volle Stellenanzeige."}
    posting = Posting(source="portal:stepstone", provider="stepstone",
                      source_job_id="1", url="https://www.stepstone.de/x",
                      company="c", title="t", description="snippet",
                      detail_url="https://www.stepstone.de/x")

    assert stepstone.hydrate(posting, 10) == "Die volle Stellenanzeige."


def test_stepstone_hydration_keeps_the_snippet_when_the_render_fails(ss) -> None:
    ss["raises"] = RuntimeError("timeout")
    posting = Posting(source="portal:stepstone", provider="stepstone",
                      source_job_id="1", url="https://x", company="c",
                      title="t", description="snippet", detail_url="https://x")

    assert stepstone.hydrate(posting, 10) == "snippet"


def test_stepstone_hydration_of_a_posting_with_no_link_does_nothing(ss) -> None:
    posting = Posting(source="portal:stepstone", provider="stepstone",
                      source_job_id="1", url="https://x", company="c",
                      title="t", description="snippet")

    assert stepstone.hydrate(posting, 10) == "snippet"


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------

def test_an_ats_entry_is_dispatched_by_provider(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr("watcher.fetchers.ats.fetch",
                        lambda entry, timeout: seen.setdefault("entry", entry) or [])

    fetch_source({"provider": "greenhouse", "token": "celonis"}, 10)

    assert seen["entry"]["provider"] == "greenhouse"


def test_an_entry_marked_as_an_ats_is_dispatched_even_without_a_provider(
        monkeypatch) -> None:
    """`kind` and `provider` are both accepted; the ATS fetcher reports the gap."""
    monkeypatch.setattr("watcher.fetchers.ats.fetch",
                        lambda entry, timeout: ["dispatched"])

    assert fetch_source({"kind": "ats", "company": "Acme"}, 10) == ["dispatched"]


def test_a_portal_entry_is_dispatched_by_name(ss) -> None:
    ss["pages"] = [_ss_page([SS_ITEM])]

    [post] = fetch_source(SS_ENTRY, 10)

    assert post.provider == "stepstone"


def test_a_portal_nobody_has_written_yet_says_so(ss) -> None:
    with pytest.raises(SourceNotImplemented):
        fetch_source({"name": "indeed", "queries": ["x"]}, 10)


def test_a_portal_whose_module_is_missing_says_so(monkeypatch) -> None:
    """Listed in the registry but absent from disk — a half-finished addition."""
    monkeypatch.setitem(
        __import__("watcher.fetchers", fromlist=["x"]).PORTAL_MODULES,
        "indeed", "indeed")

    with pytest.raises(SourceNotImplemented, match="not implemented yet"):
        fetch_source({"name": "indeed"}, 10)


def test_source_urls_of_an_ats_entry_give_the_board_and_the_feed() -> None:
    urls = source_urls({"provider": "greenhouse", "token": "celonis"})

    assert urls.board == "https://job-boards.greenhouse.io/celonis"
    assert urls.feed.startswith("https://boards-api.greenhouse.io")
    assert urls.searches == ()


def test_source_urls_of_a_portal_give_the_site_and_its_searches() -> None:
    urls = source_urls(SS_ENTRY)

    assert urls.board == "https://www.stepstone.de"
    assert urls.searches[0][0] == "data scientist"
    assert urls.feed == ""


def test_source_urls_of_an_unimplemented_portal_are_blank_not_an_error() -> None:
    """A status report must print a source it cannot build a URL for."""
    assert source_urls({"name": "indeed"}) == source_urls({"name": "indeed"})
    assert source_urls({"name": "indeed"}).board == ""


def test_source_urls_of_a_portal_with_no_search_pages_still_reports_the_site(
        monkeypatch) -> None:
    monkeypatch.delattr(stepstone, "search_urls")

    assert source_urls(SS_ENTRY).board == "https://www.stepstone.de"


# --------------------------------------------------------------------------
# hydration dispatch
# --------------------------------------------------------------------------

def _hydratable(provider: str, detail_url: str = "https://x/job/1") -> Posting:
    return Posting(source=f"portal:{provider}", provider=provider,
                   source_job_id="1", url="https://x/job/1", company="c",
                   title="t", description="teaser", detail_url=detail_url)


def test_hydrate_does_nothing_for_a_posting_that_already_has_its_body() -> None:
    """No `detail_url` means the list endpoint gave us the whole ad."""
    assert hydrate(_hydratable("greenhouse", detail_url=""), 10) == "teaser"


def test_hydrate_uses_the_ats_hydrator_when_the_provider_has_one(
        monkeypatch) -> None:
    monkeypatch.setitem(
        __import__("watcher.fetchers.ats", fromlist=["x"]).HYDRATORS,
        "workday", lambda posting, timeout: "the workday body")

    assert hydrate(_hydratable("workday"), 10) == "the workday body"


def test_hydrate_uses_the_portal_hydrator_when_the_provider_is_a_portal(
        ss) -> None:
    ss["texts"] = {"https://x/job/1": "the stepstone body"}

    assert hydrate(_hydratable("stepstone"), 10) == "the stepstone body"


def test_hydrate_keeps_the_teaser_for_a_provider_with_no_hydrator() -> None:
    """`greenhouse` bodies arrive inline, so there is nothing to hydrate with."""
    assert hydrate(_hydratable("greenhouse"), 10) == "teaser"


def test_hydrate_keeps_the_teaser_for_a_portal_that_cannot_hydrate(
        monkeypatch) -> None:
    monkeypatch.delattr(stepstone, "hydrate")

    assert hydrate(_hydratable("stepstone"), 10) == "teaser"
