"""A build that stops to ask a question must deliver the question.

`CLAUDE.md` gives the pipeline three reasons to stop mid-run — a claim the
canonical profile does not support, a posting that cannot be captured, an
ambiguous company name — and tells it to stop and say so rather than invent an
answer. One build did exactly that: the posting was built around an agent
framework stack the profile does not carry, and the run laid out three options
and waited.

The watcher then reported it as `incomplete: missing CV.pdf, Cover Letter.pdf`,
filed it under Failed, and deleted the folder. The question itself never left
the log. From the outside a correct refusal was indistinguishable from a broken
build, and the one thing that would have resolved it — what the run actually
said — was the one thing thrown away.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from watcher import builder
from watcher.builder import (DONE, FAILED, INCOMPLETE, NEEDS_DECISION, Job,
                             Outcome)
from watcher.notifier import TELEGRAM_MAX_CHARS


# Abridged from the build that prompted this, keeping its shape: a headline, the
# gap it found, and the choice it put back to the user.
QUESTION = """The Match Brief just came back with a real integrity flag — this \
is exactly the "stop and ask" case CLAUDE.md calls out.

**The gap:** the posting is centered on building AI agents. None of that is in \
`rules/00-canonical-profile.md`. The rest of it matches your background well.

Three ways to go:
1. **Proceed as-is** — lean on the data-engineering half and stay silent on the \
agent-building ask.
2. **Update the canonical profile first**, then run the pipeline.
3. **Skip this posting.**

