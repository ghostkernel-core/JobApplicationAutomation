"""The v6 Jobsuche payload mapping.

Every assertion here is a field the v6 rename moved. These run against a
recorded payload rather than the live API so the suite stays offline and
deterministic; `test_live_endpoints.py` is the opt-in check that the real
service still matches.
"""

from __future__ import annotations

import base64

import pytest

from watcher.fetchers import arbeitsagentur as aa
from watcher.fetchers.base import StructuralError, TransientError

ENTRY = {"name": "arbeitsagentur", "location": "Düsseldorf", "radius_km": 100,
         "queries": ["Data Scientist"]}

# One record shaped exactly like a live v6 hit, including the fields that only
# some records carry.
JOB = {
    "stellenangebotsart": "ARBEIT",
    "stellenangebotsTitel": "Data Scientist (w/m/d)",
    "hauptberuf": "Data Scientist",
    "firma": "LANXESS Deutschland GmbH",
    "referenznummer": "10000-1207127956-S",
    "datumErsteVeroeffentlichung": "2026-07-08",
    "veroeffentlichungszeitraum": {"von": "2026-07-08"},
    "homeofficemoeglich": False,
    "stellenlokationen": [{
        "adresse": {"plz": "50679", "ort": "Köln",
                    "region": "NORDRHEIN_WESTFALEN", "land": "DEUTSCHLAND"},
    }],
}


def page(*jobs):
    return {"ergebnisliste": list(jobs), "maxErgebnisse": len(jobs),
            "page": 1, "size": 50}


@pytest.fixture()
def fetched(monkeypatch):
    """Fetch with a stubbed transport; returns (postings, recorded_urls)."""
    def run(payloads, **kwargs):
        calls = []

        def fake_get_json(sess, url, timeout, params=None, **_):
            calls.append((url, params))
            outcome = payloads[min(len(calls) - 1, len(payloads) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(aa, "get_json", fake_get_json)
        entry = {**ENTRY, **kwargs}
        return aa.fetch(entry, 5), calls

    return run


# --------------------------------------------------------------------------
# field mapping
# --------------------------------------------------------------------------

def test_core_fields_map_from_v6_names(fetched):
    postings, _ = fetched([page(JOB)])
    assert len(postings) == 1
    posting = postings[0]
    assert posting.company == "LANXESS Deutschland GmbH"
    assert posting.title.startswith("Data Scientist")
    assert posting.source_job_id == "10000-1207127956-S"
    assert posting.country == "DE"
    assert str(posting.posted_at) == "2026-07-08"


def test_location_comes_from_stellenlokationen(fetched):
    """v6 replaced the flat `arbeitsort` object with a list."""
    postings, _ = fetched([page(JOB)])
    assert postings[0].location == "Köln, Nordrhein-Westfalen"


def test_region_enum_is_humanised(fetched):
    job = {**JOB, "stellenlokationen": [
        {"adresse": {"ort": "Stuttgart", "region": "BADEN_WUERTTEMBERG"}}]}
    postings, _ = fetched([page(job)])
    assert postings[0].location == "Stuttgart, Baden-Württemberg"


def test_missing_location_is_tolerated(fetched):
    job = {k: v for k, v in JOB.items() if k != "stellenlokationen"}
    postings, _ = fetched([page(job)])
    assert postings[0].location == ""


def test_external_url_uses_the_v6_capitalisation(fetched):
    """`externeURL`, not `externeUrl`. Reading the old spelling silently sends
    every posting to the Jobbörse mirror instead of the employer."""
    job = {**JOB, "externeURL": "https://jobs.example.com/ds-42"}
    postings, _ = fetched([page(job)])
    assert postings[0].url == "https://jobs.example.com/ds-42"


def test_legacy_external_url_spelling_still_read(fetched):
    job = {**JOB, "externeUrl": "https://jobs.example.com/legacy"}
    postings, _ = fetched([page(job)])
    assert postings[0].url == "https://jobs.example.com/legacy"


def test_falls_back_to_the_jobboerse_page(fetched):
    postings, _ = fetched([page(JOB)])
    assert postings[0].url.endswith("/jobdetail/10000-1207127956-S")


def test_homeoffice_flag_sets_remote(fetched):
    """v6 carries this in search, so remote is known before hydration."""
    postings, _ = fetched([page({**JOB, "homeofficemoeglich": True})])
    assert postings[0].remote is True


def test_detail_url_is_v4_and_base64(fetched):
    """Search moved to v6; details did not. v6 jobdetails answers 403."""
    postings, _ = fetched([page(JOB)])
    detail = postings[0].detail_url
    token = base64.b64encode(JOB["referenznummer"].encode()).decode()
    assert "/pc/v4/jobdetails/" in detail
    assert detail.endswith(token)


def test_record_without_a_reference_is_skipped(fetched):
    postings, _ = fetched([page({**JOB, "referenznummer": None}, JOB)])
    assert len(postings) == 1


def test_duplicate_references_collapse(fetched):
    postings, _ = fetched([page(JOB, JOB)])
    assert len(postings) == 1


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------

def test_search_hits_v6(fetched):
    _, calls = fetched([page(JOB)])
    assert "/pc/v6/jobs" in calls[0][0]


def test_query_parameters_are_passed_through(fetched):
    _, calls = fetched([page(JOB)])
    params = calls[0][1]
    assert params["was"] == "Data Scientist"
    assert params["wo"] == "Düsseldorf"
    assert params["umkreis"] == 100
    assert params["angebotsart"] == aa.OFFER_KIND


def test_location_is_omitted_when_unset(fetched):
    _, calls = fetched([page(JOB)], location="", radius_km=0)
    assert "wo" not in calls[0][1]


def test_paging_stops_on_a_short_page(fetched):
    _, calls = fetched([page(JOB)])
    assert len(calls) == 1


# --------------------------------------------------------------------------
# failure propagation
# --------------------------------------------------------------------------

def test_structural_failure_survives_as_structural(fetched):
    """This is the whole point: a 404 must reach the poller as a park signal,
    not flattened into a generic RuntimeError that gets retried hourly."""
    with pytest.raises(StructuralError):
        fetched([StructuralError("HTTP 404 from /pc/v6/jobs", 404)])


def test_total_transient_failure_raises(fetched):
    with pytest.raises(Exception) as excinfo:
        fetched([TransientError("HTTP 503", 503)])
    assert not isinstance(excinfo.value, StructuralError)


def test_one_failing_query_does_not_lose_the_others(fetched):
    """Partial failure returns what worked rather than failing the source."""
    postings, _ = fetched(
        [page(JOB), TransientError("HTTP 503", 503)],
        queries=["Data Scientist", "AI Engineer"])
    assert len(postings) == 1


def test_search_urls_match_the_polled_query():
    urls = aa.search_urls(ENTRY)
    assert len(urls) == 1
    query, url = urls[0]
    assert query == "Data Scientist"
    assert "was=Data%20Scientist" in url
    assert "umkreis=100" in url
