"""The tools you reach for between cycles: discover, dedupe-check, rehydrate.

None of these run on a schedule. They are what someone types when a board has
gone quiet, when a company needs adding, or when a bug has already put bad rows
in the database — which is why the bar for them is different from the watcher's.
A poll that gets something wrong is corrected on the next one twenty minutes
later. These are run once, by hand, over the whole back catalogue, and what they
do is not polled again.

`rehydrate` gets most of the attention below, because it is the one that writes.
Two properties carry it. It must be **resumable**: a row that gets a real body
stops matching the query that selects work, so an interrupted sweep continues
rather than restarts, and a row it could not write is deferred rather than
blamed. And a body arriving must **throw the old verdict away** — the first
version of the module replaced the description and stopped there, which fixed
the record and left 185 postings holding scores read off three hundred
characters of preamble, under a log line saying `filled 88` as though it were
done.

`discover` and `dedupe_check` only read, so what matters for them is that a
miss is as informative as a hit. A discover run that finds no board prints the
slugs it tried, because the next move is passing the right one by hand; a
dedupe miss names the window it searched, because "no duplicate" over seven
days and over a year are different answers.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

from watcher import config, dedupe_check, discover, logsetup, rehydrate, store
from watcher.dedupe import ExistingApplication
from watcher.normalize import Posting
from watcher.poll import TEASER_CHARS


# --------------------------------------------------------------------------
# discover: turning a company name into a board slug
# --------------------------------------------------------------------------

class Response:
    """Enough of `requests.Response` for the five probes."""

    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


@pytest.fixture()
def boards(monkeypatch):
    """A network where each URL substring answers with whatever a test says.

    Keyed on a substring rather than the exact URL so a test names the provider
    it is exercising and not the shape of its endpoint, which is `ats.py`'s
    business and already covered there.
    """
    answers: dict[str, Response] = {}

    class Session:
        def get(self, url, timeout=None):
            for fragment, response in answers.items():
                if fragment in url:
                    return response
            return Response(404)

    monkeypatch.setattr(discover, "session", Session)
    return answers


def test_a_company_name_becomes_the_slugs_a_board_might_file_it_under() -> None:
    variants = discover.slug_variants("Zillow Group")
    assert "zillowgroup" in variants
    assert "zillow-group" in variants
    assert "zillow" in variants, "the first word alone is a common filing"
    assert "ZillowGroup" in variants, "and some providers keep the casing"


def test_the_legal_tail_is_tried_without_itself() -> None:
    """`Acme Holding GmbH` files as `acme-holding` far more often than not."""
    variants = discover.slug_variants("Acme Holding GmbH")
    assert "acmeholding" in variants
    assert "acme-holding" in variants


def test_accents_and_punctuation_are_folded_out() -> None:
    """A slug is ASCII. `Zalando SE` and `Métro & Co.` must both survive."""
    assert "metroco" in discover.slug_variants("Métro & Co.")


def test_an_extra_slug_is_tried_first() -> None:
    """It came from the careers URL, so it beats anything guessed."""
    assert discover.slug_variants("Remerge", ["remerge-gmbh"])[0] == "remerge-gmbh"


def test_a_name_with_nothing_usable_in_it_yields_no_slugs() -> None:
    assert discover.slug_variants("!!! ???") == []


def test_the_same_slug_is_not_tried_twice() -> None:
    """`Remerge` produces the same string four different ways."""
    variants = discover.slug_variants("Remerge")
    assert len(variants) == len(set(variants))


@pytest.mark.parametrize("provider, fragment, response, count", [
    ("greenhouse", "boards-api.greenhouse.io",
     Response(payload={"jobs": [{"title": "ML Engineer"}, {"title": "SRE"}]}), 2),
    ("lever", "api.lever.co",
     Response(payload=[{"text": "Data Scientist"}]), 1),
    ("ashby", "api.ashbyhq.com",
     Response(payload={"jobs": [{"title": "Analyst"}]}), 1),
    ("smartrecruiters", "api.smartrecruiters.com",
     Response(payload={"totalFound": 3, "content": [{"name": "NLP Engineer"}]}), 1),
])
def test_each_provider_is_probed_on_its_own_endpoint(
        boards, provider, fragment, response, count) -> None:
    boards[fragment] = response
    probe = discover._probe(provider, "acme", 5)
    assert probe is not None
    assert probe.provider == provider and probe.count == count


def test_personio_is_read_out_of_its_xml_feed(boards) -> None:
    """The only provider here with no JSON API at all."""
    boards["jobs.personio.de"] = Response(text=(
        "<workzag-jobs><position><name><![CDATA[ML Engineer]]></name></position>"
        "<position><name>Data Analyst</name></position></workzag-jobs>"))
    probe = discover._probe("personio", "acme", 5)
    assert probe.count == 2
    assert "ML Engineer" in probe.sample and "Data Analyst" in probe.sample


def test_a_board_that_exists_but_is_empty_is_not_a_hit(boards) -> None:
    """An empty board and a wrong slug are the same answer to the question
    being asked, which is "which one do I put in sources.toml"."""
    boards["boards-api.greenhouse.io"] = Response(payload={"jobs": []})
    assert discover._probe("greenhouse", "acme", 5) is None


def test_smartrecruiters_reporting_zero_found_is_not_a_hit(boards) -> None:
    """It answers 200 with an empty page for a company it has never heard of."""
    boards["api.smartrecruiters.com"] = Response(
        payload={"totalFound": 0, "content": []})
    assert discover._probe("smartrecruiters", "acme", 5) is None


def test_lever_answering_with_an_object_rather_than_a_list_is_not_a_hit(
        boards) -> None:
    """Its 404 body is JSON too, so the status code alone is not enough."""
    boards["api.lever.co"] = Response(payload={"error": "not found"})
    assert discover._probe("lever", "acme", 5) is None


def test_personio_answering_with_a_page_rather_than_a_feed_is_not_a_hit(
        boards) -> None:
    boards["jobs.personio.de"] = Response(text="<html>404</html>")
    assert discover._probe("personio", "acme", 5) is None


@pytest.mark.parametrize("provider, fragment", [
    ("greenhouse", "boards-api.greenhouse.io"),
    ("lever", "api.lever.co"),
    ("ashby", "api.ashbyhq.com"),
    ("smartrecruiters", "api.smartrecruiters.com"),
    ("personio", "jobs.personio.de"),
])
def test_a_404_is_simply_a_miss(boards, provider, fragment) -> None:
    boards[fragment] = Response(404)
    assert discover._probe(provider, "acme", 5) is None


def test_an_unsupported_provider_is_a_miss_not_a_crash() -> None:
    """Workday is deliberately not probed — it needs three values a slug
    cannot carry."""
    assert discover._probe("workday", "acme", 5) is None


def test_a_probe_that_raises_costs_that_probe_only(monkeypatch) -> None:
    """Sixty probes run in a pool; one refused connection must not end them."""
    class Angry:
        def get(self, *a, **k):
            raise ConnectionError("refused")

    monkeypatch.setattr(discover, "session", Angry)
    assert discover._probe("greenhouse", "acme", 5) is None


def test_the_busiest_board_is_reported_first(boards, monkeypatch) -> None:
    """Two providers can both answer for the same company — an old Lever board
    left up next to the live Greenhouse one. Volume is the tiebreak."""
    monkeypatch.setattr(discover, "_probe", lambda args_p, args_s, t: {
        ("greenhouse", "acme"): discover.Probe("greenhouse", "acme", 40, "a"),
        ("lever", "acme"): discover.Probe("lever", "acme", 2, "b"),
    }.get((args_p, args_s)))

    hits = discover.discover("Acme")
    assert [h.provider for h in hits] == ["greenhouse", "lever"]


def test_the_cli_prints_a_paste_ready_toml_block(boards, monkeypatch,
                                                  capsys) -> None:
    monkeypatch.setattr(discover, "discover",
                        lambda *a: [discover.Probe("greenhouse", "acme", 12,
                                                   "ML Engineer")])
    assert discover.main(["--company", "Acme GmbH"]) == 0
    out = capsys.readouterr().out
    assert "[[ats]]" in out
    assert 'company = "Acme GmbH"' in out
    assert 'provider = "greenhouse"' in out
    assert 'token = "acme"' in out


def test_a_miss_names_the_slugs_it_tried_and_what_to_do_next(
        boards, monkeypatch, capsys) -> None:
    """Otherwise the answer is "no" with no way to act on it — and the most
    common cause is a slug no rule would generate."""
    monkeypatch.setattr(discover, "discover", lambda *a: [])
    assert discover.main(["--company", "Remerge"]) == 1
    out = capsys.readouterr().out
    assert "No board found" in out
    assert "remerge" in out, "the slugs it tried"
    assert "--slug" in out
    assert "myworkdayjobs.com" in out, "the one case a slug cannot fix"


# --------------------------------------------------------------------------
# dedupe_check: have I applied here already
# --------------------------------------------------------------------------

def _hit(**over) -> ExistingApplication:
    fields = {"company": "Deluxe", "title": "AI Engineer",
              "applied_on": dt.date(2026, 3, 1),
              "folder": "2026/Deluxe/2026-03-01 - AI Engineer",
              "similarity": 0.94, "origin": "folder"}
    fields.update(over)
    return ExistingApplication(**fields)


def test_a_duplicate_exits_nonzero_so_a_shell_can_guard_on_it(
        monkeypatch, capsys) -> None:
    monkeypatch.setattr(dedupe_check, "find_existing", lambda *a: _hit())
    assert dedupe_check.main(["--company", "Deluxe",
                              "--role", "AI Engineer"]) == 1
    out = capsys.readouterr().out
    assert "DUPLICATE" in out
    assert "0.94" in out and "folder" in out
    assert "2026-03-01" in out


def test_a_miss_names_the_window_it_searched(monkeypatch, capsys) -> None:
    """"No duplicate" over seven days and over a year are different answers."""
    monkeypatch.setattr(dedupe_check, "find_existing", lambda *a: None)
    assert dedupe_check.main(["--company", "Acme", "--role", "Data Scientist",
                              "--lookback-days", "90"]) == 0
    out = capsys.readouterr().out
    assert "new —" in out
    assert "90 days" in out


def test_the_thresholds_come_from_config_unless_overridden(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(dedupe_check, "find_existing",
                        lambda *a: seen.append(a) or None)

    dedupe_check.main(["--company", "Acme", "--role", "X"])
    from_config = seen[-1]

    dedupe_check.main(["--company", "Acme", "--role", "X",
                       "--lookback-days", "7", "--ratio", "0.5"])
    assert seen[-1][2:] == (7, 0.5)
    assert from_config[2:] != (7, 0.5), "the default is not the override"


# --------------------------------------------------------------------------
# rehydrate: fixing what a teaser put in the database
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    return path


TEASER = "Wir suchen eine Person. " * 8          # ~190 chars
BODY = ("Senior Data Scientist. 5 Jahre Erfahrung mit Python und PyTorch. "
        "Deutsch verhandlungssicher. Unbefristet, hybrid. ") * 20


def stored(path, posting_id: str, *, provider: str = "stepstone",
           description: str = TEASER, url: str | None = None, **over) -> None:
    """One row, the way a poll cycle left it before the guard was fixed."""
    if url is None:
        url = f"https://{provider}.test/{posting_id}"
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider,
                   source_job_id, url, canonical_url, company, title, location,
                   country, remote, first_seen_at, description, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (posting_id, f"loose-{posting_id}", f"portal:{provider}", provider,
             posting_id, url,
             f"https://{provider}.test/{posting_id}",
             over.get("company", "Acme GmbH"),
             over.get("title", "Data Scientist"), "Köln", "DE", 0,
             over.get("first_seen_at", "2026-08-01T09:00:00"),
             description, over.get("raw_json", "{}")))
        conn.commit()


def verdict(path, posting_id: str, *, score: int = 71,
            created_at: str = "2026-08-01T10:00:00") -> None:
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO verdicts (posting_id, score, verdict, why_json,
                   gaps_json, stop_and_ask, stop_reason, model, created_at)
               VALUES (?,?,?,?,?,0,NULL,'haiku',?)""",
            (posting_id, score, "maybe", "[]", "[]", created_at))
        conn.commit()


