"""`/status` and `/restart`, answered from the chat the watcher already talks to.

Nothing supervises this process. `watcherctl start` launches it detached and
finds it again by a marker in its command line, so "restart" from Telegram has
to mean the watcher relaunching itself — stop polling, put a fresh process in
this one's place, and tell the chat about it from the other side.

Two things make that dangerous enough to be worth tests. A restart kills any
build in flight, and the next start's recovery erases what that build had
written; so an in-flight build must refuse the command unless the user says
`force`. And the confirmation has to survive the process boundary, which only a
file on disk can do — one that is removed whether or not it was readable, or
the *next* ordinary start announces itself as a restart that never happened.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

import run_watcher
from watcher import config as watcher_config
from watcher.config import Config
from watcher.notifier import format_status


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------

class _Notifier:
    chat_id = "-1001234567890"
    config = Config()

    def __init__(self) -> None:
        self.notices: list[str] = []

    async def send_notice(self, text: str, topic: str | None = None) -> None:
        self.notices.append(text)


class _App:
    def __init__(self) -> None:
        self.bot_data: dict = {"notifier": _Notifier()}
        self.stopped = False

    def stop_running(self) -> None:
        self.stopped = True


class _Message:
    chat_id = -1001234567890
    message_thread_id = None

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


def _run(handler, app: _App, args: list[str] | None = None) -> _Message:
    message = _Message()
    asyncio.run(handler(_Update(message), _Context(app, args)))
    return message


# --------------------------------------------------------------------------
# /status
# --------------------------------------------------------------------------

def test_status_reports_the_last_cycle(monkeypatch) -> None:
    monkeypatch.setattr(run_watcher, "_unfinished", list)
    app = _App()
    app.bot_data["last_cycle"] = {"fetched": 41, "stored": 3, "notified": 1}
    app.bot_data["started_at"] = dt.datetime.now() - dt.timedelta(hours=5)

    text = _run(run_watcher.on_status, app).replies[0]
    assert "41 fetched" in text
    assert "3 new" in text
    assert "1 pinged" in text
    assert "up 5h" in text


def test_status_separates_running_builds_from_queued_ones(monkeypatch) -> None:
    monkeypatch.setattr(run_watcher, "_unfinished", lambda: [
        ("running", "Acme — Data Scientist"),
        ("queued", "Globex — ML Engineer"),
        ("queued", "Initech — Analyst"),
    ])
    app = _App()
    app.bot_data["notifier"].config = Config(build={"enabled": True})

    text = _run(run_watcher.on_status, app).replies[0]
    assert "1 running, 2 queued" in text


def test_status_survives_an_unreadable_database(monkeypatch) -> None:
    """The command answers the user; a database hiccup must degrade it, not eat it."""
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_watcher.store, "connect", broken)
    app = _App()
    assert _run(run_watcher.on_status, app).replies, "no answer at all"


def test_a_watcher_with_builds_off_says_so() -> None:
    text = format_status(Config(build={"enabled": False}), {}, {}, None)
    assert "approvals recorded, nothing built" in text


def test_status_names_the_topics_only_when_there_are_some() -> None:
    plain = format_status(Config(), {}, {}, None)
    assert "Topics" not in plain

    routed = format_status(Config(notify={"topics": {"new_posting": 12}}),
                           {}, {}, None)
    assert "Topics: 1 of 5 routed" in routed


# --------------------------------------------------------------------------
# /restart
# --------------------------------------------------------------------------

def test_restart_refuses_while_a_build_is_running(monkeypatch, tmp_path) -> None:
    """The build would be killed and its half-written folder erased."""
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", tmp_path / "restart.json")
    monkeypatch.setattr(run_watcher, "_unfinished", lambda: [
        ("running", "Acme — Data Scientist")])
    app = _App()

    text = _run(run_watcher.on_restart, app).replies[0]
    assert "Acme — Data Scientist" in text
    assert "/restart force" in text
    assert app.stopped is False, "the refusal must not restart anyway"
    assert not (tmp_path / "restart.json").exists()


def test_restart_force_goes_ahead(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", tmp_path / "restart.json")
    monkeypatch.setattr(run_watcher, "_unfinished", lambda: [
        ("running", "Acme — Data Scientist")])
    app = _App()

    _run(run_watcher.on_restart, app, ["force"])
    assert app.stopped is True
    assert app.bot_data["restart"] is True


def test_an_idle_restart_needs_no_force(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", tmp_path / "restart.json")
    monkeypatch.setattr(run_watcher, "_unfinished", list)
    app = _App()

    text = _run(run_watcher.on_restart, app).replies[0]
    assert "0 builds in flight" in text
    assert app.stopped is True


def test_a_build_title_with_markup_is_escaped(monkeypatch, tmp_path) -> None:
    """Posting titles come off the web; one with a `<` would break the message."""
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", tmp_path / "restart.json")
    monkeypatch.setattr(run_watcher, "_unfinished", lambda: [
        ("running", "Acme <b> — R&D Lead")])

    text = _run(run_watcher.on_restart, _App()).replies[0]
    assert "&lt;b&gt;" in text
    assert "R&amp;D" in text


def test_the_marker_records_who_asked(monkeypatch, tmp_path) -> None:
    """The answer is sent by a different process, so the file is the only link."""
    marker = tmp_path / "restart.json"
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", marker)
    monkeypatch.setattr(run_watcher, "_unfinished", list)

    _run(run_watcher.on_restart, _App())
    written = json.loads(marker.read_text(encoding="utf-8"))
    assert written["chat_id"] == "-1001234567890"
    assert written["requested_at"]


def test_an_unwritable_marker_still_restarts(monkeypatch, tmp_path) -> None:
    """Losing the confirmation is a nuisance; not restarting is a failed command."""
    blocked = tmp_path / "state"
    blocked.write_text("this is a file, not the state directory", encoding="utf-8")
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", blocked / "restart.json")
    monkeypatch.setattr(run_watcher, "_unfinished", list)

    app = _App()
    _run(run_watcher.on_restart, app)
    assert app.stopped is True


# --------------------------------------------------------------------------
# the other side of the restart
# --------------------------------------------------------------------------

def test_the_new_process_confirms_the_restart(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "restart.json"
    marker.write_text(json.dumps({"chat_id": "-1001234567890"}), encoding="utf-8")
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", marker)

    app = _App()
    asyncio.run(run_watcher._report_restart(app))
    assert "back up" in app.bot_data["notifier"].notices[0]
    assert not marker.exists(), "a marker that survives makes the next start lie"


def test_an_ordinary_start_says_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", tmp_path / "restart.json")
    app = _App()
    asyncio.run(run_watcher._report_restart(app))
    assert app.bot_data["notifier"].notices == []


def test_a_corrupt_marker_is_still_cleared(monkeypatch, tmp_path) -> None:
    """A half-written file must not make every later start announce a restart."""
    marker = tmp_path / "restart.json"
    marker.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", marker)

    app = _App()
    asyncio.run(run_watcher._report_restart(app))
    assert not marker.exists()
    # Still reported: the file existing at all means a restart was asked for.
    assert app.bot_data["notifier"].notices


def test_a_marker_from_another_chat_is_not_announced(monkeypatch, tmp_path) -> None:
    """TELEGRAM_CHAT_ID changed in between; whoever asked is not on this end."""
    marker = tmp_path / "restart.json"
    marker.write_text(json.dumps({"chat_id": "-100999"}), encoding="utf-8")
    monkeypatch.setattr(run_watcher, "RESTART_MARKER_PATH", marker)

    app = _App()
    asyncio.run(run_watcher._report_restart(app))
    assert app.bot_data["notifier"].notices == []
    assert not marker.exists()


# --------------------------------------------------------------------------
# relaunch
# --------------------------------------------------------------------------

def test_relaunch_repeats_this_processs_own_command_line(monkeypatch) -> None:
    """Same interpreter, same script, same flags — `watcherctl` finds it again
    by the `run_watcher` marker in that command line."""
    seen: list[list[str]] = []
    monkeypatch.setattr(run_watcher.sys, "argv", ["run_watcher.py", "--verbose"])
    monkeypatch.setattr(run_watcher.os, "name", "nt")
    monkeypatch.setattr(run_watcher.subprocess, "Popen",
                        lambda argv, **kwargs: seen.append(argv))

    run_watcher.relaunch()
    argv = seen[0]
    assert argv[0] == run_watcher.sys.executable
    assert argv[1].endswith("run_watcher.py")
    assert argv[2] == "--verbose"


def test_the_script_path_is_absolute(monkeypatch) -> None:
    """`Popen` inherits the cwd, but an absolute path removes the question."""
    import os.path

    seen: list[list[str]] = []
    monkeypatch.setattr(run_watcher.sys, "argv", ["run_watcher.py"])
    monkeypatch.setattr(run_watcher.os, "name", "nt")
    monkeypatch.setattr(run_watcher.subprocess, "Popen",
                        lambda argv, **kwargs: seen.append(argv))

    run_watcher.relaunch()
    assert os.path.isabs(seen[0][1])


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

class _Queue:
    def run_repeating(self, *args, **kwargs) -> None:
        pass

    def run_daily(self, *args, **kwargs) -> None:
        pass


class _WiredApp:
    def __init__(self) -> None:
        self.job_queue = _Queue()
        self.bot_data: dict = {}
        self.handlers: list = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)


class _BuildConfig:
    interval_minutes = 20
    digest_hour = 19
    heartbeat_hour = 9
    build_enabled = False
    kb_enabled = False
    kb_hour = 10
    kb_weekday = 6
    digest_at = (digest_hour, watcher_config.DIGEST_MINUTE)
    heartbeat_at = (heartbeat_hour, watcher_config.HEARTBEAT_MINUTE)
    kb_at = (kb_hour, watcher_config.KB_MINUTE)


class _BuildNotifier:
    chat_id = "12345"
    config = _BuildConfig()


def _wire(monkeypatch) -> _WiredApp:
    app = _WiredApp()

    class _AppBuilder:
        def token(self, *a, **k):
            return self

        def post_init(self, *a, **k):
            return self

        def build(self):
            return app

    monkeypatch.setattr(run_watcher, "Application",
                        type("F", (), {"builder": staticmethod(lambda: _AppBuilder())}))
    monkeypatch.setattr(run_watcher, "require_env", lambda name: "token")
    run_watcher.build_app(_BuildNotifier())
    return app


def test_the_commands_are_registered(monkeypatch) -> None:
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    commands = {name for handler in app.handlers
                if isinstance(handler, CommandHandler)
                for name in handler.commands}
    assert commands == {"status", "threshold", "recheck", "restart"}


def test_every_registered_command_is_offered_in_telegrams_menu(monkeypatch) -> None:
    """A command nobody can discover may as well not be there."""
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    registered = {name for handler in app.handlers
                  if isinstance(handler, CommandHandler)
                  for name in handler.commands}
    assert {name for name, _ in run_watcher.BOT_COMMANDS} == registered


def test_the_commands_are_checked_before_the_reply_handler(monkeypatch) -> None:
    """A command sent as a reply is still a command, not an approval."""
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    first_command = next(i for i, h in enumerate(app.handlers)
                         if isinstance(h, CommandHandler))
    first_message = next(i for i, h in enumerate(app.handlers)
                         if not isinstance(h, CommandHandler))
    assert first_command < first_message


def _incoming(is_topic_message: bool, chat_id: int = 12345):
    """A real Update, so the filter under test sees what Telegram would send."""
    from telegram import Chat, Message, Update

    return Update(update_id=1, message=Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=chat_id, type=Chat.SUPERGROUP),
        text="/status",
        is_topic_message=is_topic_message,
    ))


@pytest.mark.parametrize("in_topic,answered", [(False, True), (True, False)])
def test_commands_answer_from_general_but_not_inside_a_topic(
        monkeypatch, in_topic: bool, answered: bool) -> None:
    """Inside a posting topic, `/status` is far more likely a mis-sent reply.

    `is_topic_message` is unset both in General and in a chat with no forum at
    all, which is exactly the pair that should be answered.
    """
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    handler = next(h for h in app.handlers if isinstance(h, CommandHandler))
    assert bool(handler.filters.check_update(_incoming(in_topic))) is answered


def test_commands_ignore_another_chat(monkeypatch) -> None:
    """The bot may sit in more than one chat; only the configured one commands it."""
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    handler = next(h for h in app.handlers if isinstance(h, CommandHandler))
    assert not handler.filters.check_update(_incoming(False, chat_id=999))
