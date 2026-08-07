"""The machinery around a build: what it is spawned with, and what it reports.

Four other files already cover the decisions a build makes — `test_stop_and_ask`,
`test_transient_and_salvage`, `test_ready_announce`, `test_build_progress`. This
covers the parts underneath them, which are the parts nobody looks at until one
of them costs a run:

  the command    a build that starts without its guard file is a build running
                 `bypassPermissions` against the whole disk, so `command_for`
                 refuses rather than degrades
  the stream     one oversized NDJSON line used to propagate out of the worker,
                 mark a nearly-finished build failed, and leave the CLI blocked
                 on a pipe nobody was reading
  the queue      the queue lives in memory, so everything about a restart —
                 recovery, the interrupted notice, a worker that died on its own
                 exception — is state nothing else can reconstruct
  the messages   the only thing that ever reaches the user

Nothing here spawns a real process, touches a real board, or writes outside
`tmp_path`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from watcher import builder, dedupe
from watcher import store as real_store
from watcher.builder import (DONE, DUPLICATE, FAILED, INCOMPLETE, INTERRUPTED,
                             NEEDS_DECISION, Builder, Job, Outcome,
                             build_prompt, check_duplicate, clean_up,
                             command_for, duplicate_message, locate_output,
                             log_path_for, relative, result_message)
from watcher.claude_cli import ClaudeError
from watcher.config import Config


# --------------------------------------------------------------------------
# shared doubles
# --------------------------------------------------------------------------

def config(**build) -> Config:
    """A Config whose `[build]` section is whatever the test needs."""
    return Config(build=build)


class Notifier:
    """Records what a build said, and to which topic."""

    def __init__(self, *, edit_alive: bool = True) -> None:
        self.config = config()
        self.chat_id = "-1001234567890"
        self.sent: list[tuple[str, str | None, int | None]] = []
        self.edits: list[tuple[int, str]] = []
        self.notices: list[str] = []
        self.next_id = 500
        self._edit_alive = edit_alive

    async def send(self, text, reply_to=None, topic=None) -> int:
        self.sent.append((text, topic, reply_to))
        self.next_id += 1
        return self.next_id

    async def edit(self, message_id, text) -> bool:
        self.edits.append((message_id, text))
        return self._edit_alive

    async def send_notice(self, text, topic=None) -> None:
        self.notices.append(text)


class Identity:
    file_prefix = "A Candidate"

    def doc_name(self, label: str, ext: str) -> str:
        return f"{self.file_prefix} - {label}{ext}"


@pytest.fixture()
def identity(monkeypatch):
    """`identity.toml` is git-ignored, so no checkout may rely on the real one."""
    monkeypatch.setattr(builder, "load_identity", Identity)
    monkeypatch.setattr(dedupe, "load_identity", Identity)
    return Identity()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    real_store.init_db(path)
    monkeypatch.setattr(real_store, "DB_PATH", path)
    monkeypatch.setattr(real_store, "ensure_dirs", lambda: None)
    return path


# The real `_handle`, kept because the fixture below takes the name away for
# every test. The section at the bottom calls it deliberately and directly.
_real_handle = Builder._handle


@pytest.fixture(autouse=True)
def inert_worker(monkeypatch):
    """Enforce this file's opening claim that nothing here spawns a real build.

    The queue tests hand a job to a live worker, and a live worker's whole job
    is to run a build — so they reach the real `_handle`, the real
    `command_for`, and the real CLI unless something stops them. Nothing did.

    That failure is not loud. `_handle` waits on `asyncio.to_thread`, and a task
    parked in a thread cannot be cancelled: `asyncio.run` gets to the end of the
    coroutine, tries to close the loop, and sits in `_cancel_all_tasks` waiting
    for a task that will never come back. The suite stops with no output and no
    failing test. It cost a CI job twenty minutes of silence, and it only ever
    reproduced on the runner's Python — everywhere else the thread happened to
    return before the loop closed, and the test merely looked a bit slow.

    So the default `_handle` here does nothing at all. That is the honest double
    for these tests: they assert on the queue's accounting, not on what the
    worker does with a job. A test that wants build behaviour stubs `_handle`
    with its own, which overrides this.
    """
    async def inert(self, job):
        return None

    monkeypatch.setattr(Builder, "_handle", inert)


def posting(db_path, posting_id: str = "p1", *, company: str = "Acme",
            title: str = "Data Scientist") -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with real_store.connect() as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
                                     canonical_url, company, title,
                                     first_seen_at, description)
               VALUES (?, ?, 'ats:acme', 'greenhouse', ?, ?, ?, ?, ?, '')""",
            (posting_id, f"{company}|{title}", f"https://acme.test/{posting_id}",
             f"https://acme.test/{posting_id}", company, title, now),
        )


_UNSET = object()


def hit(*, folder: str = "", origin: str = "folder", company: str = "Acme",
        title: str = "Data Scientist", similarity: float = 1.0,
        applied_on=_UNSET) -> dedupe.ExistingApplication:
    return dedupe.ExistingApplication(
        company=company, title=title,
        applied_on=dt.date(2026, 8, 1) if applied_on is _UNSET else applied_on,
        folder=folder, similarity=similarity, origin=origin)


# ===========================================================================
# naming: the slug and the log path
# ===========================================================================

def test_a_title_becomes_a_filename_without_losing_its_shape() -> None:
    assert builder._slug("Senior Data Scientist") == "senior-data-scientist"
    assert builder._slug("AI Engineer (m/w/d)") == "ai-engineer-m-w-d"
    assert builder._slug("Führungskraft") == "f-hrungskraft"


def test_a_title_with_nothing_usable_in_it_still_names_a_file() -> None:
    """An empty filename is not a filename. Real postings have done this —
    a title of `※※※` is rare but a company of `—` is not."""
    assert builder._slug("!!!") == "build"
    assert builder._slug("") == "build"


def test_the_slug_is_cut_to_its_limit() -> None:
    assert builder._slug("a" * 200) == "a" * 40
    assert builder._slug("a" * 200, 10) == "a" * 10


def test_a_retry_does_not_overwrite_the_log_of_the_failure_that_caused_it(
        tmp_path, monkeypatch) -> None:
    """The timestamp only resolves to the second, so two attempts that start
    close together would otherwise leave one transcript between them — and the
    one lost is the failure worth reading."""
    monkeypatch.setattr(builder, "BUILD_LOG_DIR", tmp_path)

    first = log_path_for("Acme", "Data Scientist", 1)
    second = log_path_for("Acme", "Data Scientist", 2)
    third = log_path_for("Acme", "Data Scientist", 3)

    assert first.parent == tmp_path
    assert first.name.endswith("-acme-data-scientist.log")
    assert second.name.endswith("-retry1.log")
    assert third.name.endswith("-retry2.log")
    assert len({first.name, second.name, third.name}) == 3


def test_attempt_zero_is_treated_as_the_first() -> None:
    assert "retry" not in log_path_for("Acme", "X", 0).name


# ===========================================================================
# the command, and the guard it refuses to run without
# ===========================================================================

@pytest.fixture()
def guarded(tmp_path, monkeypatch):
    """A workspace with a settings file present and a resolvable CLI."""
    settings = tmp_path / "build_settings.json"
    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(builder, "BUILD_SETTINGS_PATH", settings)
    monkeypatch.setattr(builder, "resolve_bin", lambda name: f"/bin/{name}")
    monkeypatch.setattr(builder, "sync_build_settings", lambda: "up to date")
    return settings


def test_the_command_carries_the_model_the_guard_and_the_stream(guarded) -> None:
    cmd = command_for(config(model="sonnet", claude_bin="claude"))

    assert cmd[0] == "/bin/claude"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    # The progress checklist is fed from this stream; without it there is
    # nothing to feed.
    assert "stream-json" in cmd and "--verbose" in cmd
    assert cmd[cmd.index("--settings") + 1] == str(guarded)


def test_a_resumed_run_carries_the_session_it_is_continuing(guarded) -> None:
    """`--resume` is what makes an answer an answer rather than a second build.

    It goes ahead of `--settings` so the guard is still the last word on what
    the resumed run is allowed to do.
    """
    cmd = command_for(config(), resume="sess-abc")

    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-abc"
    assert cmd.index("--resume") < cmd.index("--settings")
    assert "--resume" not in command_for(config()), "an ordinary build is fresh"


def test_a_missing_settings_file_refuses_to_build(guarded, monkeypatch) -> None:
    """bypassPermissions with no deny list and no hook is strictly more
    dangerous than not building at all."""
    guarded.unlink()
    with pytest.raises(ClaudeError, match="refusing to run an unguarded build"):
        command_for(config())