def notified(path, posting_id: str, kind: str) -> None:
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO notifications (posting_id, chat_id,
                   telegram_message_id, kind, sent_at)
               VALUES (?, '-100', ?, ?, '2026-08-01T11:00:00')""",
            (posting_id, abs(hash(posting_id)) % 10000, kind))
        conn.commit()


def decided(path, posting_id: str) -> None:
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO decisions (posting_id, action, decided_at)
               VALUES (?, 'skip', '2026-08-01T12:00:00')""", (posting_id,))
        conn.commit()


@pytest.fixture()
def nowait(monkeypatch):
    """The sweep sleeps four seconds between page loads. Not here."""
    monkeypatch.setattr(rehydrate.time, "sleep", lambda _s: None)


# ---- work selection -------------------------------------------------------

def test_only_teasers_are_work(db) -> None:
    stored(db, "teaser")
    stored(db, "full", description=BODY)
    with store.connect(db) as conn:
        assert [r["id"] for r in rehydrate.candidates(conn)] == ["teaser"]


def test_only_the_two_recoverable_providers_are_work(db) -> None:
    """For everything else the detail URL is not the posting URL, and
    reconstructing it from a stored row is guesswork."""
    stored(db, "step", provider="stepstone")
    stored(db, "cafe", provider="hiringcafe")
    stored(db, "gh", provider="greenhouse")
    with store.connect(db) as conn:
        assert sorted(r["id"] for r in rehydrate.candidates(conn)) == \
            ["cafe", "step"]