Which do you want?"""


class _Config:
    duplicate_title_ratio = 0.8
    duplicate_lookback_days = 365
    build_retries = 0
    build_timeout_minutes = 45

    def topic_for(self, kind: str) -> int | None:
        return {"targeted_build": 162, "failed_build": 168,
                "completed_build": 165}.get(kind)


class _Store:
    QUESTION_STOP_AND_ASK = "stop_and_ask"

    def __init__(self) -> None:
        self.finished: list[tuple[str, str, str]] = []
        self.asked: list[dict] = []

    @contextlib.contextmanager
    def connect(self, path=None):
        yield None

    def finish_build(self, conn, build_id, status, folder="", detail=""):
        self.finished.append((status, folder, detail))

    def ask_question(self, conn, build_id, posting_id, kind, chat_id,
                     message_id, thread_id=None, question="", folder="",
                     session_id=""):
        self.asked.append({"build_id": build_id, "posting_id": posting_id,
                           "kind": kind, "chat_id": chat_id,
                           "message_id": message_id, "thread_id": thread_id,
                           "question": question, "folder": folder,
                           "session_id": session_id})
        return len(self.asked)


class _Builder:
    """Enough of a Builder for the real `_finish` to settle a run."""

    _finish = builder.Builder._finish

    def __init__(self) -> None:
        self.config = _Config()
        self.notifier = type("N", (), {"chat_id": "-100999"})()
        self.sent: list[str] = []
        self.topics: list[str] = []

    async def _reply(self, job, text: str,
                     topic: str = "processing_build") -> int:
        self.sent.append(text)
        self.topics.append(topic)
        return 700 + len(self.sent)


def _settle(monkeypatch, *, ok: bool, detail: str, closing: str, disk: Outcome,
            session_id: str = "sess-1"):
    cleaned: list[tuple[str, str]] = []
    store = _Store()
    monkeypatch.setattr(builder, "store", store)
    monkeypatch.setattr(builder, "locate_output", lambda c, t, cfg: disk)
    monkeypatch.setattr(builder, "clean_up",
                        lambda folder, reason: (cleaned.append((folder, reason))
                                                or f"removed {folder}"))

    instance = _Builder()
    job = Job(posting_id="p1", note="", reply_message_id=None, build_id=1)
    run = builder.RunResult(ok=ok, detail=detail, closing=closing,
                            session_id=session_id, log_file=Path("build.log"))
    verdict = asyncio.run(builder.Builder._finish(
        instance, job, "RWE", "AI Data Engineer", "<b>RWE</b> — AI Data Engineer",
        run, announced=False))
    return instance, store, cleaned, verdict


def _stopped_early() -> Outcome:
    """What the disk looked like: a folder, the posting, and nothing else."""
    return Outcome(status=INCOMPLETE,
                   folder="2026/RWE/2026-08-05 - AI Data Engineer",
                   detail="missing Someone - CV.pdf, Someone - Cover Letter.pdf")


# --------------------------------------------------------------------------
# how the run is filed
# --------------------------------------------------------------------------

def test_a_clean_stop_is_not_a_failure(monkeypatch) -> None:
    """The regression, in one assertion."""
    _, store, _, verdict = _settle(monkeypatch, ok=True, detail="",
                                   closing=QUESTION, disk=_stopped_early())

    assert store.finished[-1][0] == NEEDS_DECISION
    assert verdict == "Waiting on you"


def test_the_question_goes_where_the_approval_came_from(monkeypatch) -> None:
    """Not Failed. The user said yes in that topic; this answers them there."""
    instance, _, _, _ = _settle(monkeypatch, ok=True, detail="",
                                closing=QUESTION, disk=_stopped_early())

    assert instance.topics[-1] == "targeted_build"


def test_the_question_itself_reaches_telegram(monkeypatch) -> None:
    """The whole point: what it asked, not which files are absent."""
    instance, _, _, _ = _settle(monkeypatch, ok=True, detail="",
                                closing=QUESTION, disk=_stopped_early())

    text = instance.sent[-1]
    assert "Which do you want?" in text
    assert "Update the canonical profile first" in text
    # The file list was the old report and explained nothing.
    assert "missing Someone - CV.pdf" not in text


def test_the_folder_is_kept_while_the_question_is_open(monkeypatch) -> None:
    """The run is paused, not abandoned.

    The archived posting, the Match Brief and the Research Note are most of
    what makes answering cheap; erasing them turned every answer into a rebuild
    from step 00. Nothing reads as "already applied" in the meantime, because
    `dedupe.is_complete` requires the PDFs. Cleanup moves to whichever comes
    first — the user declining, or the question expiring.
    """
    _, _, cleaned, _ = _settle(monkeypatch, ok=True, detail="",
                               closing=QUESTION, disk=_stopped_early())

    assert cleaned == []


def test_a_failed_run_is_still_erased_on_the_spot(monkeypatch) -> None:
    """Only a question buys a reprieve. A failure leaves nothing behind."""
    _, _, cleaned, _ = _settle(monkeypatch, ok=False, detail="API Error: 503",
                               closing="", disk=_stopped_early())

    assert len(cleaned) == 1
    folder, reason = cleaned[0]
    assert folder == "2026/RWE/2026-08-05 - AI Data Engineer"
    # The reason is one line in the cleanup log, not the paragraphs above it.
    assert len(reason) < 80


def test_the_question_is_recorded_against_the_message_that_asked_it(
        monkeypatch) -> None:
    """Without the row the message is just text: a reply has nothing to match
    against, and the run's own "answer here" is a promise nothing keeps."""
    instance, store, _, _ = _settle(monkeypatch, ok=True, detail="",
                                    closing=QUESTION, disk=_stopped_early())

    assert len(store.asked) == 1
    asked = store.asked[0]
    assert asked["kind"] == "stop_and_ask"
    assert asked["message_id"] == 701, "the message the question arrived in"
    assert asked["thread_id"] == 162, "the Targeted topic it landed in"
    assert asked["session_id"] == "sess-1", "what --resume will continue"
    assert asked["folder"] == "2026/RWE/2026-08-05 - AI Data Engineer"
    assert "Which do you want?" in asked["question"]
    assert instance.topics[-1] == "targeted_build"


