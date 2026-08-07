"""A dropped response is retried, and never erases a finished application.

One build lost both of these at once. The CLI reported `API Error: Server error
mid-response` during Interview Prep, 5 minutes after the CV and cover letter had
passed QA and been announced as ready. Two things then went wrong in sequence:

  1. `is_transient` did not recognise the message — the pattern asked for
     "internal server error" and the CLI does not say "internal" — so the retry
     the build had been granted never ran.
  2. Cleanup erased the folder anyway, because the code asked the exit code
     whether the run succeeded instead of asking the disk whether the documents
     were there. A CV and a cover letter that were finished, checked, and
     announced were deleted for a study aid that had not finished.

The two properties worth pinning: an upstream drop is retryable, and the disk
outranks the exit code when deciding what to delete.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from pathlib import Path

import pytest

from watcher import builder
from watcher.builder import DONE, FAILED, INCOMPLETE, Job, Outcome, is_transient


# The exact detail recorded for that build, copied from the log.
DROPPED = ("API Error: Server error mid-response. The response above may be "
           "incomplete. (exit 1)")


# --------------------------------------------------------------------------
# is_transient
# --------------------------------------------------------------------------

def test_the_failure_that_prompted_this_is_transient() -> None:
    """The whole first half of the bug in one assertion."""
    assert is_transient(DROPPED)


@pytest.mark.parametrize("detail", [
    "API Error: Server error mid-response. The response above may be incomplete.",
    "API Error: 503 All accounts are temporarily unavailable",
    "API Error: 429 rate_limit_error",
    "Internal server error",
    "502 Bad Gateway",
    "gateway time-out",
    "upstream connect error",
    "connection reset by peer",
    "ECONNRESET",
    "fetch failed",
    "Overloaded",
])
def test_upstream_symptoms_earn_a_retry(detail: str) -> None:
    assert is_transient(detail)


@pytest.mark.parametrize("detail", [
    "",
    "timed out after 45 min",
    "the build crashed, see watcher.log",
    "the build reported success but no dated folder appeared",
    "missing Someone - Cover Letter.pdf",
    "401 Unauthorized",
    "403 Forbidden",
])
def test_permanent_failures_are_still_not_retried(detail: str) -> None:
    """Widening the pattern must not turn it into a catch-all.

    Retrying a timeout, a crash, or a run that wrote the wrong files spends
    another 45 minutes arriving at the same answer. 401/403 do not heal.
    """
    assert not is_transient(detail)


# --------------------------------------------------------------------------
# the salvage decision in _handle
# --------------------------------------------------------------------------

class _Config:
    duplicate_title_ratio = 0.8
    duplicate_lookback_days = 365
    build_retries = 0
    build_timeout_minutes = 45
    build_progress_updates = False

    def topic_for(self, kind: str) -> int | None:
        return {"targeted_build": 162, "failed_build": 168,
                "completed_build": 165}.get(kind)


class _Store:
    """Stands in for `watcher.store`, recording what the build was filed as."""

    QUESTION_STOP_AND_ASK = "stop_and_ask"

    def __init__(self) -> None:
        self.finished: list[tuple[str, str, str]] = []
        self.asked: list[dict] = []
        self.sessions: list[tuple[int, str]] = []

    @contextlib.contextmanager
    def connect(self, path=None):
        yield None

    def ask_question(self, conn, build_id, posting_id, kind, chat_id,
                     message_id, thread_id=None, question="", folder="",
                     session_id=""):
        self.asked.append({"kind": kind, "message_id": message_id,
                           "session_id": session_id, "folder": folder})
        return len(self.asked)

    def get_posting(self, conn, posting_id):
        return {"company": "Acme", "title": "Data Scientist",
                "url": "https://example.com/job"}

    def finish_build(self, conn, build_id, status, folder="", detail=""):
        self.finished.append((status, folder, detail))

    def mark_build_running(self, conn, build_id, log_file):
        pass

    def set_build_session(self, conn, build_id, session_id):
        self.sessions.append((build_id, session_id))


class _Builder:
    """Enough of a Builder for the real `_handle` to run against."""

    # `_handle` hands the settling half of a run — salvage, cleanup, the result
    # message — to `_finish`, which is the half these tests are about. Taken from
    # the real class rather than stubbed, or there would be nothing left under
    # test.
    _finish = builder.Builder._finish

    def __init__(self, ok: bool, detail: str, announce: bool,
                 closing: str = "") -> None:
        self.config = _Config()
        self.notifier = type("N", (), {"chat_id": "-100999"})()
        self.sent: list[str] = []
        self.topics: list[str] = []
        self._ok, self._detail, self._announce = ok, detail, announce
        self._closing = closing

    async def _reply(self, job, text: str,
                     topic: str = "processing_build") -> int:
        self.sent.append(text)
        self.topics.append(topic)
        return 700 + len(self.sent)

    async def _announce_ready(self, job, company, title, label) -> bool:
        if self._announce:
            return True
        await asyncio.sleep(3600)  # still polling when the build ends
        return True

    async def _attempt(self, job, company, title, url, label, reporter=None):
        # Yield twice so the announcer task actually gets to run, the way it
        # would during a real build.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return builder.RunResult(ok=self._ok, detail=self._detail,
                                 closing=self._closing,
                                 log_file=Path("build.log"))


def _run(monkeypatch, *, ok: bool, detail: str, disk: Outcome,
         announce: bool = False, closing: str = ""):
    """Drive the real `_handle` with a fixed build result and a fixed disk."""
    cleaned: list[tuple[str, str]] = []

    def fake_clean_up(folder: str, reason: str) -> str:
        cleaned.append((folder, reason))
        return f"removed {folder}"

    store = _Store()
    monkeypatch.setattr(builder, "store", store)
    monkeypatch.setattr(builder, "check_duplicate", lambda c, t, cfg: (None, ""))
    monkeypatch.setattr(builder, "locate_output",
                        lambda c, t, cfg: dataclasses.replace(disk))
    monkeypatch.setattr(builder, "clean_up", fake_clean_up)

    instance = _Builder(ok, detail, announce, closing)
    job = Job(posting_id="p1", note="", reply_message_id=None, build_id=1)
    asyncio.run(builder.Builder._handle(instance, job))
    return instance, store, cleaned


def _complete() -> Outcome:
    return Outcome(status=DONE,
                   folder="2026/Acme/2026-08-05 - Data Scientist",
                   documents=("Someone - CV.pdf", "Someone - Cover Letter.pdf"))


def test_a_late_failure_keeps_a_complete_application(monkeypatch) -> None:
    """The regression: every required PDF is on disk, so nothing is erased."""
    _, store, cleaned = _run(monkeypatch, ok=False, detail=DROPPED,
                             disk=_complete())

    assert cleaned == [], "the documents were all there — nothing to erase"
    status, folder, record = store.finished[-1]
    assert status == DONE
    assert folder == "2026/Acme/2026-08-05 - Data Scientist"
    assert "Server error mid-response" in record, (
        "the run's failure still belongs in the record")


def test_the_late_failure_is_reported_rather_than_hidden(monkeypatch) -> None:
    """Keeping the application is not the same as pretending the run was clean.

    Interview Prep and the tracker row are the two things a late failure
    usually costs, and both are things the user has to go and do by hand.
    """
    instance, _, _ = _run(monkeypatch, ok=False, detail=DROPPED,
                          disk=_complete())
    assert "did not finish cleanly" in instance.sent[-1]
    assert "Server error mid-response" in instance.sent[-1]


def test_a_salvaged_run_does_not_retract_the_ready_message(monkeypatch) -> None:
    """The ready message was true. Retracting it would be the lie."""
    instance, store, cleaned = _run(monkeypatch, ok=False, detail=DROPPED,
                                    disk=_complete(), announce=True)
    assert cleaned == []
    assert store.finished[-1][0] == DONE
    assert "retracts" not in instance.sent[-1].lower()
    assert "Build failed" not in instance.sent[-1]


def test_a_half_built_application_is_still_erased(monkeypatch) -> None:
    """The salvage must not become an excuse to keep debris.

    A folder without its required PDFs reads as "already applied" to the
    duplicate check and locks the role out of ever being retried.
    """
    disk = Outcome(status=INCOMPLETE,
                   folder="2026/Acme/2026-08-05 - Data Scientist",
                   detail="missing Someone - Cover Letter.pdf",
                   documents=("Someone - CV.pdf",))
    _, store, cleaned = _run(monkeypatch, ok=False, detail=DROPPED, disk=disk)

    assert cleaned, "a folder missing a required document must not survive"
    assert store.finished[-1][0] == FAILED


def test_a_failure_that_wrote_nothing_is_unchanged(monkeypatch) -> None:
    disk = Outcome(status=FAILED,
                   detail="the build reported success but no dated folder appeared")
    _, store, cleaned = _run(monkeypatch, ok=False, detail=DROPPED, disk=disk)

    assert len(cleaned) == 1
    assert store.finished[-1][0] == FAILED


def test_a_clean_run_gains_no_caveat(monkeypatch) -> None:
    """The salvage path must not quietly become the only path."""
    disk = dataclasses.replace(_complete(), tracker_row=True)
    instance, store, cleaned = _run(monkeypatch, ok=True, detail="", disk=disk)

    assert cleaned == []
    assert store.finished[-1][0] == DONE
    assert "did not finish cleanly" not in instance.sent[-1]


# --------------------------------------------------------------------------
# how a salvaged run reads
# --------------------------------------------------------------------------

def test_the_message_names_what_the_late_failure_cost() -> None:
    outcome = Outcome(status=DONE,
                      folder="2026/Acme/2026-08-05 - Data Scientist",
                      documents=("Someone - CV.pdf",),
                      tracker_row=False, announced=True, salvaged=DROPPED)
    text = builder.result_message("<b>Acme</b>", outcome, Path("build.log"))

    assert "did not finish cleanly" in text
    # The two things the user now has to check are already on the line above.
    assert "no tracker row" in text
    assert "CV" in text


# --------------------------------------------------------------------------
# what a retry is allowed to do
# --------------------------------------------------------------------------
#
# The retry saved the run it was written for and then damaged the next one. A
# build announced its application as ready at 18:10, lost the connection during
# Interview Prep at 18:16, and was restarted from step 00 — twenty-six minutes
# to rebuild two documents that were already on disk and already announced. It
# also renamed the folder on the way (`AWS AI & Data Engineer` became `AWS AI
# Data Engineer`), leaving the abandoned attempt behind as a second application
# for the same role.


class _RetryConfig(_Config):
    build_retries = 1
    build_retry_delay_seconds = 0


class _Retrier:
    """Enough of a Builder for the real `_attempt` to run against."""

    _attempt = builder.Builder._attempt

    def __init__(self) -> None:
        self.config = _RetryConfig()
        self.sent: list[str] = []

    async def _reply(self, job, text: str,
                     topic: str = "processing_build") -> None:
        self.sent.append(text)


def _spawned(ok: bool, detail: str = "") -> builder.RunResult:
    return builder.RunResult(ok=ok, detail=detail)


def _drive(monkeypatch, *, spawns, disk: Outcome, job: Job | None = None):
    """Run the real `_attempt` over a fixed sequence of `_spawn` results."""
    trace: list[str] = []
    results = list(spawns)

    async def fake_spawn(prompt, config, log_file, on_event=None, resume="",
                         on_session=None):
        trace.append(f"spawn:{resume}" if resume else "spawn")
        return results.pop(0)

    def fake_clean_up(folder: str, reason: str) -> str:
        trace.append(f"clean:{folder}")
        return f"removed {folder}"

    monkeypatch.setattr(builder, "store", _Store())
    monkeypatch.setattr(builder, "_spawn", fake_spawn)
    monkeypatch.setattr(builder, "clean_up", fake_clean_up)
    monkeypatch.setattr(builder, "locate_output",
                        lambda c, t, cfg: dataclasses.replace(disk))
    monkeypatch.setattr(builder, "log_path_for",
                        lambda c, t, attempt=1, suffix="": Path(f"build-{attempt}.log"))

    instance = _Retrier()
    job = job or Job(posting_id="p1", note="", reply_message_id=None, build_id=1)
    outcome = asyncio.run(builder.Builder._attempt(
        instance, job, "Acme", "Data Scientist", "https://example.com/job",
        "<b>Acme</b> — Data Scientist"))
    return instance, outcome, trace


def test_a_drop_after_the_documents_landed_is_not_retried(monkeypatch) -> None:
    """The regression: a finished application is not rebuilt from step 00."""
    _, outcome, trace = _drive(
        monkeypatch, spawns=[_spawned(False, DROPPED)], disk=_complete())

    assert trace == ["spawn"], "the documents were already there"
    # Returned as the failure it was — `_finish` is what turns that into a
    # salvage, and it makes the same disk check to do it.
    assert outcome.ok is False and outcome.detail == DROPPED


def test_a_drop_before_the_documents_is_still_retried(monkeypatch) -> None:
    """Widening the check must not disable the retry it lives inside."""
    disk = Outcome(status=INCOMPLETE,
                   folder="2026/Acme/2026-08-05 - Data Scientist",
                   detail="missing Someone - Cover Letter.pdf",
                   documents=("Someone - CV.pdf",))
    _, outcome, trace = _drive(
        monkeypatch,
        spawns=[_spawned(False, DROPPED), _spawned(True)],
        disk=disk)

    assert trace.count("spawn") == 2
    assert outcome.ok is True


def test_the_partial_folder_is_erased_before_the_retry(monkeypatch) -> None:
    """The orphan: two folders for one application, under different names."""
    folder = "2026/Acme/2026-08-05 - Data Scientist"
    disk = Outcome(status=INCOMPLETE, folder=folder,
                   detail="missing Someone - Cover Letter.pdf",
                   documents=("Someone - CV.pdf",))
    instance, _, trace = _drive(
        monkeypatch,
        spawns=[_spawned(False, DROPPED), _spawned(True)],
        disk=disk)

    assert trace == ["spawn", f"clean:{folder}", "spawn"]
    # And the notice says so, rather than the promise it used to make that
    # nothing had been touched.
    assert "removed" in instance.sent[-1]


def test_nothing_is_erased_when_the_first_attempt_wrote_no_folder(
        monkeypatch) -> None:
    disk = Outcome(status=FAILED,
                   detail="the build reported success but no dated folder appeared")
    _, outcome, trace = _drive(
        monkeypatch,
        spawns=[_spawned(False, DROPPED), _spawned(True)],
        disk=disk)

    assert trace == ["spawn", "spawn"]
    assert outcome.ok is True


# --------------------------------------------------------------------------
# answering a question, and the session that has since been pruned
# --------------------------------------------------------------------------

def test_an_answer_resumes_the_session_that_asked(monkeypatch) -> None:
    """The whole point of keeping the session id: the run already holds the
    folder, the Match Brief and CLAUDE.md, so the answer is all it needs."""
    job = Job(posting_id="p1", note="", reply_message_id=None, build_id=1,
              answer="Go with option 2.", resume_session="sess-abc")
    _, outcome, trace = _drive(monkeypatch, spawns=[_spawned(True)],
                               disk=_complete(), job=job)

    assert trace == ["spawn:sess-abc"]
    assert outcome.ok is True


def test_a_pruned_session_is_rebuilt_rather_than_lost(monkeypatch) -> None:
    """The CLI prunes its history, and a question can sit unanswered for days.

    Starting over costs a build; dropping the answer costs the application.
    """
    job = Job(posting_id="p1", note="", reply_message_id=None, build_id=1,
              answer="Go with option 2.", resume_session="sess-gone")
    instance, outcome, trace = _drive(
        monkeypatch,
        spawns=[_spawned(False, "No conversation found with session ID: sess-gone"),
                _spawned(True)],
        disk=_complete(), job=job)

    assert trace == ["spawn:sess-gone", "spawn"], "the second is a fresh build"
    assert outcome.ok is True
    assert "session has expired" in instance.sent[-1]


def test_the_fresh_build_still_gets_its_retries(monkeypatch) -> None:
    """A dead session is not a failed attempt, so it must not spend one.

    `build_retries = 1` here: the fallback plus a genuine transient failure plus
    the retry that clears it is three spawns, and only two of them are attempts.
    """
    job = Job(posting_id="p1", note="", reply_message_id=None, build_id=1,
              answer="Option 2.", resume_session="sess-gone")
    disk = Outcome(status=INCOMPLETE,
                   folder="2026/Acme/2026-08-05 - Data Scientist",
                   detail="missing Someone - Cover Letter.pdf",
                   documents=("Someone - CV.pdf",))
    _, outcome, trace = _drive(
        monkeypatch,
        spawns=[_spawned(False, "No conversation found with session ID: sess-gone"),
                _spawned(False, DROPPED),
                _spawned(True)],
        disk=disk, job=job)

    assert trace.count("spawn") == 2, "the resume attempt does not count"
    assert outcome.ok is True
