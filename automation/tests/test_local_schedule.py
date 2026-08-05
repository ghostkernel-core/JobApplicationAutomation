"""The daily jobs fire at the local times config.toml claims they do.

`JobQueue.run_daily` reads a naive `datetime.time` as UTC. `digest_hour = 19`
was therefore firing at 21:05 in Berlin and the 09:15 heartbeat at 11:15, while
both the config comment and `watcherctl status` reported those times as local.
Two hours is enough to push the evening digest past the point anyone is still
looking and the heartbeat out of the morning altogether.

The property that matters is narrow and worth pinning: the time handed to the
scheduler carries a zone. A naive one is not "probably fine in UTC+0" — it is
the bug, on every machine that is not on UTC.
"""

from __future__ import annotations

import datetime as dt
import sys

import pytest

import run_watcher
from watcher import config
from watcher.notifier import format_status


def test_a_daily_time_carries_a_timezone() -> None:
    """The whole bug in one assertion: naive means UTC to JobQueue."""
    assert run_watcher.local_time(19, 5).tzinfo is not None


def test_the_hour_and_minute_survive() -> None:
    when = run_watcher.local_time(19, 5)
    assert (when.hour, when.minute) == (19, 5)


@pytest.mark.parametrize("hour,minute", [(9, 15), (19, 5), (0, 0), (23, 59)])
def test_every_scheduled_slot_is_zone_aware(hour: int, minute: int) -> None:
    when = run_watcher.local_time(hour, minute)
    assert when.tzinfo is not None
    assert (when.hour, when.minute) == (hour, minute)


def test_the_zone_is_resolved_not_frozen_to_todays_offset() -> None:
    """A fixed +02:00 read in August would still claim summer time in December.

    `dt.timezone` is the fixed-offset type; anything else (a `ZoneInfo`, or
    whatever `tzlocal` hands back) knows its own DST rules.
    """
    zone = run_watcher.local_time(19, 5).tzinfo
    assert not isinstance(zone, dt.timezone), (
        "a fixed offset goes stale at the next DST change")


def test_a_missing_tzlocal_still_yields_an_aware_time(monkeypatch) -> None:
    """Fallback path: wrong after the next DST change, but not wrong by hours.

    Pinning to the current offset keeps the watcher starting and keeps the
    digest inside the right evening, which beats silently reverting to UTC.
    """
    class _Missing:
        def __getattr__(self, name):
            raise ImportError("no tzlocal here")

    monkeypatch.setitem(sys.modules, "tzlocal", _Missing())

    when = run_watcher.local_time(19, 5)
    assert when.tzinfo is not None
    assert (when.hour, when.minute) == (19, 5)


def test_a_broken_system_zone_still_yields_an_aware_time(monkeypatch) -> None:
    """`get_localzone` raises on a machine with no readable zone at all."""
    class _Broken:
        @staticmethod
        def get_localzone():
            raise RuntimeError("cannot determine system timezone")

    monkeypatch.setitem(sys.modules, "tzlocal", _Broken())

    when = run_watcher.local_time(19, 5)
    assert when.tzinfo is not None


class _Config:
    interval_minutes = 20
    digest_hour = 19
    heartbeat_hour = 9
    build_enabled = False
    kb_enabled = True
    kb_hour = 10
    kb_weekday = 6  # Sunday by `date.weekday()`; run_daily counts it as 0
    # Paired the way the real Config pairs them, not hardcoded: these stubs
    # exist to keep `build_app` off the disk, not to restate the schedule.
    digest_at = (digest_hour, config.DIGEST_MINUTE)
    heartbeat_at = (heartbeat_hour, config.HEARTBEAT_MINUTE)
    kb_at = (kb_hour, config.KB_MINUTE)


class _Queue:
    def __init__(self) -> None:
        self.daily: list[dt.time] = []

    def run_repeating(self, *args, **kwargs) -> None:
        pass

    def run_daily(self, callback, time, **kwargs) -> None:
        self.daily.append(time)


class _App:
    def __init__(self) -> None:
        self.job_queue = _Queue()
        self.bot_data: dict = {}

    def add_handler(self, *args, **kwargs) -> None:
        pass


class _Notifier:
    chat_id = "12345"
    config = _Config()


def _build(monkeypatch) -> _App:
    """Run the real `build_app` with the Telegram application swapped out."""
    app = _App()

    class _AppBuilder:
        def token(self, *args, **kwargs):
            return self

        def post_init(self, *args, **kwargs):
            return self

        def build(self):
            return app

    class _AppFactory:
        @staticmethod
        def builder():
            return _AppBuilder()

    monkeypatch.setattr(run_watcher, "Application", _AppFactory)
    monkeypatch.setattr(run_watcher, "require_env", lambda name: "token")
    run_watcher.build_app(_Notifier())
    return app


def test_the_daily_jobs_are_registered_with_aware_times(monkeypatch) -> None:
    """The regression guard: `build_app` must not hand `run_daily` a bare time.

    `local_time` being correct is no use if the call site goes back to
    `dt.time(hour=...)`, which is exactly how this shipped.
    """
    app = _build(monkeypatch)

    assert len(app.job_queue.daily) == 3, "digest, heartbeat, and kb"
    for when in app.job_queue.daily:
        assert when.tzinfo is not None, (
            "run_daily was handed a naive time — JobQueue reads that as UTC")


def test_the_registered_times_match_the_config(monkeypatch) -> None:
    """19:05 and 09:15 are what config.toml and `watcherctl status` promise."""
    app = _build(monkeypatch)
    slots = {(when.hour, when.minute) for when in app.job_queue.daily}
    assert (19, 5) in slots
    assert (9, 15) in slots


# --------------------------------------------------------------------------
# what the schedule says vs what it does
# --------------------------------------------------------------------------

def test_status_quotes_the_minute_the_job_actually_runs_at(monkeypatch) -> None:
    """The second half of the same bug, and the half that survived the fix.

    Moving the daily jobs into local time left both status reports formatting a
    flat ":00" from the hour alone, so a heartbeat registered for 09:15 was
    advertised as 09:00 and looked a quarter of an hour late every morning to
    the one person reading it. The times shown and the times scheduled come
    from one property now; this is what stops them separating again.
    """
    app = _build(monkeypatch)
    scheduled = {f"{when.hour:02d}:{when.minute:02d}"
                 for when in app.job_queue.daily}
    text = format_status(config.Config(), {}, {}, None)

    assert "digest 19:05" in text and "19:05" in scheduled
    assert "heartbeat 09:15" in text and "09:15" in scheduled
    assert ":00" not in text, "a flat :00 is the old hour-only formatting"


@pytest.mark.parametrize("hour", [0, 7, 19, 23])
def test_a_changed_hour_moves_both_the_job_and_the_report(hour: int) -> None:
    """Editing `digest_hour` must not need a second edit somewhere else."""
    cfg = config.Config(notify={"digest_hour": hour})
    assert cfg.digest_at == (hour, config.DIGEST_MINUTE)
    assert config.clock(cfg.digest_at) in format_status(cfg, {}, {}, None)