def test_a_run_that_did_not_stop_to_ask_records_no_question(monkeypatch) -> None:
    disk = Outcome(status=DONE,
                   folder="2026/RWE/2026-08-05 - AI Data Engineer",
                   documents=("Someone - CV.pdf", "Someone - Cover Letter.pdf"))
    _, store, _, _ = _settle(monkeypatch, ok=True, detail="",
                             closing="All done.", disk=disk)

    assert store.asked == []


def test_a_clean_run_that_wrote_nothing_reads_the_same_way(monkeypatch) -> None:
    """No attempt is made to tell the two apart — see `_finish`.

    Both ended cleanly with no application, both are cleaned up, and quoting
    what the run said beats "reported success but no dated folder appeared"
    either way.
    """
    disk = Outcome(status=FAILED,
                   detail="the build reported success but no dated folder appeared")
    instance, store, _, _ = _settle(
        monkeypatch, ok=True, detail="",
        closing="I could not reach the posting and no text was pasted.",
        disk=disk)

    assert store.finished[-1][0] == NEEDS_DECISION
    assert "could not reach the posting" in instance.sent[-1]


def test_a_failing_run_is_still_a_failure(monkeypatch) -> None:
    """The new branch only covers clean exits — `ok` is what separates them."""
    _, store, _, verdict = _settle(
        monkeypatch, ok=False, detail="API Error: 503", closing=QUESTION,
        disk=_stopped_early())

    assert store.finished[-1][0] == FAILED
    assert verdict == "Failed"


def test_a_complete_run_is_untouched(monkeypatch) -> None:
    disk = Outcome(status=DONE,
                   folder="2026/RWE/2026-08-05 - AI Data Engineer",
                   documents=("Someone - CV.pdf", "Someone - Cover Letter.pdf"))
    _, store, cleaned, verdict = _settle(monkeypatch, ok=True, detail="",
                                         closing="All done.", disk=disk)

    assert store.finished[-1][0] == DONE
    assert cleaned == [] and verdict == "Complete"


# --------------------------------------------------------------------------
# what gets quoted
# --------------------------------------------------------------------------

def test_the_paragraphs_survive() -> None:
    """A question with numbered options is unreadable as one long line."""
    tidied = builder._tidy("Three   ways to go:\n\n\n1. **Proceed**\n2. **Skip**\n")

    assert tidied == "Three ways to go:\n\n1. **Proceed**\n2. **Skip**"


def test_only_the_closing_turns_are_kept() -> None:
    """The orchestrator ends a turn per handoff; a run has many of these."""
    turns = [f"turn {i}" for i in range(10)]

    assert builder._closing_words(turns) == "turn 7\n\nturn 8\n\nturn 9"


def test_the_question_is_not_only_the_last_turn() -> None:
    """The build that prompted this signed off after the question, not with it.

    Its final words were "still holding on your decision from above" — useless
    on its own, since the message it points at is the one that was lost.
    """
    quoted = builder._closing_words([
        "Both matcher and researcher are running in parallel now.",
        QUESTION,
        "Company research is in now too; still holding on your decision.",
    ])

    assert "Which do you want?" in quoted


def test_a_long_run_of_words_is_capped() -> None:
    quoted = builder._closing_words(["x" * 9000])

    assert len(quoted) <= builder.CLOSING_CHARS
    assert quoted.endswith("…")


def test_the_message_stays_inside_telegrams_limit() -> None:
    outcome = Outcome(status=NEEDS_DECISION, detail="word " * 4000)
    text = builder.result_message("<b>RWE</b>", outcome, Path("build.log"))

    assert len(text) <= TELEGRAM_MAX_CHARS


