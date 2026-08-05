"""Moving the score cuts from the chat, and sending what the move qualified.

Two commands, and the split between them is the whole design. `/threshold`
writes the new cut to config.toml and sends nothing; `/recheck` sends. A
threshold change acts retroactively on hundreds of postings that were scored
weeks ago and skipped over at the time, so folding the send into the setter
would make one mistyped number a phone full of pings.

Neither re-scores anything. Every posting already carries a verdict; what a cut
change alters is which of those verdicts clears the bar, and `unnotified_in_band`
answers that against whatever the cut is now. That is also why the once-per-
posting rule still holds across a re-check: `record_notification` is the
gatekeeper, not the score.

The writer itself gets its own tests because config.toml is the file the running
watcher re-reads on every access. A truncated or reformatted one is not a bad
setting — it is the watcher losing its configuration mid-cycle, and the file is
two-thirds comments explaining how the current numbers were calibrated.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

import run_watcher
from watcher import config as watcher_config
from watcher import notifier as notifier_module
from watcher import store
from watcher.config import Config, ConfigWriteError
from watcher.normalize import Posting


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------

class _Notifier:
    chat_id = "-1001234567890"

    def __init__(self, config: Config | None = None) -> None:
        self._override = config
        self.sent: list[int] = []
        self.instant_limit: int | None = None
        self.fail = False

    @property
    def config(self) -> Config:
        # Re-read on every access, exactly as the real Notifier does — that is
        # what makes a threshold written to config.toml apply without a
        # restart, and a stub holding one fixed Config would let a handler that
        # quotes a stale cut back to the user pass.
        return self._override or watcher_config.load_config()

    async def send_instant(self, limit: int = 6) -> int:
        if self.fail:
            raise RuntimeError("telegram is down")
        self.instant_limit = limit
        with store.connect() as conn:
            rows = store.unnotified_in_band(conn, self.config.notify_threshold)
        for row in rows[:limit]:
            with store.connect() as conn:
                store.record_notification(conn, row["id"], self.chat_id,
                                          len(self.sent) + 1, "instant")
            self.sent.append(row["score"])
        return len(self.sent)


class _App:
    def __init__(self, notifier: _Notifier) -> None:
        self.bot_data: dict = {"notifier": notifier}


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
    def __init__(self, app: _App, args: list[str] | None = None) -> None:
        self.application = app
        self.args = args or []


def _run(handler, app: _App, args: list[str] | None = None) -> list[str]:
    message = _Message()
    asyncio.run(handler(_Update(message), _Context(app, args)))
    return message.replies


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    """A store of this test's own — these commands count real rows."""
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(notifier_module, "DB_PATH", path)
    store.init_db(path)
    return path


