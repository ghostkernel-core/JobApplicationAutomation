"""Generated portal queries: what they may contain, and how they fail.

The query set is the aperture of the whole watcher — nothing any later stage
does can recover a posting that was never fetched. That cuts both ways, and
both directions are pinned down here.

Widening is the point, so a generated set has to be allowed to replace the
hand-written one. But every failure mode of this stage is *silent*: a location
word inside the query text double-filters against the portal's own location
field and returns an empty page that reads exactly like a quiet day, and a
malformed query can 4xx a fragile portal into being parked with no automatic
retry. So the tests that matter most are the ones asserting that a bad
generation changes nothing at all.
"""

from __future__ import annotations

import json

import pytest

from watcher import profile, queries, store
from watcher.claude_cli import ClaudeError
from watcher.config import Config

DIGEST = "A data scientist with computer-vision experience." * 5

PORTALS = [
    {"name": "arbeitsagentur", "enabled": True,
     "queries": ["Data Scientist", "Machine Learning Engineer"]},
    {"name": "hiringcafe", "enabled": True,
     "queries": ["AI Engineer", "Data Scientist"]},
]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    store.init_db(path)
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No cache file, no digest call, no model call leaks between tests."""
    monkeypatch.setattr(queries, "SEARCH_QUERIES_PATH",
                        tmp_path / "search_queries.json")
    monkeypatch.setattr(profile, "get_digest", lambda *a, **k: DIGEST)
    monkeypatch.setattr(queries, "run_json",
                        lambda *a, **k: pytest.fail("run_json was not patched"))
    monkeypatch.setattr(queries, "load_sources",
                        lambda *a, **k: pytest.fail("load_sources not patched"))
    # `_FAILED` is process-global on purpose — it is what stops a broken model
    # being retried once per portal per cycle. That makes it leak across tests.
    queries._FAILED.clear()
    yield
    queries._FAILED.clear()


def config(**overrides) -> Config:
    return Config(queries={"enabled": True, "per_portal": 4, **overrides})


def sources(monkeypatch, entries=None):
    class _Sources:
        portals = list(entries if entries is not None else PORTALS)

        def enabled_portals(self):
            return [e for e in self.portals if e.get("enabled", True)]

    monkeypatch.setattr(queries, "load_sources", lambda *a, **k: _Sources())


def responds(monkeypatch, portals, calls=None):
    def fake(prompt, **kwargs):
        if calls is not None:
            calls.append(prompt)
        return {"portals": portals}
    monkeypatch.setattr(queries, "run_json", fake)


# --------------------------------------------------------------------------
# validation — the silent failure modes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "Data Scientist",
    "Machine Learning Engineer",
    "Computer Vision Engineer",
    "Wissenschaftlicher Mitarbeiter Datenanalyse",
    "Applied Scientist Search Ranking",
])
def test_a_plain_job_title_is_accepted(query):
    assert queries.rejection(query) == ""


@pytest.mark.parametrize("query", [
    "Data Scientist Berlin",
    "Machine Learning Engineer Germany",
    "Remote Data Scientist",
    "Data Scientist Deutschland",
    "AI Engineer München",
    "Data Scientist Europe",
])
def test_a_location_term_is_rejected(query):
    """Every portal takes location as its own field. A city in the query text
    filters twice and returns an empty page that looks like a quiet day —
    which is why this is a hard reject and not a warning."""
    assert "location term" in queries.rejection(query)


@pytest.mark.parametrize("query", [
    'Data Scientist OR "ML Engineer"',
    "Data AND Scientist",
    "Data Scientist (Senior)",
    "Machine Learning*",
    "Data Scientist -junior",
    "Datenwissenschaftler und Analyst",
])
def test_boolean_and_punctuation_are_rejected(query):
    """A structural 4xx parks a fragile source with no automatic retry, so one
    unbalanced quote can take StepStone off the board until someone reads a
    status page."""
    assert queries.rejection(query) != ""


def test_a_single_word_is_too_broad_and_a_sentence_too_narrow():
    assert "words" in queries.rejection("Scientist")
    assert "words" in queries.rejection(
        "Senior Lead Machine Learning Research Engineer Team")


def test_an_overlong_query_is_rejected_even_within_the_word_count():
    assert queries.rejection("Verantwortlicher Datenverarbeitungswissenschaftler "
                             "Unternehmensbereich") != ""


def test_a_non_string_is_rejected_rather_than_coerced():
    assert queries.rejection(None) != ""
    assert queries.rejection(42) != ""
    assert queries.rejection(["Data Scientist"]) != ""


def test_the_reason_says_which_rule_was_broken():
    """`--explain` and the log both print this, and "the model is bad at this"
    and "one word is missing from _GEO_WORDS" need different fixes."""
    assert "location term" in queries.rejection("Data Scientist Hamburg")
    assert "operator" in queries.rejection('Data "Scientist"')


# --------------------------------------------------------------------------
# generate — per-portal failback
# --------------------------------------------------------------------------

def test_valid_sets_are_returned_per_portal(monkeypatch):
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    out = queries.generate(config(), PORTALS, DIGEST)
    assert set(out) == {"arbeitsagentur", "hiringcafe"}


def test_one_bad_portal_does_not_take_the_other_down(monkeypatch):
    """The two halves are asked for different languages and fail
    independently, so the fallback has to be per portal."""
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist Berlin", "Remote AI Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    out = queries.generate(config(), PORTALS, DIGEST)
    assert "arbeitsagentur" not in out
    assert out["hiringcafe"] == ["AI Engineer", "Applied Scientist"]


def test_one_surviving_query_is_not_a_query_set(monkeypatch):
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Data Scientist Berlin"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    out = queries.generate(config(), PORTALS, DIGEST)
    assert "arbeitsagentur" not in out


def test_duplicates_do_not_count_toward_the_minimum(monkeypatch):
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "data scientist", "DATA SCIENTIST"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    out = queries.generate(config(), PORTALS, DIGEST)
    assert "arbeitsagentur" not in out


def test_a_failed_call_generates_nothing_rather_than_something(monkeypatch):
    monkeypatch.setattr(queries, "run_json", lambda *a, **k: (
        _ for _ in ()).throw(ClaudeError("upstream 503")))
    assert queries.generate(config(), PORTALS, DIGEST) == {}


def test_a_response_without_a_portals_object_generates_nothing(monkeypatch):
    monkeypatch.setattr(queries, "run_json", lambda *a, **k: {"queries": []})
    assert queries.generate(config(), PORTALS, DIGEST) == {}


def test_a_portal_missing_from_the_response_keeps_its_own_list(monkeypatch):
    responds(monkeypatch, {"hiringcafe": ["AI Engineer", "Applied Scientist"]})
    out = queries.generate(config(), PORTALS, DIGEST)
    assert "arbeitsagentur" not in out


def test_the_prompt_carries_the_digest_and_asks_each_portal_for_a_language(
        monkeypatch):
    calls: list[str] = []
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    }, calls)
    queries.generate(config(), PORTALS, DIGEST)
    prompt = calls[0]
    assert DIGEST in prompt
    assert "arbeitsagentur: write these in German" in prompt
    assert "hiringcafe: write these in English" in prompt


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------

def test_an_unchanged_digest_costs_no_second_call(db, monkeypatch):
    sources(monkeypatch)
    calls: list[str] = []
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    }, calls)

    first = queries.get_queries(config())
    second = queries.get_queries(config())

    assert first == second
    assert len(calls) == 1


def test_an_edited_profile_regenerates(db, monkeypatch):
    sources(monkeypatch)
    calls: list[str] = []
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    }, calls)
    queries.get_queries(config())

    monkeypatch.setattr(profile, "get_digest", lambda *a, **k: DIGEST + " Also Rust.")
    queries.get_queries(config())

    assert len(calls) == 2


def test_the_cache_key_ignores_a_profile_edit_that_condenses_the_same():
    assert queries.cache_key(DIGEST) == queries.cache_key(DIGEST)
    assert queries.cache_key(DIGEST) != queries.cache_key(DIGEST + "x")


def test_a_failed_generation_is_not_retried_for_the_same_digest(db, monkeypatch):
    """Three portals, an hourly poll and a broken model would otherwise be
    three wasted calls an hour for as long as it stayed broken."""
    sources(monkeypatch)
    calls: list[int] = []

    def boom(*a, **k):
        calls.append(1)
        raise ClaudeError("upstream 503")
    monkeypatch.setattr(queries, "run_json", boom)

    assert queries.get_queries(config()) == {}
    assert queries.get_queries(config()) == {}
    assert len(calls) == 1


def test_a_later_profile_edit_gets_a_fresh_attempt(db, monkeypatch):
    sources(monkeypatch)
    calls: list[int] = []

    def boom(*a, **k):
        calls.append(1)
        raise ClaudeError("upstream 503")
    monkeypatch.setattr(queries, "run_json", boom)

    queries.get_queries(config())
    monkeypatch.setattr(profile, "get_digest", lambda *a, **k: DIGEST + " Also Rust.")
    queries.get_queries(config())

    assert len(calls) == 2


def test_status_never_spends_a_model_call(db, monkeypatch):
    """Not just the generation call — the digest one too.

    `profile.get_digest` regenerates through the model whenever the canonical
    profile has changed, so a read-only path that consults it to check
    freshness costs a call on exactly the run where somebody has just edited
    their profile and is looking at a status page to see what changed.
    """
    sources(monkeypatch)
    monkeypatch.setattr(profile, "get_digest",
                        lambda *a, **k: pytest.fail("status regenerated the digest"))
    assert queries.get_queries(config(), allow_generate=False) == {}


def test_status_shows_the_cached_set_without_revalidating_it(db, monkeypatch):
    """The cost of the test above: a profile edited since the last poll shows
    the previous query set for one cycle. That is the intended trade."""
    sources(monkeypatch)
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    queries.get_queries(config())

    monkeypatch.setattr(profile, "get_digest",
                        lambda *a, **k: pytest.fail("status regenerated the digest"))
    stale = queries.get_queries(config(), allow_generate=False)
    assert stale["hiringcafe"] == ["AI Engineer", "Applied Scientist"]


def test_a_corrupt_cache_file_falls_back_instead_of_raising(db, monkeypatch):
    sources(monkeypatch)
    queries.SEARCH_QUERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    queries.SEARCH_QUERIES_PATH.write_text("{not json", encoding="utf-8")
    assert queries.get_queries(config(), allow_generate=False) == {}


def test_the_cache_file_is_readable_json_with_its_key(db, monkeypatch):
    sources(monkeypatch)
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    queries.get_queries(config())

    data = json.loads(queries.SEARCH_QUERIES_PATH.read_text(encoding="utf-8"))
    assert data["key"] == queries.cache_key(DIGEST)
    assert data["portals"]["hiringcafe"] == ["AI Engineer", "Applied Scientist"]
    with store.connect(db) as conn:
        assert store.get_meta(conn, "search_queries_key") == data["key"]


def test_a_missing_digest_is_not_a_poll_failure(db, monkeypatch):
    sources(monkeypatch)
    monkeypatch.setattr(profile, "get_digest", lambda *a, **k: (
        _ for _ in ()).throw(FileNotFoundError("no canonical profile")))
    assert queries.get_queries(config()) == {}


# --------------------------------------------------------------------------
# for_entry — the substitution the poll actually makes
# --------------------------------------------------------------------------

def test_for_entry_returns_a_new_dict_and_leaves_the_original_alone(
        db, monkeypatch):
    """`_Reloader` caches the parsed Sources and hands the same dict objects to
    every caller. Writing into one here would leave generated queries in place
    after the feature was switched back off."""
    sources(monkeypatch)
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist", "Machine Learning Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    entry = dict(PORTALS[1])
    before = dict(entry)

    swapped = queries.for_entry(entry, config())

    assert swapped is not entry
    assert entry == before
    assert swapped["queries"] == ["AI Engineer", "Applied Scientist"]


def test_for_entry_is_a_no_op_when_the_feature_is_off(db, monkeypatch):
    entry = dict(PORTALS[0])
    assert queries.for_entry(entry, Config()) is entry


def test_for_entry_leaves_an_ats_board_alone(db, monkeypatch):
    entry = {"kind": "ats", "provider": "greenhouse", "company": "Acme"}
    assert queries.for_entry(entry, config()) is entry


def test_a_portal_that_failed_validation_keeps_its_own_queries(db, monkeypatch):
    sources(monkeypatch)
    responds(monkeypatch, {
        "arbeitsagentur": ["Data Scientist Berlin", "Remote AI Engineer"],
        "hiringcafe": ["AI Engineer", "Applied Scientist"],
    })
    entry = dict(PORTALS[0])
    swapped = queries.for_entry(entry, config())
    assert swapped is entry
    assert swapped["queries"] == ["Data Scientist", "Machine Learning Engineer"]