def test_the_settings_file_is_re_rendered_before_every_build(
        guarded, monkeypatch, caplog) -> None:
    """An edited template applies to the next build, not the next restart."""
    calls: list[int] = []

    def sync() -> str:
        calls.append(1)
        return "rewritten"

    monkeypatch.setattr(builder, "sync_build_settings", sync)
    with caplog.at_level("INFO", logger="watcher.build"):
        command_for(config())

    assert calls == [1]
    assert "rewritten" in caplog.text


def test_a_clone_with_no_template_falls_back_to_the_file_on_disk(
        guarded, monkeypatch) -> None:
    def missing() -> str:
        raise FileNotFoundError("no template in this clone")

    monkeypatch.setattr(builder, "sync_build_settings", missing)
    assert "--settings" in command_for(config())


def test_settings_that_would_lock_the_build_out_are_refused(
        guarded, monkeypatch) -> None:
    """`sync_build_settings` raises when the deny rules would block writes
    inside the workspace. A 45-minute timeout spent being denied one tool call
    at a time is a far worse failure than this one."""
    def broken() -> str:
        raise RuntimeError("deny rules cover the workspace")

    monkeypatch.setattr(builder, "sync_build_settings", broken)
    with pytest.raises(ClaudeError, match="build settings are unusable"):
        command_for(config())


# ===========================================================================
# the prompt
# ===========================================================================

def test_the_prompt_is_the_url_and_the_note_and_nothing_else() -> None:
    """Every sentence of scaffolding here is a second source of truth competing
    with CLAUDE.md."""
    assert build_prompt("https://x.test/job", "add German") == \
        "https://x.test/job\nadd German"
    assert build_prompt("https://x.test/job", "") == "https://x.test/job"


def test_an_answer_is_the_answer_and_nothing_else() -> None:
    """A resumed session already holds CLAUDE.md, the Match Brief and the
    folder. Re-stating the URL there would be a second source of truth for a
    run that already has the first one."""
    assert builder.answer_prompt("  Go with option 2.  ") == "Go with option 2."
    assert builder.answer_prompt("") == ""


def test_a_gone_session_is_recognised_from_what_the_cli_says() -> None:
    """The only signal available: the CLI reports it in the result text, and
    the exit code is the same one every other failure uses."""
    assert builder.session_is_gone(
        "No conversation found with session ID: be5f59ca (exit 1)")
    assert builder.session_is_gone("Session ID abc123 not found")
    assert not builder.session_is_gone("API Error: 503 Service Unavailable")
    assert not builder.session_is_gone("")


# ===========================================================================
# reading the stream
# ===========================================================================

def test_only_json_objects_are_events() -> None:
    assert builder._parse_event('{"type": "result"}') == {"type": "result"}
    assert builder._parse_event('  {"type": "x"}  \n') == {"type": "x"}
    assert builder._parse_event("not json at all") is None
    assert builder._parse_event('{"type": ') is None, "half a line mid-write"
    assert builder._parse_event("[1, 2, 3]") is None, "a list is not an event"