def test_a_row_with_no_url_cannot_be_refetched_so_it_is_not_work(db) -> None:
    stored(db, "urlless", url="")
    with store.connect(db) as conn:
        assert rehydrate.candidates(conn) == []


def test_one_provider_can_be_swept_alone(db) -> None:
    stored(db, "step", provider="stepstone")
    stored(db, "cafe", provider="hiringcafe")
    with store.connect(db) as conn:
        rows = rehydrate.candidates(conn, provider="hiringcafe")
    assert [r["id"] for r in rows] == ["cafe"]


def test_the_newest_are_swept_first_and_a_limit_stops_there(db) -> None:
    """A sweep of two hundred pages is not always run to the end, so what it
    does first should be what is most likely to still be open."""
    for day in (1, 5, 3):
        stored(db, f"p{day}", first_seen_at=f"2026-08-0{day}T09:00:00")
    with store.connect(db) as conn:
        assert [r["id"] for r in rehydrate.candidates(conn, limit=2)] == \
            ["p5", "p3"]


def test_a_failed_posting_is_remembered_and_skipped_next_run(db) -> None:
    with store.connect(db) as conn:
        rehydrate._remember_failure(conn, "gone")
        conn.commit()
    with store.connect(db) as conn:
        assert rehydrate._failed_ids(conn) == {"gone"}


