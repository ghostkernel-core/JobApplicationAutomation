"""The four `drops`-table primitives `recall.py` is built on.

Nothing here exercises the audit's stratification math or its scoring path —
that belongs to `test_recall.py`. This is purely: does a sampled row stop
being sampled once audited, does the per-stage count add up, and does pruning
touch only what has genuinely left the feed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from watcher import store
from watcher.normalize import Posting


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    store.init_db(path)
    return path


def posting(job_id: str, title: str = "Data Scientist") -> Posting:
    return Posting(source="ats", provider="greenhouse", source_job_id=job_id,
                   url=f"https://example.test/{job_id}", company="Acme",
                   title=f"{title} {job_id}", location="Berlin", country="DE")


def drop(conn, job_id: str, stage: str = "triage", reason: str = "x") -> str:
    p = posting(job_id)
    store.record_drop(conn, p, stage, reason, digest_key="KEY")
    return p.fingerprint


# --------------------------------------------------------------------------
# sample_drops / mark_drop_audited
# --------------------------------------------------------------------------

def test_sample_drops_only_returns_the_requested_stage(db):
    with store.connect(db) as conn:
        fp_triage = drop(conn, "1", stage="triage")
        drop(conn, "2", stage="title")

    with store.connect(db) as conn:
        rows = store.sample_drops(conn, "triage", limit=10)
    assert [r["id"] for r in rows] == [fp_triage]


def test_sample_drops_respects_the_limit(db):
    with store.connect(db) as conn:
        for i in range(5):
            drop(conn, str(i), stage="title")

    with store.connect(db) as conn:
        rows = store.sample_drops(conn, "title", limit=3)
    assert len(rows) == 3


def test_sample_drops_orders_newest_first(db, monkeypatch):
    with store.connect(db) as conn:
        fp_old = drop(conn, "old", stage="title")
        conn.execute("UPDATE drops SET last_seen_at = ? WHERE id = ?",
                    ("2020-01-01T00:00:00", fp_old))
        fp_new = drop(conn, "new", stage="title")

    with store.connect(db) as conn:
        rows = store.sample_drops(conn, "title", limit=10)
    assert [r["id"] for r in rows] == [fp_new, fp_old]


def test_sample_drops_does_not_fall_through_to_insertion_order(db):
    """The tie case is the *normal* case, and it used to pick one provider.

    `touch_drops` stamps one timestamp across a whole poll cycle and every
    drop still in a feed is touched every cycle, so in the live table three
    thousand rows carry two distinct `last_seen_at` values. Sorting on that
    alone leaves SQLite free to return rowid order — insertion order, which is
    source order — and the first real audit drew 39 of its 40 rows from one
    company's Greenhouse board.

    Ten draws of one row from fifty identically-stamped rows landing on the
    same row every time is what that bug looks like; the odds of the fixed
    version doing it are 50^-9.
    """
    with store.connect(db) as conn:
        for i in range(50):
            drop(conn, str(i), stage="title")
        conn.execute("UPDATE drops SET last_seen_at = '2026-08-08T00:00:46'")

    with store.connect(db) as conn:
        drawn = {store.sample_drops(conn, "title", limit=1)[0]["id"]
                 for _ in range(10)}
    assert len(drawn) > 1, "the sample is pinned to one row of fifty equal ones"


def test_sample_drops_still_prefers_a_genuinely_newer_row(db):
    """The random tiebreak must not become a random sample: where the stamps
    do differ, the newest row is still the one worth spending the budget on."""
    with store.connect(db) as conn:
        for i in range(20):
            fp = drop(conn, str(i), stage="title")
            conn.execute("UPDATE drops SET last_seen_at = ? WHERE id = ?",
                         ("2020-01-01T00:00:00", fp))
        fp_new = drop(conn, "new", stage="title")

    with store.connect(db) as conn:
        for _ in range(5):
            assert store.sample_drops(conn, "title", limit=1)[0]["id"] == fp_new


def test_a_never_audited_drop_has_no_score(db):
    with store.connect(db) as conn:
        fp = drop(conn, "1", stage="title")

    with store.connect(db) as conn:
        row = conn.execute("SELECT audited_at, audit_score FROM drops WHERE id = ?",
                           (fp,)).fetchone()
    assert row["audited_at"] is None
    assert row["audit_score"] is None


def test_mark_drop_audited_removes_it_from_the_next_sample(db):
    with store.connect(db) as conn:
        fp = drop(conn, "1", stage="title")
        store.mark_drop_audited(conn, fp, 42)

    with store.connect(db) as conn:
        rows = store.sample_drops(conn, "title", limit=10)
        row = conn.execute("SELECT audit_score FROM drops WHERE id = ?",
                           (fp,)).fetchone()
    assert rows == []
    assert row["audit_score"] == 42


def test_mark_drop_audited_accepts_no_score_for_an_unscoreable_posting(db):
    """A posting the audit could not hydrate is still marked audited — never
    re-drawn just because it could not be judged."""
    with store.connect(db) as conn:
        fp = drop(conn, "1", stage="title")
        store.mark_drop_audited(conn, fp, None)

    with store.connect(db) as conn:
        rows = store.sample_drops(conn, "title", limit=10)
        row = conn.execute("SELECT audited_at, audit_score FROM drops WHERE id = ?",
                           (fp,)).fetchone()
    assert rows == []
    assert row["audited_at"] is not None
    assert row["audit_score"] is None


# --------------------------------------------------------------------------
# drop_counts
# --------------------------------------------------------------------------

def test_drop_counts_groups_by_stage(db):
    with store.connect(db) as conn:
        drop(conn, "1", stage="triage")
        drop(conn, "2", stage="triage")
        drop(conn, "3", stage="title")

    with store.connect(db) as conn:
        counts = store.drop_counts(conn)
    assert counts == {"triage": 2, "title": 1}


def test_drop_counts_empty_table_is_an_empty_dict(db):
    with store.connect(db) as conn:
        assert store.drop_counts(conn) == {}


def test_drop_counts_since_excludes_rows_last_seen_before_the_cutoff(db):
    with store.connect(db) as conn:
        old = drop(conn, "1", stage="title")
        conn.execute("UPDATE drops SET last_seen_at = ? WHERE id = ?",
                    ("2020-01-01T00:00:00", old))
        drop(conn, "2", stage="title")

    with store.connect(db) as conn:
        counts = store.drop_counts(conn, since="2025-01-01")
    assert counts == {"title": 1}


# --------------------------------------------------------------------------
# prune_drops
# --------------------------------------------------------------------------

def test_prune_drops_removes_only_what_is_older_than_retention(db):
    with store.connect(db) as conn:
        stale = drop(conn, "1", stage="title")
        conn.execute(
            "UPDATE drops SET last_seen_at = ? WHERE id = ?",
            ((dt.date.today() - dt.timedelta(days=100)).isoformat(), stale))
        fresh = drop(conn, "2", stage="title")

    with store.connect(db) as conn:
        removed = store.prune_drops(conn, retention_days=90)

    assert removed == 1
    with store.connect(db) as conn:
        remaining = {r["id"] for r in conn.execute("SELECT id FROM drops")}
    assert remaining == {fresh}


def test_prune_drops_is_a_no_op_when_nothing_is_stale(db):
    with store.connect(db) as conn:
        drop(conn, "1", stage="title")

    with store.connect(db) as conn:
        removed = store.prune_drops(conn, retention_days=90)
    assert removed == 0


def test_a_touched_drop_is_not_pruned_even_if_first_seen_long_ago(db):
    """`touch_drops` is what a suppressed repeat gets instead of a full
    re-judgement — it must count as the row still being alive."""
    with store.connect(db) as conn:
        fp = drop(conn, "1", stage="triage")
        conn.execute(
            "UPDATE drops SET first_seen_at = ?, last_seen_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", "2020-01-01T00:00:00", fp))
        store.touch_drops(conn, [fp])

    with store.connect(db) as conn:
        removed = store.prune_drops(conn, retention_days=90)
    assert removed == 0
