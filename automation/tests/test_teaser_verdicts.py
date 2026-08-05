"""A posting judged on its teaser must be judged again on its body.

`poll.py` used to hydrate only postings whose description came back empty.
stepstone and hiringcafe both ship the search tile's one-line snippet — around
three hundred characters, non-empty — so their bodies were never fetched, and
the first couple of hundred postings were scored on the opening sentence of the
ad. `rehydrate.py` was written to apply the fix backwards and it did half the
job: it replaced the descriptions and left every verdict in place. The record
then read as though those postings had been judged, at scores read off text
nobody had, and the sweep logged `filled 88` as if it were finished.

So there are two things to hold down. A body arriving must take the old verdict
with it, for ever, without anyone remembering to ask — that is the half that
cannot recur. And the rows filled before that was true have to be re-queued
once, which is a different problem, because by then some of them had already
been messaged about and a few had been decided on.
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from watcher import rehydrate, store
from watcher.normalize import Posting


CUTOFF = "2026-08-05T15:18:21"
BEFORE, AFTER = "2026-08-03T13:43:00", "2026-08-05T17:04:00"

TEASER = "Join our team as a Data Engineer. " * 8          # ~270 chars
BODY = "We are looking for a data engineer. " * 300        # well past the guard


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """A real database, with the module-level helpers pointed at it.

    `rescore_before` opens its own connection, so the one a test sets its rows
    up on must not be sitting on them inside a transaction — hence autocommit.
    """
    path = tmp_path / "test.db"
    store.init_db(path)

    @contextlib.contextmanager
    def connect(_=None):
        connection = sqlite3.connect(path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setattr(rehydrate.store, "init_db", lambda *a, **k: None)
    with connect() as connection:
        yield connection


def add_posting(conn: sqlite3.Connection, posting_id: str, *,
                provider: str = "stepstone", description: str = TEASER) -> str:
    conn.execute(
        """INSERT INTO postings
           (id, loose_key, source, provider, url, canonical_url, company, title,
            description, first_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (posting_id, f"key-{posting_id}", f"portal:{provider}", provider,
         f"https://example.test/{posting_id}", f"https://example.test/{posting_id}",
         "ExampleCo", "Data Engineer", description, "2026-08-03T09:00:00+00:00"),
    )
    return posting_id


def add_verdict(conn, posting_id: str, score: int = 55, *, at: str = BEFORE) -> None:
    store.save_verdict(conn, posting_id,
                       {"score": score, "verdict": "maybe", "why": ["fit"],
                        "gaps": [], "stop_and_ask": False, "stop_reason": None},
                       "haiku")
    conn.execute("UPDATE verdicts SET created_at = ? WHERE posting_id = ?",
                 (at, posting_id))


def add_notification(conn, posting_id: str, kind: str) -> None:
    store.record_notification(conn, posting_id, "chat", hash(posting_id) % 10_000,
                              kind)


def add_decision(conn, posting_id: str) -> None:
    conn.execute(
        """INSERT INTO decisions (posting_id, action, decided_at)
           VALUES (?, 'skip', ?)""", (posting_id, AFTER))


def unscored_ids(conn) -> list[str]:
    return [row["id"] for row in store.unscored(conn)]


# --------------------------------------------------------------------------
# the durable half: filling a body invalidates the verdict
# --------------------------------------------------------------------------

def _fill(conn, posting_id: str, body: str = BODY) -> None:
    """Write a fetched body back the way the sweep does."""
    row = conn.execute("SELECT * FROM postings WHERE id = ?",
                       (posting_id,)).fetchone()
    posting = rehydrate._posting_from_row(row)
    posting.description = body
    rehydrate._write_back(conn, posting_id, posting)


def test_a_filled_body_drops_the_teasers_verdict(conn):
    """The regression, in one test."""
    pid = add_posting(conn, "p1")
    add_verdict(conn, pid, 55)

    _fill(conn, pid)

    assert store.get_verdict(conn, pid) is None
    assert unscored_ids(conn) == [pid]


