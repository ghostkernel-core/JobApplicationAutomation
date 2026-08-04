"""Source health: disable, backoff, park, recover.

These cover the state machine that decides whether a broken source is retried,
how often, and when it stops being retried at all. That logic is invisible in
normal operation — it only shows itself weeks later as a source that never came
back, or one that hammered a dead endpoint 24 times a day — so it is exactly the
part worth pinning down.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from watcher import store


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    store.init_db(path)
    with store.connect(path) as connection:
        yield connection


def health_row(conn: sqlite3.Connection, source: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM source_health WHERE source = ?", (source,)).fetchone()


# --------------------------------------------------------------------------
# disabling
# --------------------------------------------------------------------------

def test_failures_below_threshold_do_not_disable(conn):
    for _ in range(2):
        assert store.mark_source_failed(conn, "portal:x", "boom", 3) is None
    assert not store.is_source_disabled(conn, "portal:x")


def test_third_failure_disables_and_reports_once(conn):
    store.mark_source_failed(conn, "portal:x", "boom", 3)
    store.mark_source_failed(conn, "portal:x", "boom", 3)
    assert store.mark_source_failed(conn, "portal:x", "boom", 3) == "disabled"
    assert store.is_source_disabled(conn, "portal:x")
    # The fourth failure must stay silent, or a disabled source re-announces
    # itself on every retry probe forever.
    assert store.mark_source_failed(conn, "portal:x", "boom", 3) is None


def test_tier_threshold_is_honoured(conn):
    """A fragile source gets more rope before it switches itself off."""
    for _ in range(5):
        assert store.mark_source_failed(conn, "portal:scraper", "boom", 6) is None
    assert store.mark_source_failed(conn, "portal:scraper", "boom", 6) == "disabled"


def test_success_clears_everything(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "boom", 3)
    store.bump_source_cooldown(conn, "portal:x")
    store.mark_source_ok(conn, "portal:x")

    row = health_row(conn, "portal:x")
    assert not row["disabled"]
    assert row["consecutive_failures"] == 0
    assert row["retry_attempts"] == 0
    assert not row["parked"]
    assert row["last_error"] is None


# --------------------------------------------------------------------------
# backoff
# --------------------------------------------------------------------------

@pytest.mark.parametrize("attempts,expected", [
    (0, 60), (1, 120), (2, 240), (3, 480), (4, 960),
    (5, 1440),   # capped
    (99, 1440),  # still capped, no overflow
])
def test_backoff_ladder(attempts, expected):
    assert store.backoff_minutes(attempts, 60, 2.0, 1440) == expected


def test_backoff_factor_one_is_flat():
    """factor = 1.0 must reproduce the old constant cooldown exactly."""
    assert [store.backoff_minutes(n, 60, 1.0, 1440) for n in range(5)] == [60] * 5


def test_backoff_disabled_when_base_is_zero():
    assert store.backoff_minutes(3, 0, 2.0, 1440) == 0


def test_retry_due_uses_backoff(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "boom", 3)
    # Went down two hours ago with one failed probe already recorded, so the
    # next cooldown is 120 minutes — due exactly now, not an hour ago.
    conn.execute(
        "UPDATE source_health SET disabled_at = ?, retry_attempts = 1 "
        "WHERE source = ?",
        ((dt.datetime.now() - dt.timedelta(minutes=119)).isoformat(), "portal:x"))
    assert not store.retry_is_due(conn, "portal:x", 60, 2.0, 1440)

    conn.execute(
        "UPDATE source_health SET disabled_at = ? WHERE source = ?",
        ((dt.datetime.now() - dt.timedelta(minutes=121)).isoformat(), "portal:x"))
    assert store.retry_is_due(conn, "portal:x", 60, 2.0, 1440)


def test_failed_probe_widens_the_cooldown(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "boom", 3)
    assert store.retry_attempts(conn, "portal:x") == 0
    store.bump_source_cooldown(conn, "portal:x")
    store.bump_source_cooldown(conn, "portal:x")
    assert store.retry_attempts(conn, "portal:x") == 2


def test_retry_never_due_when_auto_retry_is_off(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "boom", 3)
    assert not store.retry_is_due(conn, "portal:x", 0, 2.0, 1440)
    assert store.retry_due_at(health_row(conn, "portal:x"), 0) is None


# --------------------------------------------------------------------------
# parking
# --------------------------------------------------------------------------

def test_structural_failure_parks_at_the_threshold(conn):
    store.mark_source_failed(conn, "portal:x", "HTTP 404", 3, structural=True)
    store.mark_source_failed(conn, "portal:x", "HTTP 404", 3, structural=True)
    # A single 404 can be one bad deploy on the far end; three in a row is an
    # endpoint that moved. So parking still waits for the threshold.
    assert not store.is_source_parked(conn, "portal:x")
    assert store.mark_source_failed(
        conn, "portal:x", "HTTP 404", 3, structural=True) == "parked"
    assert store.is_source_parked(conn, "portal:x")


def test_parked_source_is_never_auto_retried(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "HTTP 404", 3, structural=True)
    conn.execute(
        "UPDATE source_health SET disabled_at = ? WHERE source = ?",
        ((dt.datetime.now() - dt.timedelta(days=30)).isoformat(), "portal:x"))
    # Thirty days past any cooldown and still not due: that is the point.
    assert not store.retry_is_due(conn, "portal:x", 60, 2.0, 1440)
    assert store.retry_due_at(health_row(conn, "portal:x"), 60, 2.0, 1440) is None


def test_transient_failure_disables_without_parking(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "HTTP 503", 3)
    assert store.is_source_disabled(conn, "portal:x")
    assert not store.is_source_parked(conn, "portal:x")


def test_already_disabled_source_can_be_parked_later(conn):
    """Down transiently, then the endpoint disappears underneath it.

    Without this the source would keep probing a dead URL forever, because the
    disable already fired and `mark_source_failed` never reports twice.
    """
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "HTTP 503", 3)
    assert store.park_source(conn, "portal:x", "HTTP 404 from /v4/jobs")
    assert store.is_source_parked(conn, "portal:x")
    # Idempotent: no second escalation for the same park.
    assert not store.park_source(conn, "portal:x", "HTTP 404 from /v4/jobs")


def test_reset_unparks(conn):
    for _ in range(3):
        store.mark_source_failed(conn, "portal:x", "HTTP 404", 3, structural=True)
    store.reset_source(conn, "portal:x")
    row = health_row(conn, "portal:x")
    assert not row["parked"] and not row["disabled"]
    assert row["park_reason"] is None
    assert store.retry_attempts(conn, "portal:x") == 0


def test_park_reason_is_recorded(conn):
    for _ in range(3):
        store.mark_source_failed(
            conn, "portal:x", "HTTP 404 from /pc/v4/jobs", 3, structural=True)
    assert "404" in health_row(conn, "portal:x")["park_reason"]


# --------------------------------------------------------------------------
# migration safety
# --------------------------------------------------------------------------

def test_row_predating_the_migration_is_due_immediately(tmp_path):
    """An install disabled before `parked`/`retry_attempts` existed must still
    recover, not wedge itself on a column the row does not have."""
    path = tmp_path / "old.db"
    store.init_db(path)
    with store.connect(path) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "portal:x", "boom", 3)
        conn.execute("UPDATE source_health SET disabled_at = NULL "
                     "WHERE source = 'portal:x'")
        row = health_row(conn, "portal:x")
    assert store.retry_due_at(row, 60, 2.0, 1440) is not None


def test_row_is_parked_tolerates_missing_column():
    assert store.row_is_parked(None) is False