def test_unreadable_failure_memory_is_treated_as_empty(db, caplog) -> None:
    """It is a hint, not a record. Refusing to run because of it would mean a
    corrupt meta row blocking the sweep forever."""
    with store.connect(db) as conn:
        store.set_meta(conn, rehydrate.FAILED_KEY, "{not json")
        conn.commit()
    with store.connect(db) as conn, caplog.at_level(logging.WARNING):
        assert rehydrate._failed_ids(conn) == set()
    assert "not readable JSON" in caplog.text


# ---- the row -> Posting round trip ----------------------------------------

def test_the_detail_url_is_the_posting_url_for_both_providers(db) -> None:
    stored(db, "p1", provider="stepstone")
    with store.connect(db) as conn:
        row = rehydrate.candidates(conn)[0]
    posting = rehydrate._posting_from_row(row)
    assert posting.detail_url == posting.url


def test_unreadable_raw_json_costs_the_extras_not_the_row(db) -> None:
    stored(db, "p1", raw_json="{oops")
    with store.connect(db) as conn:
        row = rehydrate.candidates(conn)[0]
    assert rehydrate._posting_from_row(row).raw == {}


# ---- the write ------------------------------------------------------------

def test_a_body_arriving_rewrites_what_was_read_off_the_teaser(db) -> None:
    """The five derived columns are a cache of the description. Leaving them
    would mean a ping quoting a seniority bar from one text next to a body
    that says something else."""
    stored(db, "p1")
    verdict(db, "p1")
    with store.connect(db) as conn:
        row = rehydrate.candidates(conn)[0]
    posting = rehydrate._posting_from_row(row)
    posting.description = BODY

    with store.connect(db) as conn:
        rehydrate._write_back(conn, "p1", posting)
        conn.commit()

    with store.connect(db) as conn:
        after = conn.execute("SELECT * FROM postings WHERE id='p1'").fetchone()
        assert len(after["description"]) > TEASER_CHARS
        assert after["level"] == posting.level
        assert after["years_required"] == posting.years_required