def test_the_cut_never_lands_inside_an_entity() -> None:
    """Telegram rejects a message ending in a half-written `&amp;`."""
    outcome = Outcome(status=NEEDS_DECISION, detail="&" * 4000)
    text = builder.result_message("<b>RWE</b>", outcome, Path("build.log"))

    assert len(text) <= TELEGRAM_MAX_CHARS
    body, _, _ = text.rpartition("\n")
    assert not body.rstrip("…").endswith(("&", "&a", "&am", "&amp"))


# --------------------------------------------------------------------------
# answering it
# --------------------------------------------------------------------------
#
# The other half of the same bug. Asking the question was worth nothing while
# the answer had nowhere to go: the message ended "Reply here with what you
# want done", and the reply came back "That message isn't one of mine."

class _Replied:
    """The message the user sent, and what the watcher said back to it.

    Carries the whole set of content fields a real `telegram.Message` has, not
    just the one under test. Both live failures this file covers were about the
    watcher looking at the wrong field — `text` where the user had typed a
    caption, `reply_to_message` where Telegram had filled in a topic root — and
    a fake that only defines the field being asserted cannot catch that class
    of bug at all.
    """

    chat_id = -100999
    message_id = 900

    def __init__(self, text: str = "", *, answering: int | None = None,
                 thread_id: int | None = None, caption: str = "",
                 document=None, photo=()) -> None:
        self.text = text
        self.caption = caption
        self.document = document
        self.photo = list(photo)
        self.video = self.audio = self.voice = None
        self.video_note = self.animation = None
        self.message_thread_id = thread_id
        self.reply_to_message = (
            type("R", (), {"message_id": answering})() if answering else None)
        self.replies: list[str] = []

    async def reply_html(self, text: str) -> None:
        self.replies.append(text)


class _InTopic(_Replied):
    """What Telegram actually sends for an ordinary message in a forum topic.

    Not a reply — the user typed it into the topic and pressed send — but
    `reply_to_message` is filled in with the topic's own root message anyway,
    because that field is how a client knows which topic a message belongs to.

    That is one live failure in full: `filters.REPLY` is
    `bool(message.reply_to_message)`, so the reply handler claimed every
    message in every topic, the bare-message handler became unreachable there,
    and a copy-pasted answer was looked up as a reply to the topic root. It
    came back "That message isn't one of mine."
    """

    def __init__(self, text: str = "", *, thread_id: int = 162, **kwargs) -> None:
        super().__init__(text, thread_id=thread_id, answering=thread_id,
                         **kwargs)


class _Queue:
    """Records what reached the builder, without being one."""

    def __init__(self) -> None:
        self.answered: list[tuple[str, str]] = []
        self.queued: list[tuple[str, str, bool]] = []

    async def answer(self, question, text: str) -> int:
        self.answered.append((question["posting_id"], text))
        return 0

    async def enqueue(self, posting_id, note, reply_message_id=None,
                      override_duplicate: bool = False) -> int:
        self.queued.append((posting_id, note, override_duplicate))
        return 0


def _chat(monkeypatch, tmp_path, *, questions=(), build_enabled: bool = True):
    """A watcher with `questions` outstanding, ready to be replied to."""
    import run_watcher
    from watcher import store as real_store
    from watcher.config import Config

    path = tmp_path / "watch.db"
    real_store.init_db(path)
    monkeypatch.setattr(real_store, "DB_PATH", path)
    monkeypatch.setattr(real_store, "ensure_dirs", lambda: None)
    # The weekly proposal is consulted before any posting lookup and reads a
    # real file; nothing here is about it.
    monkeypatch.setattr(run_watcher.kb, "pending", lambda: None)

    now = "2026-08-06T12:00:00"
    with real_store.connect() as conn:
        for n, (posting_id, company, kind, folder) in enumerate(questions, 1):
            conn.execute(
                """INSERT INTO postings (id, loose_key, source, provider, url,
                                         canonical_url, company, title,
                                         first_seen_at, description)
                   VALUES (?, ?, 'ats:x', 'greenhouse', ?, ?, ?,
                           'Data Scientist', ?, '')""",
                (posting_id, f"{company}|DS", f"https://x.test/{posting_id}",
                 f"https://x.test/{posting_id}", company, now))
            build = real_store.queue_build(conn, posting_id)
            real_store.ask_question(conn, build, posting_id, kind, "-100999",
                                    800 + n, thread_id=162,
                                    question=QUESTION, folder=folder,
                                    session_id="sess-1")

    queue = _Queue()
    app = type("App", (), {})()
    app.bot_data = {
        "notifier": type("N", (), {
            "chat_id": "-100999",
            "config": Config(build={"enabled": build_enabled}),
        })(),
        "builder": queue,
    }
    context = type("Ctx", (), {"application": app, "args": []})()
    return run_watcher, context, queue


