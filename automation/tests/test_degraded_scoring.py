"""What happens to a posting the scorer could not judge.

This is the failure that motivated the code under test: a ~60-second upstream
blip made six consecutive batches fail, the matcher stored its fallback verdict
(45, `maybe`) for every posting in them, and `store.unscored()` only selects
postings with *no* verdict row — so 49 postings were permanently parked below
`notify_threshold` and could never be re-scored. Nothing was logged as broken,
because from the outside "nothing matched today" and "scoring is dead" looked
identical.

So the invariants worth pinning down are: a degraded result must not become a
stored verdict on the first failure, it must still become one eventually (or the
retry never terminates), and the reported reason for a failure must be the real
error rather than the benign warning that used to mask it.
"""

from __future__ import annotations

import sqlite3

import pytest

from watcher import claude_cli, matcher, store


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    store.init_db(path)
    with store.connect(path) as connection:
        yield connection


class FakeConfig:
    """Only the fields `matcher._persist` reads."""

    match_model = "haiku"
    max_score_attempts = 3


def add_posting(conn: sqlite3.Connection, posting_id: str, title: str = "AI Engineer"
                ) -> str:
    conn.execute(
        """INSERT INTO postings
           (id, loose_key, source, provider, url, canonical_url, company, title,
            first_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (posting_id, f"key-{posting_id}", "portal:test", "test",
         f"https://example.test/{posting_id}", f"https://example.test/{posting_id}",
         "ExampleCo", title, "2026-08-04T09:00:00+00:00"),
    )
    return posting_id


def persist(conn, posting_id, verdict, config=None):
    """Call the real persistence path and hand back the report it filled in."""
    report = matcher.MatchReport()
    matcher._persist(conn, posting_id, verdict, config or FakeConfig(), report)
    return report


def unscored_ids(conn) -> list[str]:
    return [row["id"] for row in store.unscored(conn)]


# --------------------------------------------------------------------------
# a degraded result is held back, not stored
# --------------------------------------------------------------------------

def test_first_failure_writes_no_verdict_and_stays_unscored(conn):
    """The bug in one test: a blip must not end the posting's life."""
    pid = add_posting(conn, "p1")
    report = persist(conn, pid, matcher._degraded("upstream 503"))

    assert store.get_verdict(conn, pid) is None
    assert unscored_ids(conn) == [pid]
    assert (report.deferred, report.exhausted) == (1, 0)


def test_failure_reason_is_kept_for_the_next_attempt(conn):
    pid = add_posting(conn, "p1")
    persist(conn, pid, matcher._degraded("upstream 503"))

    row = conn.execute(
        "SELECT attempts, last_error FROM score_attempts WHERE posting_id = ?",
        (pid,)).fetchone()
    assert row["attempts"] == 1
    assert row["last_error"] == "upstream 503"


def test_a_good_verdict_still_persists_normally(conn):
    pid = add_posting(conn, "p1")
    report = persist(conn, pid, {"score": 81, "verdict": "strong", "why": ["fit"],
                                 "gaps": [], "stop_and_ask": False,
                                 "stop_reason": None})

    stored = store.get_verdict(conn, pid)
    assert stored is not None and stored["score"] == 81
    assert unscored_ids(conn) == []
    assert (report.deferred, report.exhausted) == (0, 0)


def test_recovery_clears_the_attempt_counter(conn):
    """Two blips then a success must not leave the posting one failure from
    being written off weeks later."""
    pid = add_posting(conn, "p1")
    persist(conn, pid, matcher._degraded("blip"))
    persist(conn, pid, matcher._degraded("blip"))
    persist(conn, pid, {"score": 70, "verdict": "strong", "why": [], "gaps": [],
                        "stop_and_ask": False, "stop_reason": None})

    assert conn.execute("SELECT COUNT(*) c FROM score_attempts").fetchone()["c"] == 0


# --------------------------------------------------------------------------
# ...but not forever
# --------------------------------------------------------------------------

def test_retries_terminate_at_max_score_attempts(conn):
    """Otherwise a posting the scorer genuinely cannot read is re-scored on
    every cycle, for ever, at full token cost."""
    pid = add_posting(conn, "p1")
    for _ in range(FakeConfig.max_score_attempts - 1):
        assert store.get_verdict(conn, pid) is None
        persist(conn, pid, matcher._degraded("unparseable posting"))

    report = persist(conn, pid, matcher._degraded("unparseable posting"))

    stored = store.get_verdict(conn, pid)
    assert stored is not None and stored["score"] == 45
    assert unscored_ids(conn) == []
    assert (report.deferred, report.exhausted) == (0, 1)


def test_attempt_budget_is_per_posting(conn):
    """One posting exhausting its retries must not spend another's."""
    kept, doomed = add_posting(conn, "p1"), add_posting(conn, "p2")
    for _ in range(FakeConfig.max_score_attempts):
        persist(conn, doomed, matcher._degraded("bad"))
    persist(conn, kept, matcher._degraded("blip"))

    assert store.get_verdict(conn, doomed) is not None
    assert store.get_verdict(conn, kept) is None


# --------------------------------------------------------------------------
# recovering postings buried before any of this existed
# --------------------------------------------------------------------------

def test_rescore_finds_and_clears_only_degraded_verdicts(conn):
    buried, real = add_posting(conn, "p1"), add_posting(conn, "p2")
    store.save_verdict(conn, buried, matcher._FALLBACK, "haiku")
    # Same score and band as the fallback, but a real judgement — it must
    # survive, which is why the marker is the gaps phrase and not the 45.
    store.save_verdict(conn, real, {"score": 45, "verdict": "maybe", "why": [],
                                    "gaps": ["no German required"],
                                    "stop_and_ask": False, "stop_reason": None},
                       "haiku")

    assert store.degraded_verdict_ids(conn) == [buried]
    assert store.clear_degraded_verdicts(conn) == 1
    assert unscored_ids(conn) == [buried]
    assert store.get_verdict(conn, real) is not None


def test_rescore_gives_back_a_full_attempt_budget(conn):
    """A posting written off under the old behaviour deserves a clean slate,
    not re-burial on its first retry."""
    pid = add_posting(conn, "p1")
    for _ in range(FakeConfig.max_score_attempts):
        persist(conn, pid, matcher._degraded("blip"))

    store.clear_degraded_verdicts(conn)
    persist(conn, pid, matcher._degraded("blip"))

    assert store.get_verdict(conn, pid) is None
    assert unscored_ids(conn) == [pid]


def test_rescore_on_a_healthy_database_is_a_no_op(conn):
    add_posting(conn, "p1")
    store.save_verdict(conn, "p1", {"score": 80, "verdict": "strong", "why": [],
                                    "gaps": [], "stop_and_ask": False,
                                    "stop_reason": None}, "haiku")
    assert store.clear_degraded_verdicts(conn) == 0
    assert store.get_verdict(conn, "p1") is not None


def test_fallback_uses_the_marker_the_store_searches_for(conn):
    """These two constants live in different modules and must not drift; if
    they do, `rescore` silently stops finding anything."""
    assert matcher._FALLBACK["gaps"] == [store.DEGRADED_GAP]


# --------------------------------------------------------------------------
# reporting the failure that caused all this
# --------------------------------------------------------------------------

BENIGN = "⚠ claude.ai connectors are disabled in this environment"


def test_benign_stderr_does_not_outrank_the_real_error():
    """The original misdiagnosis: stderr is never empty, so `stderr or stdout`
    reported this warning as the cause of every failure."""
    detail = claude_cli.failure_detail("Credit balance is too low", BENIGN)
    assert "Credit balance is too low" in detail
    assert "connectors" not in detail


def test_a_real_stderr_line_is_kept_alongside_stdout():
    detail = claude_cli.failure_detail("partial output", f"{BENIGN}\nfatal: no such model")
    assert "fatal: no such model" in detail
    assert "partial output" in detail


def test_nothing_useful_says_so_rather_than_returning_empty():
    detail = claude_cli.failure_detail("", BENIGN)
    assert "no output" in detail
    # The warning is still quoted, so the line is diagnosable rather than blank.
    assert "connectors" in detail


def test_detail_is_length_capped():
    assert len(claude_cli.failure_detail("x" * 5000, "", limit=200)) == 200


# --------------------------------------------------------------------------
# and what the status report says about them
# --------------------------------------------------------------------------
#
# The counts are the only way anyone sees this from outside, and they were
# telling two different stories about the same postings. An outage parked 25 of
# them, and `/status` then read `25 retrying · 25 unjudged` for hours — one half
# of the line promising the watcher was still working on them, the other half
# correct. Nothing was retrying: they had run out of attempts, and their
# `score_attempts` rows simply outlived the loop that wrote them.

def snapshot(conn) -> dict:
    return store.snapshot(conn, notify_threshold=65, digest_threshold=40)


def test_a_posting_still_in_the_retry_loop_counts_as_retrying(conn):
    pid = add_posting(conn, "p1")
    persist(conn, pid, matcher._degraded("upstream 503"))

    snap = snapshot(conn)
    assert (snap["retrying"], snap["unjudged"]) == (1, 0)
    assert snap["pending"] == 1  # the next cycle really will pick it up


def test_a_posting_out_of_attempts_is_unjudged_not_retrying(conn):
    """The regression. Its attempt row survives; it is not being retried."""
    pid = add_posting(conn, "p1")
    for _ in range(FakeConfig.max_score_attempts):
        persist(conn, pid, matcher._degraded("upstream 503"))

    assert conn.execute(
        "SELECT COUNT(*) c FROM score_attempts").fetchone()["c"] == 1
    snap = snapshot(conn)
    assert (snap["retrying"], snap["unjudged"]) == (0, 1)
    assert snap["pending"] == 0  # nothing will pick it up again


def test_the_two_counts_never_describe_the_same_posting(conn):
    """One held back, one written off — one of each, not two of both."""
    held, done = add_posting(conn, "p1"), add_posting(conn, "p2")
    persist(conn, held, matcher._degraded("blip"))
    for _ in range(FakeConfig.max_score_attempts):
        persist(conn, done, matcher._degraded("blip"))

    snap = snapshot(conn)
    assert (snap["retrying"], snap["unjudged"]) == (1, 1)


def test_rescore_clears_both_counts(conn):
    pid = add_posting(conn, "p1")
    for _ in range(FakeConfig.max_score_attempts):
        persist(conn, pid, matcher._degraded("blip"))

    store.clear_degraded_verdicts(conn)

    snap = snapshot(conn)
    assert (snap["retrying"], snap["unjudged"]) == (0, 0)
    assert snap["pending"] == 1