class Handle:
    """Enough of a file handle for `_lines` to write its skip note to."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, chunk: str) -> None:
        self.text += chunk

    def flush(self) -> None:
        pass


def drain(reader, handle) -> list[bytes]:
    async def scenario() -> list[bytes]:
        return [raw async for raw in builder._lines(reader, handle)]
    return asyncio.run(scenario())


def test_ordinary_lines_pass_straight_through() -> None:
    async def scenario():
        reader = asyncio.StreamReader(limit=1024)
        reader.feed_data(b'{"a": 1}\n{"b": 2}\n')
        reader.feed_eof()
        return [raw async for raw in builder._lines(reader, Handle())]

    assert asyncio.run(scenario()) == [b'{"a": 1}\n', b'{"b": 2}\n']


def test_an_oversized_line_is_skipped_rather_than_ending_the_build() -> None:
    """The proofreader reads rendered PDF pages, and a 385 KB PNG arrives
    base64-encoded on one line. Whatever the ceiling, some day a line exceeds
    it — and `readline` signals that by raising, which previously propagated out
    of the worker and marked a nearly-finished build failed."""
    handle = Handle()

    async def scenario():
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b'{"type": "system"}\n')
        reader.feed_data(b"x" * 500 + b"\n")
        reader.feed_eof()
        return [raw async for raw in builder._lines(reader, handle)]

    kept = asyncio.run(scenario())
    assert kept == [b'{"type": "system"}\n'], "the good line still arrived"
    assert "skipped an oversized output line" in handle.text


class Awkward:
    """A stream that raises on `readline`, then again on the drain read."""

    def __init__(self) -> None:
        self.reads = 0

    async def readline(self) -> bytes:
        self.reads += 1
        if self.reads == 1:
            raise ValueError("Separator is not found, and chunk exceed the limit")
        return b""

    async def read(self, _n: int) -> bytes:
        raise RuntimeError("the transport is already gone")


def test_a_drain_that_itself_fails_does_not_end_the_build() -> None:
    """Belt and braces: the drain exists to unblock the CLI, and a drain that
    cannot run is not worth turning into the failure it was avoiding."""
    handle = Handle()
    assert drain(Awkward(), handle) == []
    assert "skipped an oversized output line" in handle.text


# ===========================================================================
# what a failing result event says
# ===========================================================================

def test_the_reason_comes_from_result_not_subtype() -> None:
    """Two builds were recorded as failed with the detail "success", because
    `subtype` describes how the turn ended rather than what went wrong."""
    event = {"type": "result", "subtype": "success", "is_error": True,
             "result": "API Error: 503 All accounts are temporarily unavailable"}
    assert builder._result_detail(event) == \
        "API Error: 503 All accounts are temporarily unavailable"


def test_subtype_is_the_fallback_when_there_is_nothing_better() -> None:
    assert builder._result_detail({"subtype": "error_during_execution"}) == \
        "error_during_execution"
    assert builder._result_detail({}) == "the CLI reported an error"


def test_a_long_reason_is_cut_to_fit_a_message() -> None:
    detail = builder._result_detail({"result": "word " * 200}, limit=50)
    assert len(detail) <= 50
    assert detail.endswith("…")


def test_a_reason_is_flattened_to_one_line() -> None:
    detail = builder._result_detail({"result": "first line\n\n  second   line"})
    assert detail == "first line second line"


# ===========================================================================
# the run itself
# ===========================================================================

class Stdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


PID = 4321


class Process:
    """A CLI whose stdout is a script and whose exit is whatever a test says.

    Built inside the running loop — a `StreamReader` binds to whichever loop is
    current when it is constructed, and every test here runs its own.
    """

    def __init__(self, lines, returncode: int = 0, *, hang: bool = False,
                 wait_error: Exception | None = None) -> None:
        self.stdout = asyncio.StreamReader(limit=builder.STREAM_LIMIT)
        for line in lines:
            self.stdout.feed_data(line.encode("utf-8") if isinstance(line, str)
                                  else line)
        self.stdout.feed_eof()
        self.stdin = Stdin()
        self.returncode = returncode
        self.pid = PID
        self._hang = hang
        self._wait_error = wait_error

    async def wait(self) -> int:
        if self._wait_error is not None:
            raise self._wait_error
        if self._hang:
            await asyncio.sleep(3600)
        return self.returncode


def event(**fields) -> str:
    return json.dumps(fields) + "\n"


@pytest.fixture()
def spawnable(monkeypatch):
    """`_spawn` with the process, the command, and the kill all under control."""
    state = type("S", (), {"process": None, "killed": [], "cmd": ["claude"],
                           "make": None})()

    async def create(*cmd, **kwargs):
        state.launched = (cmd, kwargs)
        state.process = state.make()
        return state.process

    def command_for(cfg, resume=""):
        state.resume = resume
        return state.cmd + (["--resume", resume] if resume else [])

    monkeypatch.setattr(builder, "command_for", command_for)
    monkeypatch.setattr(builder.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(builder, "_kill_tree", state.killed.append)
    return state


def spawn(spawnable, log_file: Path, make_process, *, minutes=45,
          on_event=None, resume="", on_session=None):
    spawnable.make = make_process
    return asyncio.run(builder._spawn(
        "https://x.test/job", config(timeout_minutes=minutes), log_file,
        on_event=on_event, resume=resume, on_session=on_session))


def test_a_clean_run_reports_success_and_writes_its_transcript(
        spawnable, tmp_path) -> None:
    log_file = tmp_path / "build.log"
    run = spawn(spawnable, log_file, lambda: Process([
        event(type="system", subtype="init"),
        event(type="result", is_error=False, result="All documents rendered."),
    ]))

    assert (run.ok, run.detail) == (True, "")
    assert run.closing == "All documents rendered."
    written = log_file.read_text(encoding="utf-8")
    assert written.startswith("$ claude")
    assert "https://x.test/job" in written, "the prompt is part of the record"
    assert '"type": "result"' in written or '"type":"result"' in written
    # The prompt goes in on stdin and the pipe is closed, or the CLI waits.
    assert spawnable.process.stdin.written == b"https://x.test/job"
    assert spawnable.process.stdin.closed


def test_a_failing_result_carries_its_reason_out(spawnable, tmp_path) -> None:
    run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="result", is_error=True, subtype="success",
              result="API Error: Server error mid-response"),
    ], returncode=1))

    assert run.ok is False
    assert "Server error mid-response" in run.detail
    assert "(exit 1)" in run.detail, "the exit code rides alongside, not instead"


def test_a_non_zero_exit_after_a_clean_result_is_still_a_failure(
        spawnable, tmp_path) -> None:
    """The CLI has reported success on the stream and then died on the way out.
    Recording that as a completed build is how a run with no PDFs gets filed as
    done."""
    run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="result", is_error=False, result="Done."),
    ], returncode=1))

    assert run.ok is False
    assert run.detail == "exit 1"


def test_the_closing_words_keep_the_last_few_turns_not_just_the_last(
        spawnable, tmp_path) -> None:
    """The orchestrator ends a turn every time it hands off to background
    subagents, so a build's question is rarely in its final turn — one run asked
    whether to claim experience the profile does not carry and signed off with
    "still holding on your decision from above"."""
    run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="result", is_error=False, result="Phase A dispatched."),
        event(type="result", is_error=False,
              result="Should I claim agent-framework experience?"),
        event(type="result", is_error=False, result="Still holding."),
    ]))

    assert "Should I claim agent-framework experience?" in run.closing
    assert "Still holding." in run.closing


def test_a_repeated_turn_is_not_recorded_twice(spawnable, tmp_path) -> None:
    run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="result", is_error=False, result="Same words."),
        event(type="result", is_error=False, result="Same words."),
    ]))
    assert run.closing == "Same words."


def test_the_closing_words_are_capped(spawnable, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(builder, "CLOSING_CHARS", 40)
    run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="result", is_error=False, result="word " * 100),
    ]))
    assert len(run.closing) <= 40 and run.closing.endswith("…")


def test_a_hung_build_is_killed_along_with_everything_it_started(
        spawnable, tmp_path) -> None:
    """A bare terminate leaves node and latexmk alive holding the deliverable
    folder open, and the next attempt then fails for an unrelated reason."""
    run = spawn(spawnable, tmp_path / "b.log",
                lambda: Process([], hang=True), minutes=0.001)

    assert run.ok is False
    assert "timed out after" in run.detail
    assert spawnable.killed == [PID]


def test_a_fault_mid_stream_kills_the_tree_before_it_propagates(
        spawnable, tmp_path) -> None:
    """Anything that stops us reading stdout strands the CLI on a pipe nobody
    drains, and it sits there until the machine reboots."""
    with pytest.raises(RuntimeError, match="transport died"):
        spawn(spawnable, tmp_path / "b.log",
              lambda: Process([], wait_error=RuntimeError("transport died")))
    assert spawnable.killed == [PID]


def test_every_event_is_offered_to_the_progress_hook(spawnable,
                                                     tmp_path) -> None:
    seen: list[dict] = []
    spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="system", subtype="init"),
        "not json\n",
        event(type="result", is_error=False, result="Done."),
    ]), on_event=seen.append)

    assert [e.get("type") for e in seen] == ["system", "result"]


def test_the_session_id_is_taken_off_the_stream(spawnable, tmp_path) -> None:
    """The id arrives on the init event and is repeated on every result. It is
    what turns "the run stopped to ask" into something answerable."""
    run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="system", subtype="init", session_id="sess-abc"),
        event(type="result", is_error=False, session_id="sess-abc",
              result="Should I claim that?"),
    ]))

    assert run.session_id == "sess-abc"


def test_the_session_is_handed_over_the_moment_it_is_known(
        spawnable, tmp_path) -> None:
    """Not on the way out. A build that is killed or times out is exactly the
    one worth resuming, and waiting for the return value loses the id in every
    case where it is worth most."""
    seen: list[str] = []
    spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="system", subtype="init", session_id="sess-abc"),
        event(type="result", is_error=False, session_id="sess-abc",
              result="Done."),
    ]), on_session=seen.append)

    assert seen == ["sess-abc"], "once, on the first sighting"


def test_a_broken_session_hook_does_not_cost_an_application(
        spawnable, tmp_path, caplog) -> None:
    """Same rule as the progress hook: bookkeeping must not end a build."""
    def explode(_session: str) -> None:
        raise ZeroDivisionError("store is wedged")

    with caplog.at_level("ERROR", logger="watcher.build"):
        run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
            event(type="system", subtype="init", session_id="sess-abc"),
            event(type="result", is_error=False, result="Done."),
        ]), on_session=explode)

    assert run.ok is True
    assert "session hook failed" in caplog.text


def test_the_resume_reaches_the_command(spawnable, tmp_path) -> None:
    spawn(spawnable, tmp_path / "b.log", lambda: Process([
        event(type="result", is_error=False, result="Done."),
    ]), resume="sess-abc")

    assert spawnable.resume == "sess-abc"


def test_a_broken_progress_hook_does_not_cost_an_application(
        spawnable, tmp_path, caplog) -> None:
    """A defect in progress reporting must not end a build. This is the whole
    reason `feed` is wrapped rather than trusted."""
    def explode(_event: dict) -> None:
        raise ZeroDivisionError("progress bug")

    with caplog.at_level("ERROR", logger="watcher.build"):
        run = spawn(spawnable, tmp_path / "b.log", lambda: Process([
            event(type="result", is_error=False, result="Done."),
        ]), on_event=explode)

    assert run.ok is True
    assert "progress hook failed" in caplog.text


# ===========================================================================
# killing the tree
# ===========================================================================

def test_the_kill_reaches_the_children(monkeypatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(builder.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd))
    builder._kill_tree(1234)
    assert ran == [["taskkill", "/F", "/T", "/PID", "1234"]]


def test_a_kill_that_cannot_run_is_logged_and_survived(monkeypatch,
                                                       caplog) -> None:
    """On a machine without taskkill — CI, for one — this must not raise into
    the timeout handler it was called from."""
    def missing(cmd, **kw):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(builder.subprocess, "run", missing)
    with caplog.at_level("ERROR", logger="watcher.build"):
        builder._kill_tree(1234)
    assert "could not kill build process tree" in caplog.text


# ===========================================================================
# duplicates
# ===========================================================================

@pytest.fixture()
def existing(monkeypatch):
    """What `dedupe` says about prior applications for this company and role."""
    found: list[dedupe.ExistingApplication] = []
    monkeypatch.setattr(builder.dedupe, "collect_existing",
                        lambda *a, **kw: list(found))
    return found


def test_a_finished_application_blocks_the_rebuild(existing, monkeypatch,
                                                   identity, tmp_path) -> None:
    folder = tmp_path / "2026-08-01 - Data Scientist"
    folder.mkdir()
    for name in dedupe.required_pdfs():
        (folder / name).write_bytes(b"%PDF")
    existing.append(hit(folder=str(folder)))

    blocking, partial = check_duplicate("Acme", "Data Scientist", config())
    assert blocking is not None and partial == ""


def test_an_abandoned_folder_is_reported_and_then_built_over(
        existing, identity, tmp_path) -> None:
    """Otherwise one killed run locks that role out permanently."""
    folder = tmp_path / "2026-08-01 - Data Scientist"
    folder.mkdir()
    existing.append(hit(folder=str(folder)))

    blocking, partial = check_duplicate("Acme", "Data Scientist", config())
    assert blocking is None, "a folder with no documents is not an application"
    assert "looks incomplete" in partial
    assert "Cover Letter" in partial


def test_a_complete_application_behind_an_abandoned_one_still_blocks(
        existing, identity, tmp_path) -> None:
    """Ordering matters: the partial note must not shadow the real hit."""
    partial_folder = tmp_path / "2026-08-01 - Data Scientist"
    partial_folder.mkdir()
    existing.append(hit(folder=str(partial_folder), similarity=0.9))
    existing.append(hit(origin="tracker", similarity=0.85))

    blocking, note = check_duplicate("Acme", "Data Scientist", config())
    assert blocking is not None and blocking.origin == "tracker"
    assert "looks incomplete" in note, "the earlier partial is still reported"


def test_no_prior_application_blocks_nothing(existing) -> None:
    assert check_duplicate("Acme", "Data Scientist", config()) == (None, "")


# ===========================================================================
# what the run actually produced, read off disk
# ===========================================================================

def test_a_build_that_wrote_no_folder_is_a_failure_whatever_it_claimed(
        existing) -> None:
    """A build that reports success while writing nothing is a real failure
    mode, and the model is the last thing that should be trusted to notice."""
    outcome = locate_output("Acme", "Data Scientist", config())
    assert outcome.status == FAILED
    assert "no dated folder appeared" in outcome.detail


def test_a_folder_with_both_required_documents_is_done(existing, identity,
                                                       tmp_path) -> None:
    folder = tmp_path / "2026-08-05 - Data Scientist"
    folder.mkdir()
    for name in dedupe.required_pdfs():
        (folder / name).write_bytes(b"%PDF")
    (folder / "A Candidate - Interview Prep.pdf").write_bytes(b"%PDF")
    existing.append(hit(folder=str(folder)))

    outcome = locate_output("Acme", "Data Scientist", config())
    assert outcome.status == DONE
    assert outcome.detail == ""
    assert "A Candidate - Interview Prep.pdf" in outcome.documents


def test_a_folder_missing_a_required_document_is_incomplete(
        existing, identity, tmp_path) -> None:
    folder = tmp_path / "2026-08-05 - Data Scientist"
    folder.mkdir()
    (folder / dedupe.required_pdfs()[0]).write_bytes(b"%PDF")
    existing.append(hit(folder=str(folder)))

    outcome = locate_output("Acme", "Data Scientist", config())
    assert outcome.status == INCOMPLETE
    assert "missing" in outcome.detail and "Cover Letter" in outcome.detail


def test_a_tracker_row_is_noticed_even_without_a_folder(existing, identity,
                                                        tmp_path) -> None:
    """The tracker row is written at step 10, so its absence after a DONE run
    is the thing the user has to go and fix by hand."""
    folder = tmp_path / "2026-08-05 - Data Scientist"
    folder.mkdir()
    for name in dedupe.required_pdfs():
        (folder / name).write_bytes(b"%PDF")
    existing.append(hit(folder=str(folder)))
    existing.append(hit(origin="tracker"))

    assert locate_output("Acme", "Data Scientist", config()).tracker_row is True


def test_a_recorded_folder_that_is_no_longer_there_is_not_the_output(
        existing, identity, tmp_path) -> None:
    existing.append(hit(folder=str(tmp_path / "gone")))
    assert locate_output("Acme", "Data Scientist", config()).status == FAILED


# ===========================================================================
# cleanup
# ===========================================================================

@pytest.fixture()
def cleanup(monkeypatch):
    """`cleanup_application.py` without running it."""
    state = type("S", (), {"result": None, "cmds": []})()

    def run(cmd, **kwargs):
        state.cmds.append(cmd)
        if isinstance(state.result, Exception):
            raise state.result
        return state.result

    monkeypatch.setattr(builder.subprocess, "run", run)
    return state


def done(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_nothing_on_disk_means_nothing_to_clean(cleanup, tmp_path) -> None:
    """No folder means no tracker row either — step 10 only ever runs against a
    folder it has just filled."""
    assert clean_up("", "failed") == ""
    assert clean_up(str(tmp_path / "never-existed"), "failed") == ""
    assert cleanup.cmds == []


def test_a_cleanup_names_what_it_removed(cleanup, tmp_path) -> None:
    cleanup.result = done(stdout="Removed for: build failed\n"
                                 "  folder 2026/Acme/2026-08-05 - X\n"
                                 "  tracker row\n")
    line = clean_up(str(tmp_path), "the build stopped to ask")

    assert line == "cleaned up: folder 2026/Acme/2026-08-05 - X; tracker row"
    cmd = cleanup.cmds[0]
    assert "--folder" in cmd and cmd[cmd.index("--folder") + 1] == str(tmp_path)
    assert cmd[cmd.index("--reason") + 1] == "the build stopped to ask"
    assert cmd[1].endswith("cleanup_application.py"), (
        "the deletion lives in the script both entry points share")


def test_a_reasonless_cleanup_still_gives_the_log_something(cleanup,
                                                            tmp_path) -> None:
    cleanup.result = done(stdout="Removed for: build failed\n  folder X\n")
    clean_up(str(tmp_path), "")
    assert cleanup.cmds[0][cleanup.cmds[0].index("--reason") + 1] == "build failed"


def test_an_idempotent_cleanup_is_not_reported_as_work(cleanup,
                                                       tmp_path) -> None:
    cleanup.result = done(stdout="Nothing to clean up for that folder.")
    assert clean_up(str(tmp_path), "failed") == ""


def test_a_report_with_no_detail_lines_still_says_it_ran(cleanup,
                                                         tmp_path) -> None:
    cleanup.result = done(stdout="Removed for: build failed")
    assert clean_up(str(tmp_path), "failed") == "cleaned up"


def test_a_failed_cleanup_is_reported_rather_than_raised(cleanup, tmp_path,
                                                         caplog) -> None:
    """A cleanup that fails is worth reporting, not worth turning into a second
    failure on top of the first."""
    cleanup.result = done(2, stderr="refusing: path is outside the workspace")
    with caplog.at_level("ERROR", logger="watcher.build"):
        assert clean_up(str(tmp_path), "failed") == "cleanup failed — see watcher.log"
    assert "outside the workspace" in caplog.text


def test_a_cleanup_that_cannot_start_is_survivable(cleanup, tmp_path,
                                                   caplog) -> None:
    cleanup.result = subprocess.TimeoutExpired(["python"], 180)
    with caplog.at_level("ERROR", logger="watcher.build"):
        assert clean_up(str(tmp_path), "failed") == \
            "cleanup could not run — see watcher.log"
    assert "cleanup could not run" in caplog.text


# ===========================================================================
# how a build reads
# ===========================================================================

def test_a_folder_inside_the_workspace_is_shown_the_way_it_is_filed() -> None:
    folder = builder.REPO_ROOT / "2026" / "Acme" / "2026-08-05 - Data Scientist"
    assert relative(str(folder)).replace("\\", "/") == \
        "2026/Acme/2026-08-05 - Data Scientist"


def test_a_folder_somewhere_else_is_left_alone(tmp_path) -> None:
    """A path that is not under the workspace has no relative form, and
    guessing one would point the user at a folder that is not there."""
    outside = tmp_path / "elsewhere"
    assert relative(str(outside)) == str(outside)


def test_a_duplicate_says_when_and_where(identity) -> None:
    text = duplicate_message("<b>Acme</b> — DS",
                             hit(folder="/w/2026/Acme/2026-08-01 - DS"))
    assert "Duplicate" in text
    assert "2026-08-01" in text
    assert "Nothing was built" in text


def test_a_duplicate_known_only_from_the_tracker_still_reads(identity) -> None:
    text = duplicate_message("<b>Acme</b> — DS",
                             hit(origin="tracker", applied_on=None))
    assert "an unknown date" in text
    assert "logged in the tracker" in text


def test_the_document_list_drops_the_owners_prefix(identity) -> None:
    assert builder._doc_list(("A Candidate - CV.pdf",
                              "A Candidate - Cover Letter.pdf")) == \
        "CV · Cover Letter"


def test_a_stopped_build_quotes_the_question_and_fits_the_ceiling() -> None:
    """The whole point of this branch is the quoted text — it is a question,
    and half a question is no use."""
    outcome = Outcome(status=NEEDS_DECISION,
                      detail="Should I claim agent-framework experience? " * 200)
    text = result_message("<b>Acme</b> — DS", outcome, Path("b.log"))

    assert "Stopped to ask" in text
    assert len(text) <= builder.TELEGRAM_MAX_CHARS
    assert text.endswith("<i>log: b.log</i>")


def test_a_cut_message_never_ends_inside_an_html_entity() -> None:
    """Telegram rejects a message with a broken entity outright, so a run that
    stopped to ask would be reported as a send failure instead."""
    text = builder._fit("x" * (builder.TELEGRAM_MAX_CHARS - 40) + "&amp;",
                        Path("b.log"))
    assert len(text) <= builder.TELEGRAM_MAX_CHARS
    assert "&am" not in text.replace("&amp;", "")


def test_a_short_message_is_not_touched() -> None:
    assert builder._fit("hello", Path("b.log")) == \
        "hello\n<i>log: b.log</i>"


def test_a_failure_says_what_was_removed_rather_than_pointing_at_it() -> None:
    """Anything short of DONE has been cleaned up by now, so the folder path
    describes something that no longer exists."""
    outcome = Outcome(status=FAILED, folder="2026/Acme/2026-08-05 - DS",
                      detail="timed out after 45 min",
                      cleaned="cleaned up: folder; tracker row",
                      announced=True)
    text = result_message("<b>Acme</b> — DS", outcome, Path("b.log"))

    assert "Build failed" in text
    assert "retracts the ready message" in text
    assert "timed out after 45 min" in text
    assert "🧹" in text
    assert "log: b.log" in text


def test_a_failure_that_left_nothing_behind_says_so() -> None:
    text = result_message("<b>Acme</b> — DS",
                          Outcome(status=INCOMPLETE, detail="missing CV.pdf"),
                          Path("b.log"))
    assert "Built with issues" in text
    assert "Nothing was left behind" in text


# ===========================================================================
# the live checklist
# ===========================================================================

def reporter(notifier=None, *, footer: str = "", message_id: int = 77,
             **build) -> builder._ProgressReporter:
    return builder._ProgressReporter(
        notifier or Notifier(), config(**build),
        Job("p1", "", None, 1), "<b>Acme</b> — DS", message_id, footer=footer)


def test_a_retry_starts_the_checklist_over() -> None:
    """A retry re-runs the pipeline from step 00, so keeping the previous
    attempt's ticks would show a build further along than it is."""
    rep = reporter()
    rep.tracker.feed({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Agent",
         "input": {"subagent_type": "00-posting-archiver"}}]}})
    before = rep.tracker.counts()

    rep.begin_attempt(2, 2)

    assert rep.attempt == 2 and rep.attempts == 2
    assert rep._bump.is_set()
    assert rep.tracker.counts() != before or before[0] == 0
    assert "attempt 2/2" in rep.render()