def _send(run_watcher, handler, context, message):
    asyncio.run(handler(type("U", (), {"effective_message": message})(), context))
    return message.replies[0] if message.replies else ""


def test_a_reply_to_the_question_reaches_the_run(monkeypatch, tmp_path) -> None:
    """The literal symptom: this reply used to come back "not one of mine"."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    message = _Replied("option 2, update the profile first", answering=801)

    text = _send(rw, rw.on_reply, context, message)

    assert queue.answered == [("p1", "option 2, update the profile first")]
    assert "RWE" in text


def test_free_text_is_passed_through_rather_than_parsed(
        monkeypatch, tmp_path) -> None:
    """"no sponsorship needed, I have a permit" begins with `no`. The grammar
    would read that as a decline and erase the folder; the run that asked is the
    only thing that knows what its own answer means."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    answer = "no sponsorship needed, I already hold a permit"

    _send(rw, rw.on_reply, context, _Replied(answer, answering=801))

    assert queue.answered == [("p1", answer)]


def test_a_bare_message_answers_the_one_open_question(
        monkeypatch, tmp_path) -> None:
    """Finding and long-pressing the right message to answer a question the bot
    just asked is friction with nothing behind it."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])

    _send(rw, rw.on_stray, context, _Replied("go with option 1", thread_id=162))

    assert queue.answered == [("p1", "go with option 1")]


def test_two_open_questions_are_listed_rather_than_guessed_between(
        monkeypatch, tmp_path) -> None:
    rw, context, queue = _chat(monkeypatch, tmp_path, questions=[
        ("p1", "RWE", "stop_and_ask", ""),
        ("p2", "Roche", "stop_and_ask", "")])

    text = _send(rw, rw.on_stray, context, _Replied("yes", thread_id=162))

    assert queue.answered == [], "a wrong guess answers the wrong run"
    assert "RWE" in text and "Roche" in text
    assert "/pending" in text


def test_a_bare_message_with_nothing_open_starts_nothing(
        monkeypatch, tmp_path) -> None:
    """The rule the user set: a stray message may never begin a build."""
    rw, context, queue = _chat(monkeypatch, tmp_path)

    text = _send(rw, rw.on_stray, context, _Replied("yes"))

    assert queue.answered == [] and queue.queued == []
    assert "/pending" in text


def test_a_message_in_the_wrong_topic_is_not_applied_to_another(
        monkeypatch, tmp_path) -> None:
    """`open_questions` filters by thread rather than falling back to all of
    them, so a message typed into the wrong topic is refused, not redirected."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])

    _send(rw, rw.on_stray, context, _Replied("option 1", thread_id=999))

    assert queue.answered == []


def test_no_still_means_no_and_takes_the_folder_with_it(
        monkeypatch, tmp_path) -> None:
    """The folder is retained while the question is open. A decline is the
    moment the run stops being paused and starts being abandoned."""
    rw, context, queue = _chat(monkeypatch, tmp_path, questions=[
        ("p1", "RWE", "stop_and_ask", "2026/RWE/2026-08-05 - Data Scientist")])
    cleaned: list[tuple[str, str]] = []
    monkeypatch.setattr(rw.builder_mod, "clean_up",
                        lambda folder, reason: cleaned.append((folder, reason))
                        or "removed the folder")

    text = _send(rw, rw.on_reply, context, _Replied("no", answering=801))

    assert queue.answered == []
    assert cleaned and cleaned[0][0].endswith("2026-08-05 - Data Scientist")
    assert "Dropped" in text and "RWE" in text


