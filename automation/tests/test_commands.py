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
from watcher import normalize, store
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
        self.targeted: list[tuple] = []

    async def send_notice(self, text: str, topic: str | None = None) -> None:
        self.notices.append(text)

    async def send_targeted(self, company, title, url, score, note) -> int:
        self.targeted.append((company, title, url, score, note))
        return 900


class _App:
    def __init__(self) -> None:
        self.bot_data: dict = {"notifier": _Notifier()}
        self.stopped = False

    def stop_running(self) -> None:
        self.stopped = True


class _Message:
    chat_id = -1001234567890
    message_thread_id = None
    message_id = 1

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


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A database of this test's own for the commands that read one."""
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    return path


def _posting(posting_id: str = "p1", *, company: str = "Acme",
             title: str = "Data Scientist",
             url: str = "https://acme.test/p1") -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
                                     canonical_url, company, title,
                                     first_seen_at, description)
               VALUES (?, ?, 'ats:acme', 'greenhouse', ?, ?, ?, ?, ?, '')""",
            (posting_id, f"{company}|{title}", url,
             normalize.canonical_url(url), company, title, now))


def _question(posting_id: str = "p1", *, kind: str = "stop_and_ask",
              question: str = "Sponsorship is not available. Still apply?",
              folder: str = "") -> int:
    with store.connect() as conn:
        build = store.queue_build(conn, posting_id)
        return store.ask_question(conn, build, posting_id, kind, "-100123",
                                  501, thread_id=162, question=question,
                                  folder=folder)


class _Worker:
    """A builder that records what it was asked to do and nothing else."""

    def __init__(self, *, running: bool = False, queued=None) -> None:
        self.running = running
        self.queued: list[tuple[str, str, int | None]] = []
        self.dropped: list[str] = []
        self._next = queued

    async def enqueue(self, posting_id, note, reply_message_id=None,
                      override_duplicate: bool = False) -> int:
        self.queued.append((posting_id, note, reply_message_id))
        return 0

    async def cancel_current(self) -> bool:
        return self.running

    def drop_queued(self, posting_id: str = ""):
        self.dropped.append(posting_id)
        return self._next


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
# /pending
# --------------------------------------------------------------------------

def test_pending_on_a_quiet_watcher_says_so(db) -> None:
    app = _App()
    text = _run(run_watcher.on_pending, app).replies[0]
    assert "Nothing waiting on you" in text


def test_pending_says_builds_are_off_only_when_they_are(db) -> None:
    """A silent `/pending` on a watcher that cannot build anything reads as
    "nothing to do" when the truth is "nothing will ever happen"."""
    app = _App()
    assert "Builds are switched off" in _run(run_watcher.on_pending, app).replies[0]

    app.bot_data["notifier"].config = Config(build={"enabled": True})
    assert "Builds are switched off" not in _run(
        run_watcher.on_pending, app).replies[0]


def test_pending_lists_an_open_question_with_what_it_asked(db) -> None:
    _posting()
    _question()

    text = _run(run_watcher.on_pending, _App()).replies[0]
    assert "Waiting on an answer" in text
    assert "Acme" in text and "Data Scientist" in text
    assert "stopped to ask" in text
    assert "Sponsorship is not available" in text, (
        "the question itself is the only thing that says what to answer")
    assert "1." in text, "the number is the handle /cancel takes"


def test_pending_distinguishes_a_declined_duplicate(db) -> None:
    _posting()
    _question(kind="duplicate", question="Already applied 2026-06-24.")

    assert "duplicate" in _run(run_watcher.on_pending, _App()).replies[0]


def test_pending_shows_a_build_in_flight(db) -> None:
    _posting()
    with store.connect() as conn:
        build = store.queue_build(conn, "p1")
        store.mark_build_running(conn, build, "logs/builds/b.log")

    text = _run(run_watcher.on_pending, _App()).replies[0]
    assert "Builds in flight" in text
    assert "Acme — Data Scientist" in text
    assert "running" in text


def test_pending_shows_a_ping_that_was_never_answered(db) -> None:
    """The oldest of the three kinds of waiting, and the one that had nowhere to
    be seen: a posting pinged into the chat and simply scrolled past."""
    _posting()
    with store.connect() as conn:
        store.record_notification(conn, "p1", "-100123", 42, "instant")

    text = _run(run_watcher.on_pending, _App()).replies[0]
    assert "Pinged, never answered" in text
    assert "Acme — Data Scientist" in text


def test_an_answered_ping_is_not_still_pending(db) -> None:
    _posting()
    with store.connect() as conn:
        store.record_notification(conn, "p1", "-100123", 42, "instant")
        store.record_decision(conn, "p1", "skip", "", 43)

    assert "Nothing waiting on you" in _run(run_watcher.on_pending, _App()).replies[0]


def test_a_build_message_does_not_look_like_an_unanswered_ping(db) -> None:
    """Every build message is recorded now so it can be replied to. They are
    recorded under their own kind precisely so this list — and the ping
    suppression in `unnotified_in_band` — keep meaning what they meant."""
    _posting()
    with store.connect() as conn:
        store.record_notification(conn, "p1", "-100123", 77, "build")

    assert "Nothing waiting on you" in _run(run_watcher.on_pending, _App()).replies[0]


def test_pending_survives_an_unreadable_database(monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_watcher.store, "connect", broken)
    assert _run(run_watcher.on_pending, _App()).replies, "no answer at all"


# --------------------------------------------------------------------------
# /build
# --------------------------------------------------------------------------

def _build_app(db_path=None) -> _App:
    app = _App()
    app.bot_data["notifier"].config = Config(build={"enabled": True})
    app.bot_data["builder"] = _Worker()
    return app


def test_build_queues_a_url_the_watcher_already_knows(db) -> None:
    _posting(url="https://acme.test/p1")
    app = _build_app()

    text = _run(run_watcher.on_build, app,
                ["https://acme.test/p1"]).replies[0]

    worker = app.bot_data["builder"]
    assert worker.queued == [("p1", "", 1)]
    assert "Queued" in text and "Data Scientist" in text
    assert app.bot_data["notifier"].targeted, (
        "Targeted is the log of what was decided, however it was decided")
    with store.connect() as conn:
        assert store.last_decision(conn, "p1")["action"] == "approve"


def test_build_passes_a_trailing_note_to_the_pipeline(db) -> None:
    _posting()
    app = _build_app()

    _run(run_watcher.on_build, app,
         ["https://acme.test/p1", "|", "|", "|", "add", "German"])

    assert app.bot_data["builder"].queued == [("p1", "add German", 1)]


def test_an_unknown_url_on_its_own_is_refused(db) -> None:
    """Guessing the employer and role produces a build that renders into the
    wrong folder and is then reported as having produced nothing."""
    app = _build_app()

    text = _run(run_watcher.on_build, app, ["https://elsewhere.test/job/9"]).replies[0]

    assert "employer" in text and "role" in text
    assert app.bot_data["builder"].queued == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 0


def test_an_unknown_url_with_the_employer_and_role_is_recorded_and_built(db) -> None:
    app = _build_app()

    text = _run(run_watcher.on_build, app,
                ["https://elsewhere.test/job/9", "|", "Globex", "|",
                 "ML", "Engineer"]).replies[0]

    assert "Queued" in text and "Globex" in text
    with store.connect() as conn:
        row = conn.execute("SELECT company, title, source FROM postings"
                           ).fetchone()
    assert (row["company"], row["title"]) == ("Globex", "ML Engineer")
    assert row["source"] == "manual", "so it is obvious it came from a command"
    assert len(app.bot_data["builder"].queued) == 1


def test_build_without_a_url_explains_itself(db) -> None:
    app = _build_app()
    text = _run(run_watcher.on_build, app, []).replies[0]
    assert "/build https://" in text
    assert app.bot_data["builder"].queued == []


def test_build_refuses_something_that_is_not_a_url(db) -> None:
    app = _build_app()
    text = _run(run_watcher.on_build, app, ["Acme", "|", "Data", "Scientist"]).replies[0]
    assert "/build https://" in text
    assert app.bot_data["builder"].queued == []


def test_build_says_nothing_will_happen_when_builds_are_off(db) -> None:
    _posting()
    app = _build_app()
    app.bot_data["notifier"].config = Config(build={"enabled": False})

    text = _run(run_watcher.on_build, app, ["https://acme.test/p1"]).replies[0]

    assert app.bot_data["builder"].queued == []
    assert text, "silence is the one answer a command may not give"


# --------------------------------------------------------------------------
# /cancel
# --------------------------------------------------------------------------

def test_cancel_stops_the_build_that_is_running(db) -> None:
    app = _App()
    app.bot_data["builder"] = _Worker(running=True)

    text = _run(run_watcher.on_cancel, app).replies[0]

    assert "Stopping" in text
    assert app.bot_data["builder"].dropped == [], (
        "the queue is only reached when nothing is running")


def test_cancel_with_nothing_running_drops_the_next_queued(db) -> None:
    _posting("p2", company="Globex", title="ML Engineer",
             url="https://acme.test/p2")
    app = _App()
    app.bot_data["builder"] = _Worker(
        queued=run_watcher.builder_mod.Job("p2", "", None, 1))

    text = _run(run_watcher.on_cancel, app).replies[0]

    assert "Dropped" in text
    assert "Globex" in text and "ML Engineer" in text, (
        "naming it is what makes a mis-aimed cancel obvious")


def test_cancel_with_nothing_to_cancel_points_at_pending(db) -> None:
    app = _App()
    app.bot_data["builder"] = _Worker()

    text = _run(run_watcher.on_cancel, app).replies[0]

    assert "Nothing is building or queued" in text
    assert "/pending" in text


def test_cancel_closes_a_numbered_question(db, monkeypatch) -> None:
    _posting()
    qid = _question(folder="/w/2026/Acme/2026-08-06 - Data Scientist")
    cleaned: list[tuple] = []
    monkeypatch.setattr(run_watcher.builder_mod, "clean_up",
                        lambda folder, reason: cleaned.append((folder, reason))
                        or "removed the folder")
    app = _App()
    app.bot_data["builder"] = _Worker()

    text = _run(run_watcher.on_cancel, app, ["1"]).replies[0]

    assert "Closed" in text and "Acme" in text
    assert cleaned and cleaned[0][0].endswith("2026-08-06 - Data Scientist")
    with store.connect() as conn:
        assert store.open_question(conn, qid) is None
        assert store.open_questions(conn) == []


def test_cancelling_a_question_that_is_not_there(db) -> None:
    _posting()
    _question()
    app = _App()
    app.bot_data["builder"] = _Worker()

    text = _run(run_watcher.on_cancel, app, ["4"]).replies[0]

    assert "no question 4" in text
    with store.connect() as conn:
        assert len(store.open_questions(conn)) == 1, "nothing was closed"


def test_cancel_with_something_that_is_not_a_number(db) -> None:
    app = _App()
    app.bot_data["builder"] = _Worker(running=True)

    text = _run(run_watcher.on_cancel, app, ["build"]).replies[0]

    assert "not a number" in text
    assert app.bot_data["builder"].dropped == []


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
        self.groups: dict[int, list] = {}

    def add_handler(self, handler, group: int = 0) -> None:
        self.groups.setdefault(group, []).append(handler)

    @property
    def handlers(self) -> list:
        """Group 0 — the handlers that decide what a message means.

        Negative groups run first and consume nothing, so they take no part in
        the ordering the tests below are about.
        """
        return self.groups.get(0, [])


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
    assert commands == {"status", "pending", "build", "cancel", "threshold",
                        "recheck", "rescore", "restart"}


def test_every_registered_command_is_offered_in_telegrams_menu(monkeypatch) -> None:
    """A command nobody can discover may as well not be there."""
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    registered = {name for handler in app.handlers
                  if isinstance(handler, CommandHandler)
                  for name in handler.commands}
    assert {name for name, _ in run_watcher.BOT_COMMANDS} == registered


class _Menu:
    """A bot that records what reached Telegram's command menu."""

    def __init__(self, *, breaks: bool = False) -> None:
        self.published: list = []
        self.cleared: list[str] = []
        self._breaks = breaks

    async def set_my_commands(self, commands):
        if self._breaks:
            raise RuntimeError("telegram fell over")
        self.published = list(commands)

    async def delete_my_commands(self, scope=None):
        self.cleared.append(type(scope).__name__)