def test_the_body_is_still_written(conn):
    """Dropping the verdict must not have cost the thing it was dropped for."""
    pid = add_posting(conn, "p1")
    add_verdict(conn, pid)

    _fill(conn, pid)

    row = conn.execute("SELECT description FROM postings WHERE id = ?",
                       (pid,)).fetchone()
    assert len(row["description"]) == len(BODY)


def test_the_attempt_counter_goes_with_it(conn):
    """Otherwise a posting that blipped twice on the teaser is one failure from
    being written off against a body it has not been read on yet."""
    pid = add_posting(conn, "p1")
    store.bump_score_attempt(conn, pid, "upstream 503")
    add_verdict(conn, pid)

    _fill(conn, pid)

    assert conn.execute(
        "SELECT COUNT(*) c FROM score_attempts").fetchone()["c"] == 0


def test_a_posting_that_was_never_scored_is_unaffected(conn):
    """Most of the sweep's rows. Clearing nothing must not raise."""
    pid = add_posting(conn, "p1")

    _fill(conn, pid)

    assert unscored_ids(conn) == [pid]


def test_only_the_filled_posting_loses_its_verdict(conn):
    kept = add_posting(conn, "p2")
    add_verdict(conn, kept, 80)
    pid = add_posting(conn, "p1")
    add_verdict(conn, pid)

    _fill(conn, pid)

    assert store.get_verdict(conn, kept)["score"] == 80


# --------------------------------------------------------------------------
# the one-off: rows filled before the above was true
# --------------------------------------------------------------------------

def test_a_stale_verdict_on_a_full_body_is_re_queued(conn):
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid)

    report = rehydrate.rescore_before(CUTOFF)

    assert report.verdicts == 1
    assert unscored_ids(conn) == [pid]


def test_a_verdict_written_after_the_body_arrived_is_left_alone(conn):
    """The cutoff is the whole safety of this: 22 of these were scored correctly
    on the same evening the sweep ran."""
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid, at=AFTER)

    assert rehydrate.rescore_before(CUTOFF).verdicts == 0
    assert store.get_verdict(conn, pid) is not None


def test_a_posting_still_holding_a_teaser_is_left_alone(conn):
    """Re-scoring it would only produce the same wrong answer. These are the
    sweep's recorded failures and they want fetching, not judging."""
    pid = add_posting(conn, "p1", description=TEASER)
    add_verdict(conn, pid)

    assert rehydrate.rescore_before(CUTOFF).verdicts == 0
    assert store.get_verdict(conn, pid) is not None


def test_providers_that_never_shipped_a_teaser_are_left_alone(conn):
    pid = add_posting(conn, "p1", provider="arbeitsagentur", description=BODY)
    add_verdict(conn, pid)

    assert rehydrate.rescore_before(CUTOFF).verdicts == 0
    assert store.get_verdict(conn, pid) is not None


def test_a_dry_run_reports_without_touching_anything(conn):
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid)
    add_notification(conn, pid, "digest")

    report = rehydrate.rescore_before(CUTOFF, dry_run=True)

    assert (report.verdicts, report.pings) == (1, 1)
    assert store.get_verdict(conn, pid) is not None
    assert conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 1


# --------------------------------------------------------------------------
# ...and who hears about it
# --------------------------------------------------------------------------
#
# `unnotified_in_band` excludes anything with a notifications row, so for a
# posting already in a digest, re-scoring on its own corrects the database and
# changes nothing the user ever sees. That is the case the digest row has to go.

def test_a_digested_posting_can_be_surfaced_again(conn):
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid, 44)
    add_notification(conn, pid, "digest")

    report = rehydrate.rescore_before(CUTOFF)

    assert report.pings == 1
    assert conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 0


def test_a_posting_already_pinged_is_not_pinged_twice(conn):
    """It was put in front of the user properly. A second ping because the score
    moved is noise, and the verdict is corrected either way."""
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid, 72)
    add_notification(conn, pid, "instant")

    report = rehydrate.rescore_before(CUTOFF)

    assert (report.verdicts, report.pings, report.kept_instant) == (1, 0, 1)
    assert conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 1