def test_declining_a_duplicate_does_not_erase_the_application_it_matched(
        monkeypatch, tmp_path) -> None:
    """The worst thing this feature could do.

    A duplicate question is about a folder that already holds a finished
    application. If that folder were recorded as the question's own, `no` would
    read as "abandon this run, clean up after it" and delete a complete
    application and its tracker row — for an answer that meant "fine, leave it".
    """
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "Roche", "duplicate", "")])
    cleaned: list[tuple[str, str]] = []
    monkeypatch.setattr(rw.builder_mod, "clean_up",
                        lambda folder, reason: cleaned.append((folder, reason))
                        or "removed")

    _send(rw, rw.on_reply, context, _Replied("no", answering=801))

    assert cleaned == []


def test_yes_anyway_overrides_a_duplicate_decline(monkeypatch, tmp_path) -> None:
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "Roche", "duplicate", "")])

    text = _send(rw, rw.on_reply, context, _Replied("yes anyway", answering=801))

    assert queue.queued == [("p1", "", True)], (
        "'anyway' overrules this end of the system — it is not a build note")
    assert queue.answered == [], "there is no run to resume; it never started"
    assert "anyway" in text


def test_an_overridden_duplicate_still_carries_a_real_note(
        monkeypatch, tmp_path) -> None:
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "Roche", "duplicate", "")])

    _send(rw, rw.on_reply, context,
          _Replied("yes anyway, add German", answering=801))

    assert queue.queued == [("p1", "add German", True)]


def test_free_text_at_a_duplicate_is_explained_rather_than_forwarded(
        monkeypatch, tmp_path) -> None:
    """Nothing is waiting on an answer — the build never started — so passing
    the text to a model would hand it to a session that does not exist."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "Roche", "duplicate", "")])

    text = _send(rw, rw.on_reply, context,
                 _Replied("what did it match?", answering=801))

    assert queue.queued == [] and queue.answered == []
    assert "yes anyway" in text


def test_an_answered_question_is_closed(monkeypatch, tmp_path) -> None:
    """Or the next bare message in that topic answers it a second time."""
    from watcher import store as real_store

    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])

    _send(rw, rw.on_reply, context, _Replied("option 1", answering=801))

    with real_store.connect() as conn:
        assert real_store.open_questions(conn) == []


def test_an_answer_with_builds_switched_off_says_so(
        monkeypatch, tmp_path) -> None:
    rw, context, queue = _chat(monkeypatch, tmp_path, build_enabled=False,
                               questions=[("p1", "RWE", "stop_and_ask", "")])

    text = _send(rw, rw.on_reply, context, _Replied("option 1", answering=801))

    assert queue.answered == []
    assert "switched off" in text


# --------------------------------------------------------------------------
# answering it from inside a forum topic
# --------------------------------------------------------------------------
#
# Both routes worked in isolation and neither worked in the chat, because the
# chat is a forum and every message in a topic arrives looking like a reply.

def test_a_message_typed_into_a_topic_is_not_a_reply_to_the_topic(
        monkeypatch, tmp_path) -> None:
    """The bug, at the level it actually has to be fixed.

    `on_message` does the split itself precisely because a filter reading
    `reply_to_message` cannot: in a topic that field is always set.
    """
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    message = _InTopic("here is the posting text, go ahead")

    assert rw._replied_to(message) is None
    _send(rw, rw.on_message, context, message)

    assert queue.answered == [("p1", "here is the posting text, go ahead")]


def test_a_real_reply_inside_a_topic_is_still_a_reply(
        monkeypatch, tmp_path) -> None:
    """The other half. Ignoring the field outright would break the reply route
    everywhere it is actually used, since the questions are asked in a topic."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    message = _Replied("option 2", answering=801, thread_id=162)

    assert rw._replied_to(message).message_id == 801
    _send(rw, rw.on_message, context, message)

    assert queue.answered == [("p1", "option 2")]