def test_a_body_arriving_throws_the_teasers_verdict_away(db) -> None:
    """The whole re-score. `store.unscored` selects on the absence of a
    verdict, so dropping it is what makes the next cycle judge the real ad."""
    stored(db, "p1")
    verdict(db, "p1", score=88)
    with store.connect(db) as conn:
        row = rehydrate.candidates(conn)[0]
    posting = rehydrate._posting_from_row(row)
    posting.description = BODY

    with store.connect(db) as conn:
        rehydrate._write_back(conn, "p1", posting)
        conn.commit()

    with store.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM verdicts WHERE posting_id='p1'"
        ).fetchone()[0] == 0


def test_a_locked_database_is_waited_out_rather_than_failing(db, monkeypatch,
                                                              nowait) -> None:
    """A poll cycle holds its write transaction across minutes of browser I/O,
    which is longer than the 30s busy timeout. Treating that as fatal killed a
    sweep at posting 105 of 196."""
    stored(db, "p1")
    with store.connect(db) as conn:
        posting = rehydrate._posting_from_row(rehydrate.candidates(conn)[0])
    posting.description = BODY

    calls = {"n": 0}
    real = store.connect

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(rehydrate.store, "connect", flaky)
    assert rehydrate._write_with_retry("p1", posting) is True
    assert calls["n"] == 3


def test_a_lock_that_never_clears_gives_the_row_up_without_blaming_it(
        db, monkeypatch, nowait) -> None:
    """The row still holds its teaser, so it matches the work query and the
    next run picks it up. Recording it as failed would blame the ad for a lock
    held by this process's own watcher."""
    monkeypatch.setattr(rehydrate.store, "connect", _raise_locked)
    posting = Posting(source="portal:stepstone", provider="stepstone",
                      source_job_id="1", url="https://x.test/1",
                      company="A", title="B", description=BODY)
    assert rehydrate._write_with_retry("p1", posting) is False


def test_an_error_that_is_not_a_lock_is_not_retried(db, monkeypatch,
                                                     nowait) -> None:
    calls = {"n": 0}

    def broken(*a, **k):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such column: level")

    monkeypatch.setattr(rehydrate.store, "connect", broken)
    posting = Posting(source="portal:stepstone", provider="stepstone",
                      source_job_id="1", url="https://x.test/1",
                      company="A", title="B", description=BODY)
    assert rehydrate._write_with_retry("p1", posting) is False
    assert calls["n"] == 1


def _raise_locked(*a, **k):
    raise sqlite3.OperationalError("database is locked")


# ---- the sweep ------------------------------------------------------------

@pytest.fixture()
def fetches(monkeypatch):
    """What each detail URL hands back, keyed by posting id in the URL."""
    bodies: dict[str, str | Exception] = {}

    def hydrate(posting, timeout):
        answer = bodies.get(posting.source_job_id, posting.description)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rehydrate, "hydrate", hydrate)
    monkeypatch.setattr(rehydrate, "seed_profile", lambda *a: None)
    return bodies


def test_a_sweep_fills_bodies_and_reports_how_far_they_grew(db, fetches,
                                                             nowait) -> None:
    stored(db, "p1")
    stored(db, "p2")
    fetches["p1"] = BODY
    fetches["p2"] = BODY

    report = rehydrate.run(delay=0)
    assert report.considered == 2 and report.filled == 2
    assert report.failed == 0
    assert "chars avg" in report.summary()


def test_a_filled_posting_stops_being_work(db, fetches, nowait) -> None:
    """What makes the sweep resumable: an interrupted run continues rather
    than starting over."""
    stored(db, "p1")
    fetches["p1"] = BODY
    rehydrate.run(delay=0)
    assert rehydrate.run(delay=0).considered == 0