def test_the_first_attempt_keeps_what_it_has() -> None:
    rep = reporter()
    started = rep.started
    rep.begin_attempt(1, 2)
    assert rep.started == started
    assert "attempt 1/2" in rep.render()


def test_a_single_attempt_does_not_advertise_itself() -> None:
    assert "attempt" not in reporter().render()


def test_the_note_about_a_partial_folder_rides_along() -> None:
    """It was in the opener this message replaced, and it is the reason the
    build is running over a folder that already exists."""
    text = reporter(footer="Previous attempt at X looks incomplete — rebuilding.")\
        .render()
    assert "looks incomplete" in text


def test_a_job_board_title_cannot_overflow_the_message() -> None:
    rep = builder._ProgressReporter(Notifier(), config(),
                                    Job("p1", "", None, 1), "A" * 6000, 77)
    assert len(rep.render()) == builder.TELEGRAM_MAX_CHARS


def test_the_checklist_is_only_sent_when_it_has_changed() -> None:
    """Re-sending identical text earns a "message is not modified" from
    Telegram and nothing else."""
    notifier = Notifier()
    rep = reporter(notifier)

    asyncio.run(rep._flush())
    asyncio.run(rep._flush())
    assert len(notifier.edits) == 1


def test_a_deleted_message_stops_being_edited() -> None:
    notifier = Notifier(edit_alive=False)
    rep = reporter(notifier)

    asyncio.run(rep._flush())
    assert rep.message_id is None
    asyncio.run(rep._flush("Complete"))
    assert len(notifier.edits) == 1


