"""The poll funnel, split by source.

The totals answer "are listings reaching the scorer". They cannot answer the
question people actually ask, which is "why is everything coming from one
board" — and that question has two very different answers that the totals
render identical: a source returning nothing, and a source returning its whole
catalogue and having all of it filtered.

Both look like `0 new`. Only the per-source row tells them apart, so the row
has to exist even for a source that contributed nothing at all — the empty ones
are the interesting ones.
"""

from __future__ import annotations

import datetime as dt

import pytest

from watcher import notifier as notifier_module
from watcher import poll as poll_module
from watcher import store
from watcher.config import Config, SourceDefaults, Sources
from watcher.normalize import Posting
from watcher.notifier import (MAX_STATUS_SOURCES, TELEGRAM_MAX_CHARS,
                              format_status, funnel_rows)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(notifier_module, "DB_PATH", path)
    store.init_db(path)
    return path


def _posting(source: str, job_id: str, title: str = "Data Scientist",
             description: str = "") -> Posting:
    """One posting, distinct from every other unless a test says otherwise.

    Employer and role are varied per source and per id on purpose: `loose_key`
    collapses the same role at the same employer across sources, which is right
    for the poller and would silently reduce a test's fixture to one row.
    """
    return Posting(source=source, provider="greenhouse", source_job_id=job_id,
                   url=f"https://example.com/{source}/{job_id}",
                   company=f"Acme {source}", title=f"{title} {job_id}",
                   location="Berlin", country="Germany",
                   description=description or "Python and SQL. " * 80)


def _sources(*names: str) -> Sources:
    defaults = SourceDefaults(countries=("DE",),
                              title_allow=("data scientist",),
                              title_deny=())
    return Sources(defaults=defaults,
                   ats=tuple({"company": n, "provider": "greenhouse",
                              "board": n.lower()} for n in names),
                   portals=())


@pytest.fixture
def fake_boards(monkeypatch):
    """Let a test say what each board returns, by source key."""
    catalogue: dict[str, list[Posting] | Exception] = {}

    def fetch(entry, timeout):
        key = f"ats:{entry['company']}"
        found = catalogue.get(key, [])
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(poll_module, "fetch_source", fetch)
    return catalogue


# --------------------------------------------------------------------------
# counting
# --------------------------------------------------------------------------

def test_each_stage_is_attributed_to_its_own_source(db, fake_boards) -> None:
    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", "1")]
    fake_boards["ats:Beta"] = [_posting("ats:Beta", "1"),
                               _posting("ats:Beta", "2")]

    report = poll_module.poll_once(config=Config(),
                                   sources=_sources("Alpha", "Beta"))

    assert report.by_source["ats:Alpha"].fetched == 1
    assert report.by_source["ats:Alpha"].stored == 1
    assert report.by_source["ats:Beta"].fetched == 2
    assert report.by_source["ats:Beta"].stored == 2


def test_a_source_that_returned_nothing_still_gets_a_row(db, fake_boards) -> None:
    """The whole point: an absent board must not be absent from the report."""
    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", "1")]
    fake_boards["ats:Quiet"] = []

    report = poll_module.poll_once(config=Config(),
                                   sources=_sources("Alpha", "Quiet"))

    assert "ats:Quiet" in report.by_source
    assert report.by_source["ats:Quiet"].fetched == 0
    assert report.by_source["ats:Quiet"].stored == 0


def test_a_fetched_but_filtered_board_is_not_the_same_as_a_silent_one(
        db, fake_boards) -> None:
    """`0 new` from 300 listings and `0 new` from none are different faults."""
    fake_boards["ats:Loud"] = [_posting("ats:Loud", str(i), title="Chef")
                               for i in range(5)]
    fake_boards["ats:Silent"] = []

    report = poll_module.poll_once(config=Config(),
                                   sources=_sources("Loud", "Silent"))

    loud, silent = report.by_source["ats:Loud"], report.by_source["ats:Silent"]
    assert loud.stored == 0 and silent.stored == 0        # identical in totals
    assert loud.fetched == 5 and loud.filtered == 5       # but not here
    assert silent.fetched == 0 and silent.filtered == 0


def test_already_seen_is_attributed_on_the_second_poll(db, fake_boards) -> None:
    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", "1")]
    sources = _sources("Alpha")

    poll_module.poll_once(config=Config(), sources=sources)
    again = poll_module.poll_once(config=Config(), sources=sources)

    assert again.by_source["ats:Alpha"].already_known == 1
    assert again.by_source["ats:Alpha"].stored == 0


def test_a_failed_fetch_says_so_instead_of_reporting_zeros(db, fake_boards) -> None:
    fake_boards["ats:Broken"] = RuntimeError("boom")
    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", "1")]

    report = poll_module.poll_once(config=Config(),
                                   sources=_sources("Alpha", "Broken"))

    assert "boom" in report.by_source["ats:Broken"].error
    assert report.by_source["ats:Broken"].fetched == 0


def test_the_per_source_counts_add_up_to_the_totals(db, fake_boards) -> None:
    """A funnel that disagrees with the headline is worse than none."""
    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", str(i)) for i in range(3)]
    fake_boards["ats:Beta"] = [_posting("ats:Beta", "1", title="Chef"),
                               _posting("ats:Beta", "2")]

    report = poll_module.poll_once(config=Config(),
                                   sources=_sources("Alpha", "Beta"))
    counts = report.by_source.values()

    assert sum(c.fetched for c in counts) == report.fetched
    assert sum(c.already_known for c in counts) == report.already_known
    assert sum(c.filtered for c in counts) == report.filtered
    assert sum(c.stored for c in counts) == report.stored