def test_a_page_that_still_returns_a_teaser_is_recorded_as_failed(
        db, fetches, nowait) -> None:
    """`hydrate` swallows its own failures and hands back what it was given,
    so a length that did not move is the failure signal, not an exception."""
    stored(db, "gone")
    report = rehydrate.run(delay=0)
    assert report.failed == 1 and report.filled == 0
    with store.connect(db) as conn:
        assert rehydrate._failed_ids(conn) == {"gone"}


def test_a_remembered_failure_is_not_fetched_again(db, fetches, nowait) -> None:
    """Otherwise a posting that has been taken down costs a page load on
    every run forever."""
    stored(db, "gone")
    rehydrate.run(delay=0)
    second = rehydrate.run(delay=0)
    assert second.skipped == 1 and second.failed == 0


def test_retry_failed_forgets_the_memory_and_tries_them_again(
        db, fetches, nowait) -> None:
    stored(db, "gone")
    rehydrate.run(delay=0)
    fetches["gone"] = BODY
    assert rehydrate.run(delay=0, retry_failed=True).filled == 1


def test_a_page_that_raises_costs_that_posting_only(db, fetches, nowait,
                                                     caplog) -> None:
    stored(db, "boom")
    stored(db, "fine")
    fetches["boom"] = TimeoutError("navigation timed out")
    fetches["fine"] = BODY

    with caplog.at_level(logging.WARNING):
        report = rehydrate.run(delay=0)
    assert report.filled == 1 and report.failed == 1
    assert "TimeoutError" in caplog.text


def test_a_body_that_cannot_be_written_is_deferred_not_failed(
        db, fetches, nowait, monkeypatch) -> None:
    stored(db, "p1")
    fetches["p1"] = BODY
    monkeypatch.setattr(rehydrate, "_write_with_retry", lambda *a: False)

    report = rehydrate.run(delay=0)
    assert report.deferred == 1 and report.failed == 0
    assert "deferred 1" in report.summary()
    with store.connect(db) as conn:
        assert rehydrate._failed_ids(conn) == set(), "not the posting's fault"


def test_losing_the_failure_note_is_not_worth_ending_the_sweep_for(
        db, fetches, nowait, monkeypatch, caplog) -> None:
    """It costs one re-fetch on the next run. A body is worth waiting out a
    poll cycle for; a note is not."""
    stored(db, "gone")
    monkeypatch.setattr(rehydrate.store, "connect", _locked_once(db))

    with caplog.at_level(logging.WARNING):
        report = rehydrate.run(delay=0)
    assert report.failed == 1
    assert "could not record the failure" in caplog.text


def _locked_once(path):
    """`store.connect` that refuses exactly the failure-note write."""
    real = store.connect
    seen = {"n": 0}

    def connect(*a, **k):
        seen["n"] += 1
        if seen["n"] == 3:      # 1: select work, 2: (none), 3: the note
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    return connect


def test_a_dry_run_fetches_nothing_and_writes_nothing(db, fetches, nowait,
                                                       caplog) -> None:
    stored(db, "p1")
    fetches["p1"] = BODY
    with caplog.at_level(logging.INFO):
        report = rehydrate.run(dry_run=True, delay=0)
    assert report.filled == 0 and report.considered == 1
    assert "would fetch" in caplog.text
    with store.connect(db) as conn:
        assert rehydrate.candidates(conn), "still work after a dry run"


def test_nothing_to_do_returns_before_touching_the_browser(db, monkeypatch,
                                                           nowait) -> None:
    """The ordinary outcome once the sweep has been run once. Copying a
    profile for it would be a few hundred megabytes of nothing."""
    seeded: list = []
    monkeypatch.setattr(rehydrate, "seed_profile",
                        lambda *a: seeded.append(a))
    assert rehydrate.run(delay=0).considered == 0
    assert seeded == []


# ---- the browser profile --------------------------------------------------

def test_a_profile_is_copied_so_two_chromiums_do_not_share_one(tmp_path) -> None:
    source = tmp_path / "browser"
    (source / "Default").mkdir(parents=True)
    (source / "Default" / "Cookies").write_bytes(b"jar")
    target = tmp_path / "browser-rehydrate"

    rehydrate.seed_profile(source, target)
    assert (target / "Default" / "Cookies").read_bytes() == b"jar"


