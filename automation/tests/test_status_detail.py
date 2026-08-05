"""`/status` has to answer "is this thing still working", not just "is it up".

The report it replaced said `4068 fetched · 0 new · 0 pinged` and nothing else.
That is the healthy steady state — the same boards returning the same listings
every half hour, all of them already known — and it is also what a watcher whose
poller died three days ago says, forever, because the numbers are whatever they
were the last time it worked. The two were indistinguishable on the screen.

What separates them is a timestamp and a funnel. *When* the last cycle finished
tells you the loop is still turning; fetched → already seen → filtered → stored
tells you where the listings are going, so "nothing new today" can be told apart
from "everything is being dropped before it reaches the scorer".

Both have to survive a restart, which is why the cycle is written to the
database rather than kept in `bot_data`: the moment someone is most likely to
ask is just after the watcher came back, and that is exactly when the in-memory
copy is empty.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

import run_watcher
from watcher import notifier as notifier_module
from watcher import store
from watcher.config import Config
from watcher.normalize import Posting
from watcher.notifier import format_status


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(notifier_module, "DB_PATH", path)
    store.init_db(path)
    return path


def _cycle(**overrides) -> dict:
    now = dt.datetime.now()
    stats = {
        "started_at": (now - dt.timedelta(minutes=12)).isoformat(timespec="seconds"),
        "finished_at": (now - dt.timedelta(minutes=11)).isoformat(timespec="seconds"),
        "seconds": 41.2,
        "fetched": 4067, "already_known": 4061, "filtered": 0, "stored": 6,
        "scored": 6, "deferred": 0, "notified": 4, "sources_failed": [],
    }
    stats.update(overrides)
    return stats


# --------------------------------------------------------------------------
# the cycle record
# --------------------------------------------------------------------------

def test_the_cycle_survives_a_restart(db) -> None:
    """`bot_data` dies with the process; the question outlives it."""
    with store.connect() as conn:
        store.save_cycle(conn, _cycle())
    with store.connect() as conn:
        assert store.last_cycle(conn)["fetched"] == 4067


def test_no_cycle_on_record_is_not_an_error(db) -> None:
    with store.connect() as conn:
        assert store.last_cycle(conn) == {}


def test_an_unreadable_record_costs_one_line_not_the_report(db) -> None:
    with store.connect() as conn:
        store.set_meta(conn, store.LAST_CYCLE_KEY, "{not json")
    with store.connect() as conn:
        assert store.last_cycle(conn) == {}


def test_status_falls_back_to_the_stored_cycle(db) -> None:
    """A watcher that came back a minute ago still knows when it last polled."""
    with store.connect() as conn:
        store.save_cycle(conn, _cycle())

    text = format_status(Config(), {}, {}, dt.datetime.now())
    assert "4067 fetched" in text
    assert "none on record" not in text


def test_the_in_memory_cycle_wins_over_the_stored_one(db) -> None:
    with store.connect() as conn:
        store.save_cycle(conn, _cycle(fetched=1))
    text = format_status(Config(), _cycle(fetched=2), {}, None)
    assert "2 fetched" in text


# --------------------------------------------------------------------------
# what the report says
# --------------------------------------------------------------------------

def test_status_says_when_the_last_fetch_finished(db) -> None:
    """The line the whole rewrite exists for."""
    text = format_status(Config(), _cycle(), {}, None)
    assert "Last cycle:" in text
    assert "11m ago" in text
    assert "took 41s" in text


def test_status_shows_the_whole_funnel(db) -> None:
    """Fetched alone cannot distinguish quiet boards from a broken filter."""
    text = format_status(Config(), _cycle(), {}, None)
    assert "4067 fetched" in text
    assert "4061 already seen" in text
    assert "0 filtered" in text
    assert "6 new" in text
    assert "6 scored" in text


def test_status_says_when_the_next_cycle_is_due(db) -> None:
    text = format_status(Config(poll={"interval_minutes": 30}), _cycle(), {}, None)
    assert "Next cycle:" in text


def test_a_cycle_that_never_came_back_is_called_overdue(db) -> None:
    """Silence past the interval is the actual failure this report has to catch."""
    stale = dt.datetime.now() - dt.timedelta(hours=6)
    text = format_status(Config(poll={"interval_minutes": 30}),
                         _cycle(started_at=stale.isoformat(timespec="seconds"),
                                finished_at=stale.isoformat(timespec="seconds")),
                         {}, None)
    assert "overdue" in text


def test_a_failing_source_is_named_in_the_cycle_line(db) -> None:
    text = format_status(Config(), _cycle(sources_failed=["ats:Acme"]), {}, None)
    assert "ats:Acme" in text


def test_a_record_predating_the_wider_stats_is_not_shown_as_zeros(db) -> None:
    """An old row lacks three fields; a funnel of zeros reads as a dead poller."""
    text = format_status(Config(), {"fetched": 41, "stored": 3, "notified": 1},
                         {}, None)
    assert "41 fetched" in text
    assert "already seen" not in text


def test_status_reports_the_stored_state(db) -> None:
    with store.connect() as conn:
        for index, score in enumerate((90, 75, 55, 20)):
            posting = Posting(source="ats:Acme", provider="greenhouse",
                              source_job_id=str(index),
                              url=f"https://example.com/{index}",
                              company="Acme", title=f"Role {index}")
            store.insert_posting(conn, posting)
            store.save_verdict(conn, posting.fingerprint,
                               {"score": score, "verdict": "yes"}, "haiku")

    text = format_status(Config(), _cycle(), {}, None)
    assert "4 postings" in text
    assert "4 scored" in text
    # 90 and 75 clear the ping cut; 55 sits in the digest band.
    assert "Unsent:</b> 2 at ≥70 · 1 in the digest band" in text


def test_status_points_at_recheck_only_when_something_is_waiting(db) -> None:
    assert "/recheck" not in format_status(Config(), _cycle(), {}, None)

    with store.connect() as conn:
        posting = Posting(source="ats:Acme", provider="greenhouse",
                          source_job_id="1", url="https://example.com/1",
                          company="Acme", title="Role")
        store.insert_posting(conn, posting)
        store.save_verdict(conn, posting.fingerprint,
                           {"score": 90, "verdict": "yes"}, "haiku")
    assert "/recheck" in format_status(Config(), _cycle(), {}, None)


def test_status_fits_in_one_telegram_message(db) -> None:
    """4096 characters is a hard API limit — over it the message is rejected."""
    text = format_status(Config(notify={"topics": {"new_posting": 1}}),
                         _cycle(sources_failed=["a", "b", "c", "d"]),
                         {"running": 1, "queued": 3}, dt.datetime.now())
    assert len(text) < 4096


# --------------------------------------------------------------------------
# the handler
# --------------------------------------------------------------------------

class _Message:
    chat_id = -1001234567890

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_html(self, text: str) -> None:
        self.replies.append(text)


class _Update:
    def __init__(self, message: _Message) -> None:
        self.effective_message = message


class _Context:
    def __init__(self, app) -> None:
        self.application = app
        self.args: list[str] = []


class _Notifier:
    chat_id = "-1001234567890"
    config = Config()


class _App:
    def __init__(self) -> None:
        self.bot_data: dict = {"notifier": _Notifier()}


def test_the_command_reports_the_persisted_cycle(db, monkeypatch) -> None:
    """End to end: nothing in `bot_data`, and the answer is still complete."""
    monkeypatch.setattr(run_watcher, "_unfinished", list)
    with store.connect() as conn:
        store.save_cycle(conn, _cycle())

    message = _Message()
    asyncio.run(run_watcher.on_status(_Update(message), _Context(_App())))
    assert "4067 fetched" in message.replies[0]