def _scored(db, *scores: int) -> None:
    """One posting per score, stored and judged, nothing notified."""
    with store.connect() as conn:
        for index, score in enumerate(scores):
            posting = Posting(source="ats:Acme", provider="greenhouse",
                              source_job_id=str(index),
                              url=f"https://example.com/{index}",
                              company="Acme", title=f"Role {index}")
            store.insert_posting(conn, posting)
            store.save_verdict(conn, posting.fingerprint,
                               {"score": score, "verdict": "yes"}, "haiku")


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A copy of the real config.toml, so the writer is tested on the real thing.

    The reloader captured the path at import, so it is redirected too — and its
    cache is dropped either side, or the tests that use the real file would be
    served whatever the last temporary one parsed to.
    """
    path = tmp_path / "config.toml"
    shutil.copy(watcher_config.CONFIG_PATH, path)
    monkeypatch.setattr(watcher_config, "CONFIG_PATH", path)
    monkeypatch.setattr(watcher_config._config_reloader, "_path", path)
    monkeypatch.delenv("WATCHER_MATCH_NOTIFY_THRESHOLD", raising=False)
    watcher_config._config_reloader.invalidate()
    yield path
    watcher_config._config_reloader.invalidate()


# --------------------------------------------------------------------------
# the writer
# --------------------------------------------------------------------------

def test_setting_a_knob_changes_one_line_and_nothing_else(config_file) -> None:
    before = config_file.read_text(encoding="utf-8").splitlines()
    assert watcher_config.set_number("match", "notify_threshold", 62) == 70
    after = config_file.read_text(encoding="utf-8").splitlines()

    assert len(before) == len(after)
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert changed == [("notify_threshold = 70", "notify_threshold = 62")]


def test_the_calibration_comments_survive_the_edit(config_file) -> None:
    """The paragraph above the cut is the record of why it is that number.

    Round-tripping the file through a TOML writer would drop every one of these
    lines to save the parsing this does by hand, and the next person to wonder
    why 70 would have nothing to read.
    """
    watcher_config.set_number("match", "notify_threshold", 55)
    text = config_file.read_text(encoding="utf-8")
    assert "Calibrated with" in text
    assert "70 is the cut" in text


def test_a_key_is_only_matched_inside_its_own_section(config_file) -> None:
    """`[notify] digest_hour` and `[kb] hour` are different settings."""
    with pytest.raises(ConfigWriteError, match="under `\\[notify\\]`"):
        watcher_config.set_number("notify", "notify_threshold", 50)


def test_a_subsection_closes_the_one_above_it(config_file) -> None:
    """`[notify.topics]` ends `[notify]`; a naive prefix test would not."""
    assert watcher_config.set_number("notify", "digest_hour", 20) == 19
    assert "digest_hour = 20" in config_file.read_text(encoding="utf-8")


def test_writing_the_same_value_is_a_no_op(config_file) -> None:
    stamp = config_file.stat().st_mtime_ns
    assert watcher_config.set_number("match", "notify_threshold", 70) == 70
    assert config_file.stat().st_mtime_ns == stamp, "the file was rewritten"


def test_an_env_override_refuses_the_write(config_file, monkeypatch) -> None:
    """Env beats file, so a write that appears to work would change nothing.

    Silently editing a line the running watcher is not reading is worse than
    refusing: the number in the file and the number in force would disagree,
    and the chat would have been told the change landed.
    """
    monkeypatch.setenv("WATCHER_MATCH_NOTIFY_THRESHOLD", "80")
    with pytest.raises(ConfigWriteError, match="overrides config.toml"):
        watcher_config.set_number("match", "notify_threshold", 50)
    assert "notify_threshold = 70" in config_file.read_text(encoding="utf-8")


def test_a_missing_key_is_refused_rather_than_appended(config_file) -> None:
    with pytest.raises(ConfigWriteError, match="renamed or removed"):
        watcher_config.set_number("match", "invented_knob", 1)


def test_the_new_value_is_visible_immediately(config_file) -> None:
    """The reloader keys on mtime; the writer must not depend on its resolution."""
    watcher_config.load_config()
    watcher_config.set_number("match", "notify_threshold", 44)
    assert watcher_config.load_config().notify_threshold == 44


# --------------------------------------------------------------------------
# /threshold
# --------------------------------------------------------------------------

def test_bare_threshold_reports_the_cuts_and_what_is_waiting(db) -> None:
    _scored(db, 90, 75, 55, 20)
    replies = _run(run_watcher.on_threshold, _App(_Notifier()))
    text = replies[0]
    assert "≥70" in text
    assert "2 scored posting(s) qualify" in text   # 90 and 75
    assert "1 more in the band" in text            # 55
    assert "/recheck" in text, "the reader has to be told sending is separate"


def test_a_threshold_change_writes_the_file(db, config_file) -> None:
    _scored(db, 90, 75, 55, 20)
    replies = _run(run_watcher.on_threshold, _App(_Notifier()), ["50"])

    assert "notify_threshold = 50" in config_file.read_text(encoding="utf-8")
    assert "70 → 50" in replies[0]
    assert "Lowered" in replies[0]


def test_lowering_the_cut_reports_the_postings_it_just_qualified(
        db, config_file) -> None:
    """The point of the command, and the only way to see it took effect."""
    _scored(db, 90, 75, 55, 20)
    replies = _run(run_watcher.on_threshold, _App(_Notifier()), ["50"])
    assert "3 scored posting(s) now qualify" in replies[0]  # 90, 75, 55
    assert "/recheck" in replies[0]


def test_the_digest_cut_is_addressed_by_name(db, config_file) -> None:
    _run(run_watcher.on_threshold, _App(_Notifier()), ["digest", "30"])
    assert "digest_threshold = 30" in config_file.read_text(encoding="utf-8")


def test_the_ping_cut_cannot_drop_below_the_digest_cut(db, config_file) -> None:
    """Below it the digest band is empty and everything pings."""
    replies = _run(run_watcher.on_threshold, _App(_Notifier()), ["30"])
    assert "at or above the digest cut" in replies[0]
    assert "notify_threshold = 70" in config_file.read_text(encoding="utf-8")


def test_a_score_outside_0_100_is_refused(db, config_file) -> None:
    replies = _run(run_watcher.on_threshold, _App(_Notifier()), ["140"])
    assert "0–100" in replies[0]
    assert "notify_threshold = 70" in config_file.read_text(encoding="utf-8")


def test_a_non_number_is_refused_without_touching_the_file(db, config_file) -> None:
    replies = _run(run_watcher.on_threshold, _App(_Notifier()), ["sixty"])
    assert "not a number" in replies[0]
    assert "notify_threshold = 70" in config_file.read_text(encoding="utf-8")


def test_a_refused_write_is_reported_not_swallowed(db, config_file,
                                                   monkeypatch) -> None:
    monkeypatch.setenv("WATCHER_MATCH_NOTIFY_THRESHOLD", "80")
    replies = _run(run_watcher.on_threshold, _App(_Notifier()), ["60"])
    assert "overrides config.toml" in replies[0]


def test_setting_a_threshold_sends_nothing(db, config_file) -> None:
    """The split that makes the command safe to mistype."""
    _scored(db, 90, 75, 55, 20)
    notifier = _Notifier()
    _run(run_watcher.on_threshold, _App(notifier), ["45"])
    assert notifier.sent == []


# --------------------------------------------------------------------------
# /recheck
# --------------------------------------------------------------------------

def test_recheck_sends_what_the_current_cut_qualifies(db) -> None:
    _scored(db, 90, 75, 55, 20)
    notifier = _Notifier()
    replies = _run(run_watcher.on_recheck, _App(notifier))

    assert notifier.sent == [90, 75]
    assert "Sent 2 of 2" in replies[-1]


def test_recheck_picks_up_a_lowered_cut(db) -> None:
    """The two commands composed: move the cut, then send what it qualified."""
    _scored(db, 90, 75, 55, 20)
    notifier = _Notifier(Config(match={"notify_threshold": 50}))
    _run(run_watcher.on_recheck, _App(notifier))
    assert notifier.sent == [90, 75, 55]


def test_recheck_never_sends_the_same_posting_twice(db) -> None:
    _scored(db, 90, 75)
    notifier = _Notifier()
    _run(run_watcher.on_recheck, _App(notifier))
    replies = _run(run_watcher.on_recheck, _App(notifier))

    assert notifier.sent == [90, 75], "a second run re-pinged"
    assert "Nothing new to send" in replies[0]


def test_nothing_to_send_says_when_the_digest_goes(db) -> None:
    """Two below the ping cut is not "nothing" — it is "not yet"."""
    _scored(db, 55, 45)
    replies = _run(run_watcher.on_recheck, _App(_Notifier()))
    assert "Nothing new to send" in replies[0]
    assert "2 posting(s) sit in the digest band" in replies[0]
    assert "19:05" in replies[0]


def test_the_overflow_is_held_for_the_digest_not_dropped(db) -> None:
    """`send_instant` leaves them unrecorded on purpose — say so."""
    _scored(db, *range(71, 100))
    notifier = _Notifier()
    replies = _run(run_watcher.on_recheck, _App(notifier), ["5"])

    assert len(notifier.sent) == 5
    assert "held back for the digest" in replies[-1]
    assert "/recheck" in replies[-1], "the way to send the rest"


def test_the_cap_is_bounded_however_large_the_argument(db) -> None:
    _scored(db, 90)
    notifier = _Notifier()
    _run(run_watcher.on_recheck, _App(notifier), ["100000"])
    assert notifier.instant_limit is not None
    assert notifier.instant_limit <= 100


def test_a_failed_send_is_reported(db) -> None:
    _scored(db, 90)
    notifier = _Notifier()
    notifier.fail = True
    replies = _run(run_watcher.on_recheck, _App(notifier))
    assert "failed" in replies[-1]


def test_recheck_does_not_rescore(db, monkeypatch) -> None:
    """Verdicts are already on record; re-judging thousands from a chat command
    is not something that should be one word away."""
    called = []
    monkeypatch.setattr(run_watcher.matcher, "match_pending",
                        lambda *a, **k: called.append(1))
    _scored(db, 90)
    _run(run_watcher.on_recheck, _App(_Notifier()))
    assert called == []
