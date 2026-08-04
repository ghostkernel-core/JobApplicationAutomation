"""Opt-in checks against the real Jobsuche API.

    pytest automation/tests -m live

Skipped by default: CI must not fail because a third party is having a bad
morning, and the offline suite already pins the payload mapping. Run this when
the arbeitsagentur source parks itself — it answers "did the endpoint move
again?" directly, which is the first question worth asking.
"""

from __future__ import annotations

import pytest

from watcher.fetchers import arbeitsagentur as aa

pytestmark = pytest.mark.live

ENTRY = {"name": "arbeitsagentur", "location": "Düsseldorf", "radius_km": 100,
         "queries": ["Data Scientist"]}


@pytest.fixture(scope="module")
def postings():
    found = aa.fetch(ENTRY, 30)
    if not found:
        pytest.fail("v6 search returned nothing — the endpoint or the query "
                    "shape has changed again")
    return found


def test_search_returns_postings(postings):
    assert len(postings) > 5


def test_every_posting_has_the_fields_the_pipeline_needs(postings):
    for posting in postings:
        assert posting.company, f"no company: {posting.source_job_id}"
        assert posting.title, f"no title: {posting.source_job_id}"
        assert posting.source_job_id
        assert posting.url.startswith("http")


def test_most_postings_carry_a_location(postings):
    located = [p for p in postings if p.location]
    assert len(located) > len(postings) * 0.8


def test_dates_are_parsed(postings):
    dated = [p for p in postings if p.posted_at]
    assert len(dated) > len(postings) * 0.8


def test_hydrate_returns_a_body(postings):
    bodies = [aa.hydrate(p, 30) for p in postings[:3]]
    assert any(len(b) > 200 for b in bodies), \
        "no posting body came back — the v4 jobdetails endpoint may have moved"