def test_an_edit_that_raises_does_not_reach_the_build(caplog) -> None:
    class Broken(Notifier):
        async def edit(self, message_id, text):
            raise RuntimeError("telegram fell over")

    rep = reporter(Broken())
    with caplog.at_level("ERROR", logger="watcher.build"):
        asyncio.run(rep._flush())
    assert "progress edit failed" in caplog.text


def test_the_refresh_task_redraws_on_a_transition_and_on_the_tick() -> None:
    """The tick keeps the running step's clock moving; a transition redraws
    immediately. Both paths through the same loop."""
    notifier = Notifier()

    async def scenario():
        rep = reporter(notifier, refresh_seconds=1, min_interval_seconds=0)
        rep.start()
        await asyncio.sleep(0)          # let the task reach its first wait
        rep.feed({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Agent",
             "input": {"subagent_type": "00-posting-archiver"}}]}})
        await asyncio.sleep(0.05)
        await rep.stop("Complete")

    asyncio.run(scenario())
    assert notifier.edits, "the checklist was drawn at least once"
    assert "Complete" in notifier.edits[-1][1]


def test_the_floor_between_edits_is_respected() -> None:
    """Parallel phases finish several steps within a second of each other, and
    each one on its own would be an API call."""
    notifier = Notifier()

    async def scenario():
        rep = reporter(notifier, refresh_seconds=1, min_interval_seconds=1)
        rep._last_edit = builder.time.monotonic()
        rep.start()
        await asyncio.sleep(0)
        rep._bump.set()
        await asyncio.sleep(0.05)
        rep._task.cancel()

    asyncio.run(scenario())
    assert notifier.edits == [], "the floor held the burst back"


def test_stopping_a_reporter_that_never_started_is_harmless() -> None:
    notifier = Notifier()
    asyncio.run(reporter(notifier).stop("Failed"))
    assert "Failed" in notifier.edits[-1][1]


# ===========================================================================
# the queue
# ===========================================================================

def test_the_config_follows_the_notifier_unless_it_is_pinned() -> None:
    """A build queued before a settings change and started after it runs under
    the new settings — the only reading of "on the fly" that is not a trap."""
    notifier = Notifier()
    assert Builder(notifier).config is notifier.config

    pinned = config(model="opus")
    assert Builder(notifier, pinned).config is pinned


def test_a_first_job_is_told_nothing_is_ahead_of_it(db) -> None:
    """The worker announces "Building …" the moment it picks a job up, so a
    separate "Queued …" for the first job is the same news twice."""
    posting(db)

    async def scenario():
        worker = Builder(Notifier())
        first = await worker.enqueue("p1", "")
        second = await worker.enqueue("p1", "add German")
        ahead = worker.pending
        await worker.stop()
        return first, second, ahead

    first, second, pending = asyncio.run(scenario())
    assert first == 0 and second == 1
    assert pending == 2

    with real_store.connect() as conn:
        rows = conn.execute("SELECT status FROM builds ORDER BY id").fetchall()
    assert len(rows) == 2, "the intent is recorded before the queue gets to it"
    assert {r["status"] for r in rows} <= {"queued", "running"}


def test_a_running_build_counts_as_ahead(db) -> None:
    """`qsize` alone reports an empty queue while a build is running, which
    would tell the second approval it was next when it is not."""
    posting(db)

    async def scenario():
        worker = Builder(Notifier())
        worker._busy = True
        ahead = await worker.enqueue("p1", "")
        await worker.stop()
        return ahead

    assert asyncio.run(scenario()) == 1


def test_the_worker_survives_a_build_that_throws(db, monkeypatch) -> None:
    """One bad build must not take the worker down with it, or every later
    approval silently queues behind a dead task."""
    posting(db)
    notifier = Notifier()

    async def scenario():
        worker = Builder(notifier)
        handled: list[str] = []

        async def handle(job):
            handled.append(job.posting_id)
            if len(handled) == 1:
                raise RuntimeError("something in the build")

        monkeypatch.setattr(worker, "_handle", handle)
        await worker.enqueue("p1", "")
        await worker.enqueue("p1", "")
        await asyncio.wait_for(worker._queue.join(), 2)
        await worker.stop()
        return handled

    assert asyncio.run(scenario()) == ["p1", "p1"], "the second job still ran"
    with real_store.connect() as conn:
        first = conn.execute("SELECT status, detail FROM builds "
                             "ORDER BY id LIMIT 1").fetchone()
    assert first["status"] == FAILED
    assert "see watcher.log" in first["detail"]
    assert notifier.sent[0][1] == "failed_build"


def test_a_worker_that_died_is_replaced_on_the_next_approval(db) -> None:
    """A queue with no consumer accepts approvals forever and builds none."""
    posting(db)

    async def scenario():
        worker = Builder(Notifier())
        worker._ensure_worker()
        dead = worker._worker
        dead.cancel()
        try:
            await dead
        except asyncio.CancelledError:
            pass
        await worker.enqueue("p1", "")
        replaced = worker._worker
        alive = not replaced.done()
        await worker.stop()
        return dead, replaced, alive

    dead, replaced, alive = asyncio.run(scenario())
    assert replaced is not dead and alive


def test_stopping_twice_is_harmless(db) -> None:
    async def scenario():
        worker = Builder(Notifier())
        worker._ensure_worker()
        await worker.stop()
        await worker.stop()

    asyncio.run(scenario())


def test_boot_reports_the_builds_the_last_process_died_during(
        db, monkeypatch) -> None:
    """Nothing survives a restart — the queue is in memory — so an approval
    killed mid-build is otherwise indistinguishable from one still running, and
    the user waits forever for a reply that is not coming."""
    posting(db, "p1", title="Data Scientist")
    posting(db, "p2", title="ML Engineer")
    with real_store.connect() as conn:
        real_store.queue_build(conn, "p1")
        build = real_store.queue_build(conn, "p2")
        real_store.mark_build_running(conn, build, "logs/builds/b.log")

    monkeypatch.setattr(builder, "ensure_dirs", lambda: None)
    notifier = Notifier()

    async def scenario():
        worker = Builder(notifier)
        await worker.start()
        await worker.stop()

    asyncio.run(scenario())

    assert len(notifier.notices) == 1
    notice = notifier.notices[0]
    assert "2 build(s) interrupted" in notice
    assert "Data Scientist" in notice and "ML Engineer" in notice
    assert "Reply" in notice, "the user needs to know it can be retried"

    with real_store.connect() as conn:
        rows = conn.execute("SELECT status FROM builds").fetchall()
    assert {r["status"] for r in rows} == {INTERRUPTED}


