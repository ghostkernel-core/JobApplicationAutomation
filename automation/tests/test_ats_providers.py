"""Every ATS provider, against a recorded shape of its real response.

The six providers are the reliable tier and they are also the least visible one:
a board that changes a field name does not fail, it returns postings with an
empty title or a missing body, and those are filtered out silently. Nobody
notices until a company that posts weekly has been quiet for a month.

So each provider is pinned on the things a rename would break — the URL it is
polled on, the field each Posting attribute is read from, the pagination stop —
using payloads shaped like what the live endpoints returned when this was
written. The `live` marker in test_live_endpoints.py is what checks the shapes
are still current; these check the parsing given the shape.

Requests are intercepted at `get_json`/`get_text`/`post_json` rather than at the
socket, so the feed-URL builders run for real and a typo in one shows up as a
routing miss here instead of a 404 in production.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from watcher.fetchers import ats


# --------------------------------------------------------------------------
# a router standing in for the network
# --------------------------------------------------------------------------

class Router:
    """Answers by URL, and records every URL asked for.

    A miss raises rather than returning `{}` — a fetcher pointed at the wrong
    endpoint must fail the test loudly, which is exactly what it would do in
    production against a real host.
    """

    def __init__(self, monkeypatch) -> None:
        self.routes: list[tuple[str, object]] = []
        self.asked: list[str] = []
        self.posted: list[dict] = []
        monkeypatch.setattr(ats, "session", lambda: _FakeSession())
        monkeypatch.setattr(ats, "get_json", self._get)
        monkeypatch.setattr(ats, "get_text", self._get)
        monkeypatch.setattr(ats, "post_json", self._post)

    def on(self, fragment: str, payload) -> "Router":
        """Answer any URL containing `fragment` with `payload`.

        A callable payload is passed the URL, which is how the paging tests
        answer offset 0 and offset 100 differently.
        """
        self.routes.append((fragment, payload))
        return self

    def _resolve(self, url: str):
        self.asked.append(url)
        for fragment, payload in self.routes:
            if fragment in url:
                return payload(url) if callable(payload) else payload
        raise AssertionError(f"no route for {url}")

    def _get(self, _sess, url, _timeout, **_kw):
        return self._resolve(url)

    def _post(self, _sess, url, _timeout, **kwargs):
        self.posted.append(kwargs.get("json", {}))
        return self._resolve(url)


class _FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


@pytest.fixture
def net(monkeypatch) -> Router:
    return Router(monkeypatch)


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

GREENHOUSE_JOB = {
    "id": 4567890,
    "title": "Senior Data Scientist",
    "absolute_url": "https://job-boards.greenhouse.io/celonis/jobs/4567890",
    "location": {"name": "Munich, Germany"},
    "offices": [{"name": "Munich"}],
    "first_published": "2026-07-14T09:12:00-04:00",
    "updated_at": "2026-07-20T11:00:00-04:00",
    # Greenhouse entity-encodes HTML that is itself HTML. This is verbatim in
    # shape from the live feed, and the reason `to_text` decodes twice.
    "content": "&lt;p&gt;We need &lt;strong&gt;Python&lt;/strong&gt; and SQL.&lt;/p&gt;",
}

GREENHOUSE_ENTRY = {"provider": "greenhouse", "company": "Celonis",
                    "token": "celonis"}


def test_greenhouse_is_polled_on_its_board_api_with_content(net) -> None:
    net.on("boards-api.greenhouse.io", {"jobs": []})

    ats.fetch_greenhouse(GREENHOUSE_ENTRY, 10)

    assert net.asked == [
        "https://boards-api.greenhouse.io/v1/boards/celonis/jobs?content=true"]


def test_greenhouse_maps_every_field(net) -> None:
    net.on("greenhouse.io", {"jobs": [GREENHOUSE_JOB]})

    [post] = ats.fetch_greenhouse(GREENHOUSE_ENTRY, 10)

    assert post.source == "ats:Celonis"
    assert post.provider == "greenhouse"
    assert post.source_job_id == "4567890"
    assert post.url.endswith("/jobs/4567890")
    assert post.company == "Celonis"
    assert post.title == "Senior Data Scientist"
    assert post.location == "Munich, Germany"
    assert post.country == "DE"
    assert post.posted_at == dt.date(2026, 7, 14)


def test_greenhouse_bodies_arrive_as_text_not_markup(net) -> None:
    """The whole feed is doubly encoded; one decode leaves live `<p>` tags."""
    net.on("greenhouse.io", {"jobs": [GREENHOUSE_JOB]})

    [post] = ats.fetch_greenhouse(GREENHOUSE_ENTRY, 10)

    assert "Python" in post.description and "SQL" in post.description
    assert "<" not in post.description and "&lt;" not in post.description


def test_greenhouse_falls_back_to_updated_at_when_never_published(net) -> None:
    job = dict(GREENHOUSE_JOB, first_published=None)
    net.on("greenhouse.io", {"jobs": [job]})

    [post] = ats.fetch_greenhouse(GREENHOUSE_ENTRY, 10)

    assert post.posted_at == dt.date(2026, 7, 20)


def test_greenhouse_survives_a_job_with_no_location_object(net) -> None:
    """`location` comes back as null on internal-only postings."""
    net.on("greenhouse.io", {"jobs": [dict(GREENHOUSE_JOB, location=None)]})

    [post] = ats.fetch_greenhouse(GREENHOUSE_ENTRY, 10)

    assert post.location == ""


def test_greenhouse_token_is_url_quoted(net) -> None:
    net.on("boards-api", {"jobs": []})

    ats.fetch_greenhouse({"provider": "greenhouse", "token": "a b"}, 10)

    assert "a%20b" in net.asked[0]


def test_greenhouse_without_a_company_name_falls_back_to_the_token(net) -> None:
    net.on("greenhouse.io", {"jobs": [GREENHOUSE_JOB]})

    [post] = ats.fetch_greenhouse({"provider": "greenhouse", "token": "acme"}, 10)

    assert post.company == "acme" and post.source == "ats:acme"


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------

LEVER_JOB = {
    "id": "9f0e-4a21",
    "text": "Machine Learning Engineer",
    "hostedUrl": "https://jobs.lever.co/spotify/9f0e-4a21",
    "applyUrl": "https://jobs.lever.co/spotify/9f0e-4a21/apply",
    "categories": {"location": "Stockholm", "commitment": "Full-time",
                   "allLocations": ["Stockholm", "Berlin"]},
    "createdAt": 1_752_480_000_000,  # epoch milliseconds
    "descriptionPlain": "Build recommendation models.",
    "lists": [{"text": "What you will do",
               "content": "<li>Ship models</li><li>Measure them</li>"}],
    "additionalPlain": "We offer relocation.",
}

LEVER_ENTRY = {"provider": "lever", "company": "Spotify", "token": "spotify"}


def test_lever_is_polled_in_json_mode(net) -> None:
    net.on("api.lever.co", [])

    ats.fetch_lever(LEVER_ENTRY, 10)

    assert net.asked == [
        "https://api.lever.co/v0/postings/spotify?mode=json"]


def test_lever_maps_every_field(net) -> None:
    net.on("lever.co", [LEVER_JOB])

    [post] = ats.fetch_lever(LEVER_ENTRY, 10)

    assert post.source_job_id == "9f0e-4a21"
    assert post.title == "Machine Learning Engineer"
    assert post.url == "https://jobs.lever.co/spotify/9f0e-4a21"
    assert post.location == "Stockholm"
    assert post.posted_at == dt.date(2025, 7, 14)


def test_lever_joins_the_description_the_lists_and_the_extras(net) -> None:
    """Lever splits one ad across three fields; dropping any loses the middle."""
    net.on("lever.co", [LEVER_JOB])

    [post] = ats.fetch_lever(LEVER_ENTRY, 10)

    assert "Build recommendation models." in post.description
    assert "What you will do" in post.description
    assert "Ship models" in post.description
    assert "We offer relocation." in post.description


def test_lever_falls_back_to_the_apply_url(net) -> None:
    net.on("lever.co", [dict(LEVER_JOB, hostedUrl="")])

    [post] = ats.fetch_lever(LEVER_ENTRY, 10)

    assert post.url.endswith("/apply")


def test_lever_reads_html_description_when_there_is_no_plain_one(net) -> None:
    job = dict(LEVER_JOB, descriptionPlain="",
               description="<p>Build <b>models</b>.</p>")
    net.on("lever.co", [job])

    [post] = ats.fetch_lever(LEVER_ENTRY, 10)

    assert "Build" in post.description and "<p>" not in post.description


def test_lever_rejects_a_response_that_is_not_a_job_list(net) -> None:
    """A withdrawn board answers with an object, which would iterate as keys."""
    net.on("lever.co", {"error": "not found"})

    with pytest.raises(RuntimeError, match="not a job list"):
        ats.fetch_lever(LEVER_ENTRY, 10)


def test_lever_survives_a_posting_with_no_categories(net) -> None:
    net.on("lever.co", [dict(LEVER_JOB, categories=None)])

    [post] = ats.fetch_lever(LEVER_ENTRY, 10)

    assert post.location == ""


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------

ASHBY_JOB = {
    "id": "cf3b1",
    "title": "Applied Scientist",
    "jobUrl": "https://jobs.ashbyhq.com/deepl/cf3b1",
    "location": "Cologne, Germany",
    "secondaryLocations": [{"location": "Remote"}],
    "isRemote": False,
    "isListed": True,
    "publishedAt": "2026-06-30T08:00:00Z",
    "descriptionPlain": "Train translation models.",
}

ASHBY_ENTRY = {"provider": "ashby", "company": "DeepL", "token": "deepl"}


def test_ashby_is_polled_with_compensation_included(net) -> None:
    net.on("api.ashbyhq.com", {"jobs": []})

    ats.fetch_ashby(ASHBY_ENTRY, 10)

    assert net.asked == [
        "https://api.ashbyhq.com/posting-api/job-board/deepl"
        "?includeCompensation=true"]


def test_ashby_maps_every_field(net) -> None:
    net.on("ashbyhq.com", {"jobs": [ASHBY_JOB]})

    [post] = ats.fetch_ashby(ASHBY_ENTRY, 10)

    assert post.source_job_id == "cf3b1"
    assert post.title == "Applied Scientist"
    assert post.country == "DE"
    assert post.posted_at == dt.date(2026, 6, 30)
    assert post.description == "Train translation models."


def test_ashby_skips_an_unlisted_posting(net) -> None:
    """Unlisted means filled or draft — the board still returns it."""
    net.on("ashbyhq.com", {"jobs": [
        ASHBY_JOB, dict(ASHBY_JOB, id="hidden", isListed=False)]})

    postings = ats.fetch_ashby(ASHBY_ENTRY, 10)

    assert [p.source_job_id for p in postings] == ["cf3b1"]


def test_ashby_keeps_a_posting_that_does_not_say_either_way(net) -> None:
    """Absent `isListed` is not the same as `false`."""
    job = {k: v for k, v in ASHBY_JOB.items() if k != "isListed"}
    net.on("ashbyhq.com", {"jobs": [job]})

    assert len(ats.fetch_ashby(ASHBY_ENTRY, 10)) == 1


def test_ashby_trusts_its_own_remote_flag(net) -> None:
    net.on("ashbyhq.com", {"jobs": [dict(ASHBY_JOB, isRemote=True)]})

    [post] = ats.fetch_ashby(ASHBY_ENTRY, 10)

    assert post.remote is True


def test_ashby_falls_back_to_html_description_and_updated_at(net) -> None:
    job = dict(ASHBY_JOB, descriptionPlain="", publishedAt=None,
               descriptionHtml="<p>Train <i>models</i>.</p>",
               updatedAt="2026-07-02T08:00:00Z")
    net.on("ashbyhq.com", {"jobs": [job]})

    [post] = ats.fetch_ashby(ASHBY_ENTRY, 10)

    assert "Train" in post.description and "<p>" not in post.description
    assert post.posted_at == dt.date(2026, 7, 2)


# --------------------------------------------------------------------------
# SmartRecruiters — paged list, body behind a second call
# --------------------------------------------------------------------------

SR_JOB = {
    "id": "744000000",
    "name": "Data Scientist",
    "location": {"city": "Berlin", "region": "Berlin", "country": "de",
                 "remote": False},
    "releasedDate": "2026-07-01T00:00:00.000Z",
    "applyUrl": "https://jobs.smartrecruiters.com/DeliveryHero/744000000",
}

SR_ENTRY = {"provider": "smartrecruiters", "company": "Delivery Hero",
            "token": "DeliveryHero"}


def _sr_page(url: str) -> dict:
    """1062 postings across 11 pages, which is what the live board returns."""
    offset = int(url.split("offset=")[1])
    remaining = max(0, 1062 - offset)
    count = min(100, remaining)
    return {"totalFound": 1062,
            "content": [dict(SR_JOB, id=str(offset + i)) for i in range(count)]}


def test_smartrecruiters_pages_until_the_board_is_exhausted(net) -> None:
    net.on("api.smartrecruiters.com", _sr_page)

    postings = ats.fetch_smartrecruiters(SR_ENTRY, 10)

    assert len(postings) == 1062
    assert len(net.asked) == 11
    assert net.asked[0].endswith("?limit=100&offset=0")
    assert net.asked[1].endswith("?limit=100&offset=100")


def test_smartrecruiters_stops_on_a_short_page_even_if_the_total_lies(net) -> None:
    """`totalFound` has been seen higher than the board actually serves."""
    net.on("api.smartrecruiters.com",
           {"totalFound": 9999, "content": [SR_JOB] * 4})

    postings = ats.fetch_smartrecruiters(SR_ENTRY, 10)

    assert len(postings) == 4
    assert len(net.asked) == 1


def test_smartrecruiters_maps_every_field(net) -> None:
    net.on("api.smartrecruiters.com", {"totalFound": 1, "content": [SR_JOB]})

    [post] = ats.fetch_smartrecruiters(SR_ENTRY, 10)

    assert post.source_job_id == "744000000"
    assert post.title == "Data Scientist"
    assert post.location == "Berlin, Berlin"
    assert post.country == "DE"        # upper-cased from the feed's "de"
    assert post.posted_at == dt.date(2026, 7, 1)


def test_smartrecruiters_leaves_the_body_for_hydration(net) -> None:
    """Paying for 1062 bodies to keep three of them is the thing to avoid."""
    net.on("api.smartrecruiters.com", {"totalFound": 1, "content": [SR_JOB]})

    [post] = ats.fetch_smartrecruiters(SR_ENTRY, 10)

    assert post.description == ""
    assert post.detail_url == ("https://api.smartrecruiters.com/v1/companies"
                               "/DeliveryHero/postings/744000000")


def test_smartrecruiters_builds_a_url_when_the_feed_gives_none(net) -> None:
    job = {k: v for k, v in SR_JOB.items() if k != "applyUrl"}
    net.on("api.smartrecruiters.com", {"totalFound": 1, "content": [job]})

    [post] = ats.fetch_smartrecruiters(SR_ENTRY, 10)

    assert post.url == "https://jobs.smartrecruiters.com/DeliveryHero/744000000"


def test_smartrecruiters_hydration_joins_the_four_ad_sections(net) -> None:
    net.on("/postings/744000000", {"jobAd": {"sections": {
        "companyDescription": {"text": "<p>We deliver food.</p>"},
        "jobDescription": {"text": "<p>You build models.</p>"},
        "qualifications": {"text": "<p>Python.</p>"},
        "additionalInformation": {"text": "<p>Berlin office.</p>"},
    }}})
    posting = ats.Posting(
        source="ats:Delivery Hero", provider="smartrecruiters",
        source_job_id="744000000", url="https://x", company="Delivery Hero",
        title="Data Scientist",
        detail_url="https://api.smartrecruiters.com/v1/companies"
                   "/DeliveryHero/postings/744000000")

    body = ats.hydrate_smartrecruiters(posting, 10)

    for phrase in ("We deliver food.", "You build models.", "Python.",
                   "Berlin office."):
        assert phrase in body
    assert "<p>" not in body


def test_smartrecruiters_hydration_of_a_half_filled_ad_returns_what_exists(
        net) -> None:
    net.on("/postings/", {"jobAd": {"sections": {
        "jobDescription": {"text": "You build models."}}}})
    posting = ats.Posting(source="s", provider="smartrecruiters",
                          source_job_id="1", url="https://x", company="c",
                          title="t", detail_url="https://api/postings/1")

    assert ats.hydrate_smartrecruiters(posting, 10) == "You build models."


# --------------------------------------------------------------------------
# Personio — XML
# --------------------------------------------------------------------------

PERSONIO_XML = """<?xml version="1.0" encoding="utf-8"?>
<workzag-jobs>
  <position>
    <id>1234567</id>
    <office>Berlin</office>
    <department>Data</department>
    <name>Data Scientist (m/f/d)</name>
    <employmentType>permanent</employmentType>
    <createdAt>2026-06-15T10:00:00+02:00</createdAt>
    <jobDescriptions>
      <jobDescription>
        <name>Your tasks</name>
        <value>&lt;p&gt;Build forecasting models.&lt;/p&gt;</value>
      </jobDescription>
      <jobDescription>
        <name>Your profile</name>
        <value>&lt;p&gt;Python and SQL.&lt;/p&gt;</value>
      </jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>