def test_a_dry_run_attributes_what_it_would_have_stored(db, fake_boards) -> None:
    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", "1")]

    report = poll_module.poll_once(dry_run=True, config=Config(),
                                   sources=_sources("Alpha"))

    assert report.by_source["ats:Alpha"].stored == 1
    assert report.stored == 1


def test_source_stats_is_json_safe(db, fake_boards) -> None:
    """It is persisted through `save_cycle`, which json-encodes it."""
    import json

    fake_boards["ats:Alpha"] = [_posting("ats:Alpha", "1")]
    report = poll_module.poll_once(config=Config(), sources=_sources("Alpha"))

    stats = report.source_stats()
    assert json.loads(json.dumps(stats))["ats:Alpha"]["stored"] == 1


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------

def _stats(**by_source) -> dict:
    return {key: dict(counts) for key, counts in by_source.items()}


def test_failures_sort_above_everything_else() -> None:
    rows = funnel_rows(_stats(
        good={"fetched": 10, "stored": 9},
        broken={"fetched": 0, "stored": 0, "error": "HTTPError: 503"},
    ))
    assert rows[0][0] == "broken"


def test_the_productive_sources_lead(db) -> None:
    rows = funnel_rows(_stats(
        quiet={"fetched": 900, "stored": 0},
        busy={"fetched": 20, "stored": 7},
    ))
    assert [name for name, *_ in rows] == ["busy", "quiet"]


def test_volume_breaks_a_tie_on_new(db) -> None:
    rows = funnel_rows(_stats(
        small={"fetched": 5, "stored": 0},
        large={"fetched": 900, "stored": 0},
    ))
    assert [name for name, *_ in rows] == ["large", "small"]


def test_a_malformed_entry_is_skipped_not_fatal() -> None:
    rows = funnel_rows({"good": {"fetched": 1}, "bad": "not a dict"})
    assert [name for name, *_ in rows] == ["good"]


# --------------------------------------------------------------------------
# the /status block
# --------------------------------------------------------------------------

def _cycle(**overrides) -> dict:
    now = dt.datetime.now()
    stats = {
        "started_at": (now - dt.timedelta(minutes=12)).isoformat(timespec="seconds"),
        "finished_at": (now - dt.timedelta(minutes=11)).isoformat(timespec="seconds"),
        "seconds": 41.2,
        "fetched": 300, "already_known": 290, "filtered": 8, "stored": 2,
        "scored": 2, "deferred": 0, "notified": 1, "sources_failed": [],
        "sources": {
            "portal:stepstone": {"fetched": 277, "already_known": 270,
                                 "filtered": 5, "stored": 2},
            "ats:Quiet": {"fetched": 23, "already_known": 20,
                          "filtered": 3, "stored": 0},
        },
    }
    stats.update(overrides)
    return stats


def test_status_shows_the_funnel_per_source(db) -> None:
    text = format_status(Config(), _cycle(), {}, None)
    assert "Per source" in text
    assert "portal:stepstone" in text
    assert "ats:Quiet" in text


def test_a_cycle_recorded_before_this_existed_shows_no_table(db) -> None:
    """An empty table would read as "no sources configured"."""
    cycle = _cycle()
    del cycle["sources"]
    text = format_status(Config(), cycle, {}, None)
    assert "Per source" not in text


def test_a_failed_source_reads_as_failed_not_as_empty(db) -> None:
    cycle = _cycle(sources={"ats:Broken": {"fetched": 0, "already_known": 0,
                                           "filtered": 0, "stored": 0,
                                           "error": "HTTPError: 503"}})
    text = format_status(Config(), cycle, {}, None)
    assert "failed" in text
    assert "503" in text


def test_the_table_is_monospace(db) -> None:
    """Proportional font turns aligned columns into a ragged mess on a phone."""
    text = format_status(Config(), _cycle(), {}, None)
    assert "<pre>" in text and "</pre>" in text


def test_a_source_name_with_markup_is_escaped(db) -> None:
    cycle = _cycle(sources={"ats:<b>x</b>": {"fetched": 1, "stored": 0}})
    text = format_status(Config(), cycle, {}, None)
    assert "&lt;b&gt;" in text


def test_a_long_list_is_capped_and_says_so(db) -> None:
    many = {f"ats:Board{i:02d}": {"fetched": i, "already_known": 0,
                                  "filtered": 0, "stored": 0}
            for i in range(MAX_STATUS_SOURCES + 6)}
    text = format_status(Config(), _cycle(sources=many), {}, None)
    assert "+6 more" in text


def test_the_report_still_fits_in_one_telegram_message(db) -> None:
    """Telegram rejects an over-length message outright rather than trimming."""
    many = {f"portal:very-long-source-name-{i:02d}":
            {"fetched": 99999, "already_known": 99999,
             "filtered": 99999, "stored": 99999}
            for i in range(200)}
    text = format_status(Config(), _cycle(sources=many), {}, None)
    assert len(text) <= TELEGRAM_MAX_CHARS


def test_dropping_the_funnel_keeps_the_rest_of_the_report(db) -> None:
    """The funnel is the newest part, and the first thing to go when it must."""
    many = {f"portal:very-long-source-name-{i:02d}":
            {"fetched": 99999, "already_known": 99999,
             "filtered": 99999, "stored": 99999}
            for i in range(200)}
    text = format_status(Config(), _cycle(sources=many), {}, None)
    assert "300 fetched" in text
    assert "Sources:" in text