def _publish(**kwargs) -> _Menu:
    app = type("_App", (), {})()
    app.bot = menu = _Menu(**kwargs)
    asyncio.run(run_watcher._publish_commands(app))
    return menu


def test_the_menu_is_published() -> None:
    assert _publish().published == run_watcher.BOT_COMMANDS


def test_the_scopes_a_group_reads_first_are_cleared() -> None:
    """Otherwise `default` is never reached and the group menu freezes.

    Telegram resolves the "/" menu most-specific-scope-first. Four commands
    left in `all_group_chats` by an older version outranked every publish
    since — the private chat showed the full list, the group showed the stale
    one, and both calls were succeeding.
    """
    assert set(_publish().cleared) == {"BotCommandScopeAllGroupChats",
                                       "BotCommandScopeAllChatAdministrators"}


def test_a_broken_menu_call_does_not_take_the_watcher_down(caplog) -> None:
    """A stale "/" menu is cosmetic. Not starting is not."""
    assert _publish(breaks=True).published == []


def test_the_commands_are_checked_before_the_reply_handler(monkeypatch) -> None:
    """A command sent as a reply is still a command, not an approval."""
    from telegram.ext import CommandHandler

    app = _wire(monkeypatch)
    first_command = next(i for i, h in enumerate(app.handlers)
                         if isinstance(h, CommandHandler))
    first_message = next(i for i, h in enumerate(app.handlers)
                         if not isinstance(h, CommandHandler))
    assert first_command < first_message


def test_incoming_messages_are_logged_before_anything_claims_them(
        monkeypatch) -> None:
    """A group ahead of 0, so it observes and never consumes.

    Two live failures were undiagnosable because the watcher wrote nothing
    about what arrived: a file that matched no handler, and a message in a
    topic routed as a reply. Both read as "I sent something and nothing
    happened" from outside.
    """
    app = _wire(monkeypatch)
    logging_groups = [group for group, handlers in app.groups.items()
                      if any(h.callback is run_watcher._log_incoming
                             for h in handlers)]
    assert logging_groups == [-1]
    assert all(group >= 0 for group in app.groups if group != -1)


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