def test_a_clean_boot_says_nothing(db, monkeypatch) -> None:
    monkeypatch.setattr(builder, "ensure_dirs", lambda: None)
    notifier = Notifier()

    async def scenario():
        worker = Builder(notifier)
        await worker.start()
        await worker.stop()

    asyncio.run(scenario())
    assert notifier.notices == []


def test_recovery_is_not_reachable_from_an_ordinary_approval(db) -> None:
    """It declares every queued or running row abandoned, which is only true of
    rows a *previous* process left behind. Run inside a live process it would
    sweep the row enqueue itself just wrote."""
    posting(db)
    notifier = Notifier()

    async def scenario():
        worker = Builder(notifier)
        await worker.enqueue("p1", "")
        await worker.stop()

    asyncio.run(scenario())

    assert notifier.notices == []
    with real_store.connect() as conn:
        row = conn.execute("SELECT status FROM builds").fetchone()
    assert row["status"] in ("queued", "running")


def question(db_path, posting_id: str = "p1", *, kind: str = "stop_and_ask",
             message_id: int = 501, session_id: str = "sess-1") -> int:
    """An open question, recorded the way `_finish` records one."""
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, posting_id)
        return real_store.ask_question(
            conn, build, posting_id, kind, "-100123", message_id,
            thread_id=162, question="Sponsorship?", session_id=session_id)


def test_an_answer_is_queued_as_a_job_carrying_the_session_to_resume(
        db) -> None:
    """The whole point of the answer path: the job the worker picks up has to
    say both what the user said and which run said it, or the resume falls back
    to a fresh build that has never seen the question."""
    posting(db)
    qid = question(db)

    async def scenario():
        worker = Builder(Notifier())
        with real_store.connect() as conn:
            row = real_store.open_question(conn, qid)
        ahead = await worker.answer(row, "EU only, decline it")
        job = worker._queue.get_nowait()
        await worker.stop()
        return ahead, job

    ahead, job = asyncio.run(scenario())
    assert ahead == 0
    assert job.posting_id == "p1"
    assert job.answer == "EU only, decline it"
    assert job.resume_session == "sess-1"
    assert job.reply_message_id == 501, (
        "the continuation hangs off the message that asked")
    assert job.note == "", "an answer is not a build note"


def test_an_answer_to_a_question_with_no_session_still_queues(db) -> None:
    """A question written before the session id was known — an older row, or a
    run killed before its first event — must still be answerable. `_attempt`
    falls back to a fresh build with the answer as its note, which is worth
    more than refusing to accept the reply at all."""
    posting(db)
    qid = question(db, session_id="")

    async def scenario():
        worker = Builder(Notifier())
        with real_store.connect() as conn:
            row = real_store.open_question(conn, qid)
        await worker.answer(row, "go ahead")
        job = worker._queue.get_nowait()
        await worker.stop()
        return job

    assert asyncio.run(scenario()).resume_session == ""


def test_an_answer_carries_the_asking_sessions_log(db, tmp_path) -> None:
    """So the continuation's checklist can start from what the first half did.

    Resuming keeps the CLI session but not the tracker, and without this the
    steps taken before the question went out stay blank for the rest of the
    build — under a header that reads `0/13 steps` while the CV is being drafted.
    """
    posting(db)
    earlier = tmp_path / "first-half.log"
    earlier.write_text("{}\n", encoding="utf-8")
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
        real_store.mark_build_running(conn, build, str(earlier))
        qid = real_store.ask_question(
            conn, build, "p1", "stop_and_ask", "-100123", 501,
            thread_id=162, question="Sponsorship?", session_id="sess-1")

    async def scenario():
        worker = Builder(Notifier())
        with real_store.connect() as conn:
            row = real_store.open_question(conn, qid)
        await worker.answer(row, "yes")
        job = worker._queue.get_nowait()
        await worker.stop()
        return job

    assert asyncio.run(scenario()).resume_log == str(earlier)


def test_an_answer_to_a_build_that_never_logged_anything_still_queues(db) -> None:
    """A duplicate decline is a question with no run behind it at all, so there
    is no earlier checklist to inherit and nothing to look up. It answers like
    any other."""
    posting(db)
    qid = question(db, kind="duplicate")

    async def scenario():
        worker = Builder(Notifier())
        with real_store.connect() as conn:
            row = real_store.open_question(conn, qid)
        await worker.answer(row, "yes anyway")
        job = worker._queue.get_nowait()
        await worker.stop()
        return job

    assert asyncio.run(scenario()).resume_log == ""


def test_an_answer_records_its_own_build_row(db) -> None:
    """A resumed run is a build like any other — it spawns the CLI, writes to
    the folder and can be interrupted, so a restart has to find it in `builds`
    or it becomes the one kind of run nothing reports on."""
    posting(db)
    qid = question(db)

    async def scenario():
        worker = Builder(Notifier())
        with real_store.connect() as conn:
            row = real_store.open_question(conn, qid)
        await worker.answer(row, "yes")
        await worker.stop()

    asyncio.run(scenario())
    with real_store.connect() as conn:
        rows = conn.execute("SELECT posting_id FROM builds ORDER BY id"
                            ).fetchall()
    assert len(rows) == 2, "the question's own build, then the continuation"
    assert {r["posting_id"] for r in rows} == {"p1"}


def test_dropping_the_next_job_leaves_the_rest_in_order(db) -> None:
    """`/cancel` with nothing running aims at the queue head. Refilling an
    `asyncio.Queue` by hand is easy to get wrong in a way no user would report:
    the wrong job goes, or the survivors come back reversed and build in an
    order nobody asked for."""
    posting(db, "p1", title="Data Scientist")
    posting(db, "p2", title="ML Engineer")
    posting(db, "p3", title="AI Engineer")

    async def scenario():
        worker = Builder(Notifier())
        for pid in ("p1", "p2", "p3"):
            await worker.enqueue(pid, "")
        dropped = worker.drop_queued()
        rest = [worker._queue.get_nowait().posting_id for _ in range(2)]
        await worker.stop()
        return dropped, rest

    dropped, rest = asyncio.run(scenario())
    assert dropped is not None and dropped.posting_id == "p1"
    assert rest == ["p2", "p3"]


def test_a_dropped_job_is_named_rather_than_taken_off_the_front(db) -> None:
    posting(db, "p1")
    posting(db, "p2", title="ML Engineer")

    async def scenario():
        worker = Builder(Notifier())
        await worker.enqueue("p1", "")
        await worker.enqueue("p2", "")
        dropped = worker.drop_queued("p2")
        rest = [worker._queue.get_nowait().posting_id]
        await worker.stop()
        return dropped, rest

    dropped, rest = asyncio.run(scenario())
    assert dropped is not None and dropped.posting_id == "p2"
    assert rest == ["p1"]


def test_a_dropped_job_is_recorded_as_cancelled(db) -> None:
    """Its row was written at enqueue time and would otherwise sit 'queued'
    forever — which is exactly what boot recovery reads as "interrupted by a
    restart", so the next start would announce a build the user cancelled."""
    posting(db)

    async def scenario():
        worker = Builder(Notifier())
        await worker.enqueue("p1", "")
        worker.drop_queued()
        await worker.stop()

    asyncio.run(scenario())
    with real_store.connect() as conn:
        row = conn.execute("SELECT status, detail FROM builds").fetchone()
    assert row["status"] == builder.CANCELLED
    assert "queue" in row["detail"]


def test_dropping_from_an_empty_queue_says_so(db) -> None:
    """`/cancel` calls this whenever nothing is running, including when nothing
    is waiting either. `None` is how it learns to say that."""
    async def scenario():
        worker = Builder(Notifier())
        dropped = worker.drop_queued()
        await worker.stop()
        return dropped

    assert asyncio.run(scenario()) is None


def test_dropping_a_job_that_is_not_queued_leaves_the_queue_alone(db) -> None:
    posting(db)

    async def scenario():
        worker = Builder(Notifier())
        await worker.enqueue("p1", "")
        dropped = worker.drop_queued("nowhere")
        pending = worker.pending
        await worker.stop()
        return dropped, pending

    dropped, pending = asyncio.run(scenario())
    assert dropped is None
    assert pending == 1, "an unmatched id must not silently empty the queue"