"""

PERSONIO_ENTRY = {"provider": "personio", "company": "Merantix",
                  "token": "merantix"}


def test_personio_is_polled_on_its_xml_feed(net) -> None:
    net.on("jobs.personio.de", "<workzag-jobs></workzag-jobs>")

    ats.fetch_personio(PERSONIO_ENTRY, 10)

    assert net.asked == ["https://merantix.jobs.personio.de/xml"]


def test_personio_maps_every_field(net) -> None:
    net.on("personio.de", PERSONIO_XML)

    [post] = ats.fetch_personio(PERSONIO_ENTRY, 10)

    assert post.source_job_id == "1234567"
    assert post.title.startswith("Data Scientist")
    assert post.url == "https://merantix.jobs.personio.de/job/1234567"
    assert post.location == "Berlin"
    assert post.country == "DE"
    assert post.posted_at == dt.date(2026, 6, 15)


def test_personio_keeps_every_description_block_with_its_heading(net) -> None:
    net.on("personio.de", PERSONIO_XML)

    [post] = ats.fetch_personio(PERSONIO_ENTRY, 10)

    assert "Your tasks" in post.description
    assert "Build forecasting models." in post.description
    assert "Your profile" in post.description
    assert "Python and SQL." in post.description


def test_personio_says_which_subdomain_is_wrong_when_the_feed_is_html(
        net) -> None:
    """A tidy HTML error page parses as XML, so it reads as an empty board.

    That is the silent failure the typed fetch errors exist to prevent: a
    misconfigured source has to look broken, not quiet.
    """
    net.on("personio.de", "<!DOCTYPE html><html><body>Not found</body></html>")

    with pytest.raises(RuntimeError, match="check the subdomain"):
        ats.fetch_personio(PERSONIO_ENTRY, 10)


def test_personio_says_which_subdomain_is_wrong_when_the_feed_is_not_xml(
        net) -> None:
    net.on("personio.de", "Not Found")

    with pytest.raises(RuntimeError, match="check the subdomain"):
        ats.fetch_personio(PERSONIO_ENTRY, 10)


def test_personio_reports_a_genuinely_empty_board_as_empty(net) -> None:
    """The other half of the same judgement: no openings is not a failure."""
    net.on("personio.de", "<workzag-jobs></workzag-jobs>")

    assert ats.fetch_personio(PERSONIO_ENTRY, 10) == []


def test_personio_survives_a_position_missing_its_fields(net) -> None:
    net.on("personio.de",
           "<workzag-jobs><position><id>7</id></position></workzag-jobs>")

    [post] = ats.fetch_personio(PERSONIO_ENTRY, 10)

    assert post.source_job_id == "7"
    assert post.title == "" and post.location == ""


def test_personio_survives_an_empty_element(net) -> None:
    """`<office/>` parses to a node whose text is None, not an empty string."""
    net.on("personio.de",
           "<workzag-jobs><position><id>7</id><office/></position></workzag-jobs>")

    [post] = ats.fetch_personio(PERSONIO_ENTRY, 10)

    assert post.location == ""


# --------------------------------------------------------------------------
# Workday — POST search, body behind a second call
# --------------------------------------------------------------------------

WORKDAY_JOB = {
    "title": "Data Scientist",
    "externalPath": "/job/Basel/Data-Scientist_202600123",
    "locationsText": "Basel, Switzerland",
    "postedOn": "Posted 3 Days Ago",
    "bulletFields": ["202600123"],
}

WORKDAY_ENTRY = {"provider": "workday", "company": "Roche",
                 "host": "https://roche.wd3.myworkdayjobs.com/",
                 "tenant": "roche", "site": "roche-ext", "search": "data"}


def test_workday_posts_a_search_to_the_cxs_endpoint(net) -> None:
    net.on("/wday/cxs/", {"total": 0, "jobPostings": []})

    ats.fetch_workday(WORKDAY_ENTRY, 10)

    assert net.asked == [
        "https://roche.wd3.myworkdayjobs.com/wday/cxs/roche/roche-ext/jobs"]
    assert net.posted == [{"appliedFacets": {}, "limit": 20, "offset": 0,
                           "searchText": "data"}]


def test_workday_maps_every_field(net) -> None:
    net.on("/wday/cxs/", {"total": 1, "jobPostings": [WORKDAY_JOB]})

    [post] = ats.fetch_workday(WORKDAY_ENTRY, 10)

    assert post.source_job_id == "202600123"
    assert post.title == "Data Scientist"
    assert post.url == ("https://roche.wd3.myworkdayjobs.com/roche-ext"
                        "/job/Basel/Data-Scientist_202600123")
    assert post.location == "Basel, Switzerland"
    assert post.country == "CH"
    assert post.detail_url == ("https://roche.wd3.myworkdayjobs.com/wday/cxs"
                               "/roche/roche-ext"
                               "/job/Basel/Data-Scientist_202600123")


def test_workday_falls_back_to_the_path_when_there_is_no_bullet_field(net) -> None:
    net.on("/wday/cxs/", {"total": 1,
                          "jobPostings": [dict(WORKDAY_JOB, bulletFields=[])]})

    [post] = ats.fetch_workday(WORKDAY_ENTRY, 10)

    assert post.source_job_id == "/job/Basel/Data-Scientist_202600123"


def test_workday_pages_twenty_at_a_time(net) -> None:
    def page(url: str) -> dict:
        offset = net.posted[-1]["offset"]
        count = min(20, max(0, 54 - offset))
        return {"total": 54,
                "jobPostings": [dict(WORKDAY_JOB, bulletFields=[str(offset + i)])
                                for i in range(count)]}

    net.on("/wday/cxs/", page)

    postings = ats.fetch_workday(WORKDAY_ENTRY, 10)

    assert len(postings) == 54
    assert [p["offset"] for p in net.posted] == [0, 20, 40]


def test_workday_stops_at_two_hundred_however_deep_the_board_goes(net) -> None:
    """A board deeper than this is a search that needs narrowing, not paging."""
    net.on("/wday/cxs/", lambda _url: {"total": 10_000,
                                       "jobPostings": [WORKDAY_JOB] * 20})

    postings = ats.fetch_workday(WORKDAY_ENTRY, 10)

    assert len(postings) == 200
    assert len(net.posted) == 10


def test_workday_searches_for_everything_when_no_term_is_configured(net) -> None:
    entry = {k: v for k, v in WORKDAY_ENTRY.items() if k != "search"}
    net.on("/wday/cxs/", {"total": 0, "jobPostings": []})

    ats.fetch_workday(entry, 10)

    assert net.posted[0]["searchText"] == ""


def test_workday_hydration_reads_the_posting_info(net) -> None:
    net.on("/wday/cxs/", {"jobPostingInfo": {
        "jobDescription": "<p>You build <b>models</b>.</p>"}})
    posting = ats.Posting(source="ats:Roche", provider="workday",
                          source_job_id="1", url="https://x", company="Roche",
                          title="Data Scientist",
                          detail_url="https://roche/wday/cxs/roche/x/job/1")

    body = ats.hydrate_workday(posting, 10)

    assert body == "You build \nmodels\n."


def test_workday_hydration_of_an_empty_ad_returns_empty(net) -> None:
    net.on("/wday/cxs/", {})
    posting = ats.Posting(source="s", provider="workday", source_job_id="1",
                          url="https://x", company="c", title="t",
                          detail_url="https://roche/wday/cxs/roche/x/job/1")

    assert ats.hydrate_workday(posting, 10) == ""


# --------------------------------------------------------------------------
# dispatch and the registries
# --------------------------------------------------------------------------

def test_every_fetcher_has_a_feed_url_and_a_board_url() -> None:
    """`watcherctl status` prints both for each source; a gap prints nothing."""
    assert set(ats.FETCHERS) == set(ats.FEED_URLS) == set(ats.BOARD_URLS)


def test_every_hydrator_belongs_to_a_real_provider() -> None:
    assert set(ats.HYDRATORS) <= set(ats.FETCHERS)


def test_fetch_dispatches_on_provider(net) -> None:
    net.on("greenhouse.io", {"jobs": [GREENHOUSE_JOB]})

    [post] = ats.fetch(GREENHOUSE_ENTRY, 10)

    assert post.provider == "greenhouse"


def test_fetch_names_the_providers_it_does_know(net) -> None:
    """A typo in sources.toml should be self-correcting from the message."""
    with pytest.raises(ValueError) as excinfo:
        ats.fetch({"provider": "greenouse", "company": "Acme"}, 10)

    assert "greenouse" in str(excinfo.value)
    assert "greenhouse" in str(excinfo.value)


def test_fetch_rejects_an_entry_with_no_provider_at_all() -> None:
    with pytest.raises(ValueError):
        ats.fetch({"company": "Acme"}, 10)


def test_fetch_reports_the_status_code_of_a_raw_http_error(monkeypatch) -> None:
    """`requests` raises this directly if a fetcher ever calls it unwrapped."""
    response = requests.Response()
    response.status_code = 403

    def boom(_entry, _timeout):
        raise requests.HTTPError(response=response)

    monkeypatch.setitem(ats.FETCHERS, "greenhouse", boom)

    with pytest.raises(RuntimeError, match="greenhouse HTTP 403"):
        ats.fetch(GREENHOUSE_ENTRY, 10)


@pytest.mark.parametrize("entry,board,feed", [
    ({"provider": "greenhouse", "token": "celonis"},
     "https://job-boards.greenhouse.io/celonis",
     "https://boards-api.greenhouse.io/v1/boards/celonis/jobs?content=true"),
    ({"provider": "lever", "token": "spotify"},
     "https://jobs.lever.co/spotify",
     "https://api.lever.co/v0/postings/spotify?mode=json"),
    ({"provider": "ashby", "token": "deepl"},
     "https://jobs.ashbyhq.com/deepl",
     "https://api.ashbyhq.com/posting-api/job-board/deepl"
     "?includeCompensation=true"),
    ({"provider": "smartrecruiters", "token": "DeliveryHero"},
     "https://jobs.smartrecruiters.com/DeliveryHero",
     "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings"),
    ({"provider": "personio", "token": "merantix"},
     "https://merantix.jobs.personio.de",
     "https://merantix.jobs.personio.de/xml"),
    ({"provider": "workday", "host": "https://roche.wd3.myworkdayjobs.com/",
      "tenant": "roche", "site": "roche-ext"},
     "https://roche.wd3.myworkdayjobs.com/roche-ext",
     "https://roche.wd3.myworkdayjobs.com/wday/cxs/roche/roche-ext/jobs"),
])
def test_urls_are_reported_for_every_provider(entry, board, feed) -> None:
    assert ats.urls(entry) == (board, feed)


def test_urls_of_an_unknown_provider_are_blank_not_an_error() -> None:
    assert ats.urls({"provider": "taleo", "token": "x"}) == ("", "")


def test_urls_of_a_half_written_entry_are_blank_not_an_error() -> None:
    """A status report must survive a sources.toml the poller would reject."""
    assert ats.urls({"provider": "workday", "tenant": "roche"}) == ("", "")
    assert ats.urls({"provider": "greenhouse"}) == ("", "")
