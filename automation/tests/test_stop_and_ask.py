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


class _Store:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str, str]] = []

    @contextlib.contextmanager
    def connect(self, path=None):
        yield None

    def finish_build(self, conn, build_id, status, folder="", detail=""):
        self.finished.append((status, folder, detail))


class _Builder:
    """Enough of a Builder for the real `_finish` to settle a run."""

    _finish = builder.Builder._finish

    def __init__(self) -> None:
        self.config = _Config()
        self.sent: list[str] = []
        self.topics: list[str] = []

    async def _reply(self, job, text: str,
                     topic: str = "processing_build") -> None:
        self.sent.append(text)
        self.topics.append(topic)


def _settle(monkeypatch, *, ok: bool, detail: str, closing: str, disk: Outcome):
    cleaned: list[tuple[str, str]] = []
    store = _Store()
    monkeypatch.setattr(builder, "store", store)
    monkeypatch.setattr(builder, "locate_output", lambda c, t, cfg: disk)
    monkeypatch.setattr(builder, "clean_up",
                        lambda folder, reason: (cleaned.append((folder, reason))
                                                or f"removed {folder}"))

    instance = _Builder()
    job = Job(posting_id="p1", note="", reply_message_id=None, build_id=1)
    verdict = asyncio.run(builder.Builder._finish(
        instance, job, "RWE", "AI Data Engineer", "<b>RWE</b> — AI Data Engineer",
        ok, detail, closing, Path("build.log"), announced=False))
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


def test_the_half_folder_is_still_erased(monkeypatch) -> None:
    """A folder holding only an archived posting still reads as "applied"."""
    _, _, cleaned, _ = _settle(monkeypatch, ok=True, detail="",
                               closing=QUESTION, disk=_stopped_early())

    assert len(cleaned) == 1
    folder, reason = cleaned[0]
    assert folder == "2026/RWE/2026-08-05 - AI Data Engineer"
    # The reason is one line in the cleanup log, not the paragraphs above it.
    assert len(reason) < 80


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