# ===========================================================================
# _handle: the paths before the build starts
# ===========================================================================

def handle(worker: Builder, job: Job | None = None) -> None:
    """Run the real `_handle` — the one `inert_worker` replaces everywhere else.

    Called directly rather than through the queue, so there is no worker task to
    outlive the loop, and every test below stops the run before it can spawn.
    """
    asyncio.run(_real_handle(worker, job or Job("p1", "", 42, 1)))


def test_a_posting_that_vanished_is_reported_rather_than_built(db) -> None:
    """The queue holds ids, not rows, so a job outlives the posting it names."""
    notifier = Notifier()
    posting(db)
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")

    handle(Builder(notifier), Job("gone", "", 42, build))

    text, topic, _ = notifier.sent[0]
    assert "no longer in the database" in text
    assert topic == "failed_build"
    with real_store.connect() as conn:
        row = conn.execute("SELECT status, detail FROM builds").fetchone()
    assert row["status"] == FAILED and "vanished" in row["detail"]


def test_a_duplicate_is_declined_before_anything_is_spawned(
        db, monkeypatch) -> None:
    posting(db)
    monkeypatch.setattr(
        builder, "check_duplicate",
        lambda c, t, cfg: (hit(folder="/w/2026/Acme/2026-08-01 - DS"), ""))
    monkeypatch.setattr(builder, "_spawn", _never_spawned)

    notifier = Notifier()
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    handle(Builder(notifier), Job("p1", "", 42, build))

    assert "Duplicate" in notifier.sent[0][0]
    with real_store.connect() as conn:
        row = conn.execute("SELECT status FROM builds").fetchone()
    assert row["status"] == DUPLICATE


async def _never_spawned(*args, **kwargs):
    raise AssertionError("a duplicate must not spawn a build")


def test_the_decline_is_logged_and_sent_where_the_approval_was(
        db, monkeypatch, caplog) -> None:
    """Both halves of why a declined build read as one that never started.

    Nothing reached `watcher.log` at all, so the log said the job had vanished
    between the queue and the worker; and the message went to Processing while
    the `yes` that caused it was in New postings, so the only trace was in a
    topic nobody reads for answers.
    """
    posting(db)
    monkeypatch.setattr(
        builder, "check_duplicate",
        lambda c, t, cfg: (hit(folder="2026/Acme/2026-08-01 - DS"), ""))
    monkeypatch.setattr(builder, "_spawn", _never_spawned)

    notifier = Notifier()
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    with caplog.at_level("INFO", logger="watcher.build"):
        handle(Builder(notifier), Job("p1", "", 42, build))

    assert "declined as a duplicate" in caplog.text
    text, topic, _ = notifier.sent[0]
    assert topic == "targeted_build"
    assert "yes anyway" in text, "a decline the user can overrule"


def test_a_decline_is_recorded_as_a_question(db, monkeypatch) -> None:
    """So `/pending` lists it and a reply to it resolves — the same machinery a
    stop-and-ask uses, because both are the watcher waiting on a decision."""
    posting(db)
    monkeypatch.setattr(
        builder, "check_duplicate",
        lambda c, t, cfg: (hit(folder="2026/Acme/2026-08-01 - DS"), ""))
    monkeypatch.setattr(builder, "_spawn", _never_spawned)

    notifier = Notifier()
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    handle(Builder(notifier), Job("p1", "", 42, build))

    with real_store.connect() as conn:
        row = conn.execute("SELECT * FROM questions").fetchone()
    assert row["kind"] == real_store.QUESTION_DUPLICATE
    assert row["message_id"] == notifier.next_id
    assert row["answered_at"] is None
    # The matched folder is named in the text and nowhere else. `folder` means
    # "what this run left behind, erase it if the run is abandoned", and this
    # run left nothing — that folder holds a finished application, so filling
    # the column in would make `no` delete it.
    assert not row["folder"]
    assert "2026/Acme/2026-08-01 - DS" in row["question"]


def test_an_override_skips_the_duplicate_check_entirely(db, monkeypatch) -> None:
    """`yes anyway`. The check is not re-run and softened — it is not run.

    Softening it would need a second threshold nobody set; the user has already
    looked at the folder this would duplicate and said build it anyway.
    """
    posting(db)

    def refuse(company, title, cfg):
        raise AssertionError("the override must not consult the matcher")

    monkeypatch.setattr(builder, "check_duplicate", refuse)
    monkeypatch.setattr(builder, "locate_output",
                        lambda c, t, cfg: Outcome(status=FAILED))
    monkeypatch.setattr(builder, "clean_up", lambda folder, reason: "")

    async def attempt(self, job, company, title, url, label, reporter=None):
        return builder.RunResult(ok=True, log_file=Path("b.log"))

    monkeypatch.setattr(Builder, "_attempt", attempt)

    notifier = Notifier()
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    handle(Builder(notifier),
           Job("p1", "", 42, build, override_duplicate=True))

    assert "🛠 Building" in notifier.sent[0][0]


def test_an_answer_says_it_is_continuing_rather_than_starting(
        db, monkeypatch) -> None:
    """The run it belongs to already has a folder. "Building" would read as a
    second application for the same job."""
    posting(db)

    def refuse(company, title, cfg):
        raise AssertionError("an answer has already been through this check")

    monkeypatch.setattr(builder, "check_duplicate", refuse)
    monkeypatch.setattr(builder, "locate_output",
                        lambda c, t, cfg: Outcome(status=FAILED))
    monkeypatch.setattr(builder, "clean_up", lambda folder, reason: "")

    async def attempt(self, job, company, title, url, label, reporter=None):
        return builder.RunResult(ok=True, log_file=Path("b.log"))

    monkeypatch.setattr(Builder, "_attempt", attempt)

    notifier = Notifier()
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    handle(Builder(notifier),
           Job("p1", "", 42, build, answer="Option 2.",
               resume_session="sess-abc"))

    assert "💬 Continuing" in notifier.sent[0][0]


def test_every_build_message_becomes_a_valid_reply_target(db) -> None:
    """The literal symptom the user hit: the run ended by asking them to reply,
    and the reply came back "that message isn't one of mine" — because `_reply`
    never recorded what it sent, and `_resolve` reads `notifications`."""
    posting(db)
    notifier = Notifier()
    worker = Builder(notifier)
    job = Job("p1", "", 42, 1)

    message_id = asyncio.run(worker._reply(job, "⏸ Stopped to ask"))

    with real_store.connect() as conn:
        found = real_store.postings_for_message(conn, notifier.chat_id,
                                                message_id)
    assert found == ["p1"]


def test_a_build_message_is_not_mistaken_for_having_pinged_the_posting(
        db) -> None:
    """`unnotified_in_band` reads the same table. Recording build reports there
    with a ping's kind would silence the ping for a posting never pinged."""
    posting(db)
    notifier = Notifier()
    worker = Builder(notifier)

    asyncio.run(worker._reply(Job("p1", "", 42, 1), "🛠 Building…"))

    with real_store.connect() as conn:
        row = conn.execute(
            "SELECT kind FROM notifications WHERE posting_id = 'p1'").fetchone()
    assert row["kind"] == "build"
    assert "build" not in real_store.PING_KINDS


def test_the_opener_carries_the_partial_note_and_becomes_the_checklist(
        db, monkeypatch, identity) -> None:
    """The opener is stored so a restart can find it: the queue lives in
    memory, so a build interrupted by one would otherwise leave its checklist
    frozen mid-run and reading as still in progress for good."""
    posting(db)
    monkeypatch.setattr(builder, "check_duplicate",
                        lambda c, t, cfg: (None, "Previous attempt looks incomplete."))
    monkeypatch.setattr(builder, "locate_output",
                        lambda c, t, cfg: Outcome(status=FAILED))
    monkeypatch.setattr(builder, "clean_up", lambda folder, reason: "")

    async def attempt(self, job, company, title, url, label, reporter=None):
        return builder.RunResult(ok=True, log_file=Path("b.log"))

    monkeypatch.setattr(Builder, "_attempt", attempt)

    notifier = Notifier()
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    handle(Builder(notifier), Job("p1", "", 42, build))

    opener = notifier.sent[0][0]
    assert "🛠 Building" in opener
    assert "Previous attempt looks incomplete." in opener

    with real_store.connect() as conn:
        row = conn.execute(
            "SELECT progress_message_id FROM builds").fetchone()
    assert row["progress_message_id"] == 501
    assert notifier.edits, "the opener was edited rather than replaced"