def test_an_existing_copy_is_reused_rather_than_refreshed(tmp_path,
                                                           caplog) -> None:
    """It holds whatever challenge state the last sweep accumulated, and a
    second run starting warm is worth a few dozen megabytes."""
    source, target = tmp_path / "browser", tmp_path / "copy"
    source.mkdir()
    (source / "new").write_text("x")
    target.mkdir()

    with caplog.at_level(logging.INFO):
        rehydrate.seed_profile(source, target)
    assert not (target / "new").exists()
    assert "reusing" in caplog.text


def test_no_profile_to_copy_starts_cold_rather_than_failing(tmp_path,
                                                             caplog) -> None:
    with caplog.at_level(logging.INFO):
        rehydrate.seed_profile(tmp_path / "absent", tmp_path / "copy")
    assert "starting cold" in caplog.text


def test_files_held_open_by_the_live_browser_are_skipped_and_named(
        tmp_path, monkeypatch, caplog) -> None:
    """copytree copies everything it can and raises once at the end with the
    list. A profile missing a lock file still carries the cookie jar."""
    source = tmp_path / "browser"
    source.mkdir()
    monkeypatch.setattr(shutil, "copytree", _copytree_partial)

    with caplog.at_level(logging.INFO):
        rehydrate.seed_profile(source, tmp_path / "copy")
    assert "were in use" in caplog.text
    assert "LOCK" in caplog.text


def _copytree_partial(src, dst, dirs_exist_ok=False):
    raise shutil.Error([(str(Path(src) / "LOCK"), "dst", "in use")])


# ---- the back catalogue ---------------------------------------------------

def test_a_verdict_older_than_the_body_it_judged_is_work(db) -> None:
    stored(db, "old", description=BODY)
    verdict(db, "old", created_at="2026-08-01T10:00:00")
    stored(db, "new", description=BODY)
    verdict(db, "new", created_at="2026-08-05T20:00:00")

    with store.connect(db) as conn:
        rows = rehydrate.scored_on_a_teaser(conn, "2026-08-05T15:18:21")
    assert [r["id"] for r in rows] == ["old"]


def test_a_posting_still_holding_a_teaser_is_the_sweeps_job_not_this_one(
        db) -> None:
    stored(db, "teaser")
    verdict(db, "teaser", created_at="2026-08-01T10:00:00")
    with store.connect(db) as conn:
        assert rehydrate.scored_on_a_teaser(conn, "2026-08-05T15:18:21") == []


def test_a_never_messaged_posting_only_needs_its_verdict_dropped(db) -> None:
    """If the real body scores high the next cycle pings, which is the
    ordinary path."""
    stored(db, "quiet", description=BODY)
    verdict(db, "quiet")
    report = rehydrate.rescore_before("2026-08-05T15:18:21")
    assert report.verdicts == 1 and report.pings == 0
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


def test_a_digested_posting_is_unmuted_as_well(db) -> None:
    """`unnotified_in_band` excludes anything with a notifications row, so
    re-scoring alone would correct the record and change nothing the user
    ever sees."""
    stored(db, "digested", description=BODY)
    verdict(db, "digested")
    notified(db, "digested", "digest")

    report = rehydrate.rescore_before("2026-08-05T15:18:21")
    assert report.verdicts == 1 and report.pings == 1
    with store.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


def test_a_posting_already_pinged_about_is_left_alone(db) -> None:
    """A second ping for a score that moved is noise."""
    stored(db, "pinged", description=BODY)
    verdict(db, "pinged")
    notified(db, "pinged", "instant")

    report = rehydrate.rescore_before("2026-08-05T15:18:21")
    assert report.kept_instant == 1 and report.pings == 0
    assert "already pinged" in report.summary()


def test_a_posting_already_decided_on_is_left_alone(db) -> None:
    """Re-offering something the user skipped is worse than noise."""
    stored(db, "skipped", description=BODY)
    verdict(db, "skipped")
    notified(db, "skipped", "digest")
    decided(db, "skipped")

    report = rehydrate.rescore_before("2026-08-05T15:18:21")
    assert report.kept_decided == 1
    assert report.pings == 0, "a decided posting is not unmuted either"
    assert "already decided" in report.summary()