def test_a_posting_the_user_decided_on_is_never_re_offered(conn):
    """Re-offering something they skipped is worse than silence, and the digest
    row is the only thing keeping it out of the next round-up."""
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid, 44)
    add_notification(conn, pid, "digest")
    add_decision(conn, pid)

    report = rehydrate.rescore_before(CUTOFF)

    assert (report.pings, report.kept_decided) == (0, 1)
    assert conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"] == 1
    # The record is still corrected — only the messaging is held back.
    assert unscored_ids(conn) == [pid]


def test_a_never_notified_posting_needs_no_help(conn):
    """It is already eligible; dropping the verdict is the entire fix."""
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid, 30)

    report = rehydrate.rescore_before(CUTOFF)

    assert (report.pings, report.kept_instant, report.kept_decided) == (0, 0, 0)


def test_the_three_groups_are_counted_separately(conn):
    quiet = add_posting(conn, "p1", description=BODY)
    digested = add_posting(conn, "p2", description=BODY)
    pinged = add_posting(conn, "p3", description=BODY)
    decided = add_posting(conn, "p4", description=BODY)
    for pid in (quiet, digested, pinged, decided):
        add_verdict(conn, pid)
    add_notification(conn, digested, "digest")
    add_notification(conn, pinged, "instant")
    add_notification(conn, decided, "digest")
    add_decision(conn, decided)

    report = rehydrate.rescore_before(CUTOFF)

    assert report.verdicts == 4
    assert (report.pings, report.kept_instant, report.kept_decided) == (1, 1, 1)
    assert sorted(unscored_ids(conn)) == ["p1", "p2", "p3", "p4"]


def test_running_it_twice_finds_nothing_the_second_time(conn):
    pid = add_posting(conn, "p1", description=BODY)
    add_verdict(conn, pid)
    add_notification(conn, pid, "digest")

    rehydrate.rescore_before(CUTOFF)

    assert rehydrate.rescore_before(CUTOFF).summary().startswith("0 verdict(s)")


# --------------------------------------------------------------------------
# the store primitives
# --------------------------------------------------------------------------

def test_clearing_no_verdicts_is_a_no_op(conn):
    assert store.clear_verdicts(conn, []) == 0
    assert store.forget_notifications(conn, []) == 0


def test_a_repeated_id_is_counted_once(conn):
    pid = add_posting(conn, "p1")
    add_verdict(conn, pid)

    assert store.clear_verdicts(conn, [pid, pid]) == 1


def test_forgetting_one_kind_leaves_the_other(conn):
    """A posting can hold both rows — digested first, pinged later after a
    threshold change."""
    pid = add_posting(conn, "p1")
    add_notification(conn, pid, "digest")
    store.record_notification(conn, pid, "chat", 999, "instant")

    assert store.forget_notifications(conn, [pid], kind="digest") == 1
    rows = conn.execute("SELECT kind FROM notifications").fetchall()
    assert [row["kind"] for row in rows] == ["instant"]


def test_hydrate_writes_back_through_the_same_path():
    """`_write_back` is the only writer, so the guarantee cannot be bypassed by
    a caller that updates the description itself."""
    source = rehydrate.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert text.count("UPDATE postings\n") == 1


def test_the_teaser_guard_is_the_one_poll_uses():
    """Two modules deciding separately what counts as a teaser is how this bug
    got in; if they drift, the sweep and the poller disagree about which rows
    are already fixed."""
    from watcher import poll

    assert rehydrate.TEASER_CHARS is poll.TEASER_CHARS


def test_a_posting_needs_a_url_to_be_worth_fetching(conn):
    """`candidates` is unchanged, but it shares the teaser test with the
    re-score selector and the two must keep agreeing about it."""
    add_posting(conn, "p1", description=TEASER)
    conn.execute("UPDATE postings SET url = '' WHERE id = 'p1'")

    assert rehydrate.candidates(conn) == []


def test_posting_from_row_survives_unreadable_raw_json(conn):
    pid = add_posting(conn, "p1")
    conn.execute("UPDATE postings SET raw_json = 'not json' WHERE id = ?", (pid,))
    row = conn.execute("SELECT * FROM postings WHERE id = ?", (pid,)).fetchone()

    posting = rehydrate._posting_from_row(row)

    assert isinstance(posting, Posting) and posting.raw == {}