def test_no_checklist_is_started_when_progress_is_switched_off(
        db, monkeypatch, identity) -> None:
    """Off, the message says `🛠 Building …` once and nothing more until the run
    ends — which is exactly how it behaved before this existed."""
    posting(db)
    monkeypatch.setattr(builder, "check_duplicate", lambda c, t, cfg: (None, ""))
    monkeypatch.setattr(builder, "locate_output",
                        lambda c, t, cfg: Outcome(status=FAILED))
    monkeypatch.setattr(builder, "clean_up", lambda folder, reason: "")

    async def attempt(self, job, company, title, url, label, reporter=None):
        assert reporter is None
        return builder.RunResult(ok=True, log_file=Path("b.log"))

    monkeypatch.setattr(Builder, "_attempt", attempt)

    notifier = Notifier()
    notifier.config = config(progress_updates=False)
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    handle(Builder(notifier), Job("p1", "", 42, build))

    assert notifier.edits == []


# ===========================================================================
# _attempt: a crash on the way
# ===========================================================================

def test_a_crash_inside_the_spawn_becomes_an_ordinary_failure(
        db, monkeypatch, caplog) -> None:
    """Routed into the failure path rather than up to the worker's catch-all,
    which knows no company and so cannot clean up."""
    posting(db)

    async def explode(prompt, cfg, log_file, **kwargs):
        raise OSError("the pipe closed")

    monkeypatch.setattr(builder, "_spawn", explode)
    monkeypatch.setattr(builder, "log_path_for",
                        lambda c, t, attempt=1, suffix="": Path(f"b{attempt}.log"))

    worker = Builder(Notifier())
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")

    with caplog.at_level("ERROR", logger="watcher.build"):
        run = asyncio.run(worker._attempt(
            Job("p1", "", None, build), "Acme", "Data Scientist",
            "https://x.test/job", "<b>Acme</b> — DS"))

    assert run.ok is False
    assert run.detail == "the build crashed, see watcher.log"
    assert run.closing == "" and run.log_file == Path("b1.log")
    assert "build crashed" in caplog.text


def test_each_attempt_is_told_which_one_it_is(db, monkeypatch) -> None:
    posting(db)
    seen: list[tuple[int, int]] = []

    monkeypatch.setattr(builder, "_spawn",
                        lambda *a, **kw: _ok_spawn())
    monkeypatch.setattr(builder, "log_path_for",
                        lambda c, t, attempt=1, suffix="": Path("b.log"))

    class Rep:
        def begin_attempt(self, attempt, attempts):
            seen.append((attempt, attempts))
        feed = staticmethod(lambda event: None)

    worker = Builder(Notifier(), config(retries=2))
    with real_store.connect() as conn:
        build = real_store.queue_build(conn, "p1")
    asyncio.run(worker._attempt(Job("p1", "", None, build), "Acme", "DS",
                                "https://x.test/job", "L", Rep()))
    assert seen == [(1, 3)]


async def _ok_spawn():
    return builder.RunResult(ok=True)


# ===========================================================================
# the announcer's patience
# ===========================================================================

def announce(monkeypatch, outcomes, *, folder: Path | None = None):
    """Run the announcer over a scripted sequence of disk readings."""
    steps = iter(outcomes)
    recorder = type("R", (), {"config": config(), "sent": []})()

    async def reply(job, text, topic="processing_build"):
        recorder.sent.append((text, topic))

    recorder._reply = reply

    def look(company, title, cfg):
        try:
            return next(steps)
        except StopIteration:
            raise asyncio.CancelledError from None

    monkeypatch.setattr(builder, "locate_output", look)
    monkeypatch.setattr(builder, "ready_message", lambda label, outcome: "READY")

    async def no_wait(_seconds):
        pass

    monkeypatch.setattr(builder.asyncio, "sleep", no_wait)

    async def scenario():
        try:
            return await Builder._announce_ready(
                recorder, None, "Acme", "DS", "<b>Acme</b>")
        except asyncio.CancelledError:
            return False

    return asyncio.run(scenario()), recorder


def test_the_announcer_keeps_waiting_while_the_folder_is_not_done(
        monkeypatch) -> None:
    fired, recorder = announce(monkeypatch, [
        Outcome(status=FAILED),
        Outcome(status=INCOMPLETE, folder="somewhere"),
        Outcome(status=DONE, folder=""),
    ])
    assert fired is False and recorder.sent == []


def test_a_pdf_that_is_not_readable_yet_is_not_ready(monkeypatch,
                                                     tmp_path) -> None:
    """The folder is there and the run says DONE, but a file the render is
    still opening reads as an OSError, not as zero bytes."""
    folder = tmp_path / "2026-08-05 - DS"
    folder.mkdir()
    monkeypatch.setattr(builder.dedupe, "required_pdfs",
                        lambda: ("A - CV.pdf", "A - Cover Letter.pdf"))

    fired, recorder = announce(monkeypatch, [
        Outcome(status=DONE, folder=str(folder)),  # neither file exists yet
        Outcome(status=DONE, folder=str(folder)),
    ])
    assert fired is False and recorder.sent == []


def test_an_empty_pdf_is_not_ready_either(monkeypatch, tmp_path) -> None:
    folder = tmp_path / "2026-08-05 - DS"
    folder.mkdir()
    (folder / "A - CV.pdf").write_bytes(b"%PDF")
    (folder / "A - Cover Letter.pdf").write_bytes(b"")
    monkeypatch.setattr(builder.dedupe, "required_pdfs",
                        lambda: ("A - CV.pdf", "A - Cover Letter.pdf"))

    fired, recorder = announce(monkeypatch, [
        Outcome(status=DONE, folder=str(folder)),
        Outcome(status=DONE, folder=str(folder)),
    ])
    assert fired is False and recorder.sent == []


# ===========================================================================
# the command line
# ===========================================================================

@pytest.fixture()
def cli(monkeypatch, guarded, tmp_path):
    monkeypatch.setattr(builder, "force_utf8", lambda: None)
    monkeypatch.setattr(builder, "load_config", config)
    monkeypatch.setattr(builder, "BUILD_LOG_DIR", tmp_path)
    monkeypatch.setattr(builder, "check_duplicate", lambda c, t, cfg: (None, ""))
    return guarded


def test_the_cli_needs_something_to_build(cli, capsys) -> None:
    assert builder.main([]) == 2
    assert "give either --posting" in capsys.readouterr().out


def test_the_cli_refuses_to_start_a_real_build(cli, capsys) -> None:
    """Real builds are started by replying to a Telegram notification. A second
    way in is a second way to end up with two pipelines writing LaTeX at once."""
    assert builder.main(["--company", "Acme", "--title", "DS"]) == 2
    assert "Only --dry-run is supported" in capsys.readouterr().out


def test_a_dry_run_shows_the_command_the_prompt_and_the_log(cli,
                                                            capsys) -> None:
    assert builder.main(["--company", "Acme", "--title", "AI Engineer",
                         "--url", "https://x.test/job", "--note", "add German",
                         "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "company : Acme" in out
    assert "note    : add German" in out
    assert "would run:" in out
    assert "--permission-mode" in out
    assert "https://x.test/job\\nadd German" in out
    assert "-acme-ai-engineer.log" in out


def test_a_dry_run_with_no_note_says_so(cli, capsys) -> None:
    builder.main(["--company", "Acme", "--title", "DS", "--dry-run"])
    assert "note    : (none)" in capsys.readouterr().out


def test_a_dry_run_reads_the_posting_from_the_database(cli, db, capsys) -> None:
    posting(db)
    assert builder.main(["--posting", "p1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "company : Acme" in out
    assert "https://acme.test/p1" in out


def test_an_unknown_posting_id_is_an_error_not_a_build(cli, db, capsys) -> None:
    assert builder.main(["--posting", "nope", "--dry-run"]) == 1
    assert "no posting with id nope" in capsys.readouterr().out


def test_a_dry_run_reports_a_duplicate_instead_of_a_command(cli, monkeypatch,
                                                            capsys) -> None:
    monkeypatch.setattr(builder, "check_duplicate",
                        lambda c, t, cfg: (hit(folder="/w/2026/Acme/x"), ""))
    assert builder.main(["--company", "Acme", "--title", "DS", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DUPLICATE" in out
    assert "No build would run." in out
    assert "would run:" not in out


def test_a_dry_run_repeats_the_partial_note(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(builder, "check_duplicate",
                        lambda c, t, cfg: (None, "looks incomplete — rebuilding"))
    builder.main(["--company", "Acme", "--title", "DS", "--dry-run"])
    assert "looks incomplete" in capsys.readouterr().out


def test_a_dry_run_says_when_the_build_could_not_start_at_all(
        cli, capsys) -> None:
    """The point of the dry run: find out here rather than in a Telegram
    thread forty minutes later."""
    cli.unlink()
    assert builder.main(["--company", "Acme", "--title", "DS", "--dry-run"]) == 1
    assert "cannot build:" in capsys.readouterr().out