def test_a_rescore_dry_run_counts_without_changing_anything(db) -> None:
    stored(db, "quiet", description=BODY)
    verdict(db, "quiet")
    report = rehydrate.rescore_before("2026-08-05T15:18:21", dry_run=True)
    assert report.verdicts == 1
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1


def test_the_rescore_log_lists_a_sample_and_counts_the_rest(db, caplog) -> None:
    for i in range(14):
        stored(db, f"p{i:02d}", description=BODY)
        verdict(db, f"p{i:02d}", score=90 - i)
    with caplog.at_level(logging.INFO):
        rehydrate.rescore_before("2026-08-05T15:18:21", dry_run=True)
    assert "and 4 more" in caplog.text


def test_a_rescore_can_be_limited_to_one_provider(db) -> None:
    stored(db, "step", provider="stepstone", description=BODY)
    verdict(db, "step")
    stored(db, "cafe", provider="hiringcafe", description=BODY)
    verdict(db, "cafe")

    report = rehydrate.rescore_before("2026-08-05T15:18:21",
                                      provider="stepstone", dry_run=True)
    assert report.verdicts == 1


# ---- the CLI --------------------------------------------------------------

def test_the_rescore_flag_is_its_own_mode(monkeypatch, caplog) -> None:
    """It touches no network, so it wants no browser profile, no delay, and
    none of the sweep's other machinery."""
    seen: list = []
    monkeypatch.setattr(rehydrate, "rescore_before",
                        lambda *a, **k: seen.append((a, k)) or rehydrate.Rescore())
    monkeypatch.setattr(rehydrate, "run", _never_called)
    monkeypatch.setattr(rehydrate, "setup", lambda *a: None)
    monkeypatch.delenv(config.BROWSER_PROFILE_ENV, raising=False)

    assert rehydrate.main(["--rescore-before", "2026-08-05T15:18:21"]) == 0
    assert seen[0][0] == ("2026-08-05T15:18:21",)
    assert config.BROWSER_PROFILE_ENV not in __import__("os").environ


def test_the_sweep_points_the_browser_at_its_own_profile(monkeypatch) -> None:
    """The watcher owns state/browser/ for as long as it runs, and two
    Chromium processes cannot share one profile."""
    monkeypatch.setattr(rehydrate, "run", lambda **k: rehydrate.Report())
    monkeypatch.setattr(rehydrate, "setup", lambda *a: None)
    assert rehydrate.main([]) == 0
    import os
    assert os.environ[config.BROWSER_PROFILE_ENV] == str(rehydrate.PROFILE_COPY)


def test_the_cli_passes_every_flag_through(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(rehydrate, "run",
                        lambda **k: seen.append(k) or rehydrate.Report())
    monkeypatch.setattr(rehydrate, "setup", lambda *a: None)

    rehydrate.main(["--dry-run", "--provider", "hiringcafe", "--limit", "5",
                    "--delay", "0.5", "--timeout", "10", "--retry-failed",
                    "--verbose"])
    assert seen[0] == {"dry_run": True, "provider": "hiringcafe", "limit": 5,
                       "delay": 0.5, "retry_failed": True, "timeout": 10}


def _never_called(*a, **k):
    raise AssertionError("the sweep must not run in rescore mode")


# --------------------------------------------------------------------------
# logging setup
# --------------------------------------------------------------------------

def test_a_console_that_cannot_be_reconfigured_is_left_as_it_is() -> None:
    """`force_utf8` runs at the top of every CLI, including ones whose stdout
    has been replaced by a pytest capture object with no `reconfigure`."""
    class Fixed:
        def reconfigure(self, **k):
            raise ValueError("underlying stream is detached")

    import sys
    real = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = Fixed()
    try:
        logsetup.force_utf8()       # must not raise
    finally:
        sys.stdout, sys.stderr = real


def test_setup_is_configured_once_however_many_clis_call_it(monkeypatch) -> None:
    """Two handlers means every line logged twice."""
    monkeypatch.setattr(logsetup, "_CONFIGURED", False)
    monkeypatch.setattr(logsetup, "ensure_dirs", lambda: None)

    logger = logsetup.setup(to_file=False)
    count = len(logger.handlers)
    logsetup.setup(to_file=False)
    assert len(logger.handlers) == count
    for handler in logger.handlers[-1:]:
        logger.removeHandler(handler)