def test_a_command_in_a_topic_is_redirected_not_answered_as_one(
        monkeypatch, tmp_path) -> None:
    """Only General serves commands, and a waiting run must not be told "/pending"
    is the answer it was holding out for."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])

    text = _send(rw, rw.on_message, context, _InTopic("/pending"))

    assert queue.answered == []
    assert "General" in text


# --------------------------------------------------------------------------
# answering it with a file
# --------------------------------------------------------------------------
#
# The question that prompted this asked for the posting text, because the
# capture had failed. A file was the obvious answer, and both handlers required
# `filters.TEXT` — so it matched none of them and nothing happened at all.

class _File:
    file_id = "f-1"
    file_unique_id = "u-1"

    def __init__(self, name: str = "posting.html") -> None:
        self.file_name = name


def _with_downloads(monkeypatch, rw, context, tmp_path, *, breaks: bool = False):
    """A bot that hands back files, and the answers directory it fills."""
    answers = tmp_path / "answers"
    monkeypatch.setattr(rw, "ANSWER_FILES", answers)

    class _Handle:
        async def download_to_drive(self, path):
            path.write_text("<html>the posting</html>", encoding="utf-8")

    class _Bot:
        async def get_file(self, file_id):
            if breaks:
                raise RuntimeError("file is too big")
            return _Handle()

    context.bot = _Bot()
    return answers


def test_a_file_answers_the_question_by_path_not_by_content(
        monkeypatch, tmp_path) -> None:
    """A saved posting page runs to hundreds of KB; the prompt carries the path.

    Which is also what CLAUDE.md tells the pipeline to do with these files —
    grep them or read them in slices, never whole.
    """
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    answers = _with_downloads(monkeypatch, rw, context, tmp_path)

    _send(rw, rw.on_message, context,
          _InTopic(caption="here you go", document=_File()))

    saved = answers / "1" / "posting.html"
    assert saved.read_text(encoding="utf-8") == "<html>the posting</html>"
    posting_id, answer = queue.answered[0]
    assert posting_id == "p1"
    assert "here you go" in answer, "the caption is half the message"
    assert str(saved) in answer
    assert "<html>" not in answer, "the file travels as a path, not inline"


def test_a_file_with_nothing_typed_under_it_still_answers(
        monkeypatch, tmp_path) -> None:
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    _with_downloads(monkeypatch, rw, context, tmp_path)

    _send(rw, rw.on_message, context, _InTopic(document=_File()))

    assert len(queue.answered) == 1


def test_a_file_that_cannot_be_downloaded_says_so(monkeypatch, tmp_path) -> None:
    """Silence is what made this a bug in the first place."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    _with_downloads(monkeypatch, rw, context, tmp_path, breaks=True)

    text = _send(rw, rw.on_message, context, _InTopic(document=_File()))

    assert queue.answered == []
    assert "20 MB" in text


def test_a_hostile_file_name_cannot_escape_the_answers_folder(
        monkeypatch, tmp_path) -> None:
    """The name comes from whoever sent the file, so it is not a path."""
    rw, context, queue = _chat(monkeypatch, tmp_path,
                               questions=[("p1", "RWE", "stop_and_ask", "")])
    answers = _with_downloads(monkeypatch, rw, context, tmp_path)

    _send(rw, rw.on_message, context,
          _InTopic(document=_File("../../../etc/passwd")))

    written = [p for p in answers.rglob("*") if p.is_file()]
    assert [p.parent for p in written] == [answers / "1"]
