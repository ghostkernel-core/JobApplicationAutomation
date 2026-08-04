"""The build reports the application before Interview Prep has rendered.

Interview Prep is a private study aid that lands several minutes after the CV
and cover letter. On the run that prompted this, 7.5 of 40 minutes went to it
while the user waited on a build whose employer-facing documents were already
finished, checked, and logged.

The announcer is a courtesy running alongside the build, so the two properties
that matter are: it never fires on a half-written PDF, and a fault in it never
changes the build's own outcome.
"""

from __future__ import annotations

import asyncio

import pytest

from watcher.builder import DONE, FAILED, INCOMPLETE, Outcome, _settle, \
    ready_message, result_message


# --------------------------------------------------------------------------
# _settle
# --------------------------------------------------------------------------

def test_settle_reports_a_delivered_announcement() -> None:
    async def scenario() -> bool:
        async def announced() -> bool:
            return True
        task = asyncio.ensure_future(announced())
        await asyncio.sleep(0)
        return await _settle(task)

    assert asyncio.run(scenario()) is True


def test_settle_cancels_an_announcer_that_never_fired() -> None:
    """The usual case: the build ends while the announcer is still polling."""
    async def scenario() -> tuple[bool, bool]:
        async def never() -> bool:
            await asyncio.sleep(3600)
            return True
        task = asyncio.ensure_future(never())
        await asyncio.sleep(0)
        return await _settle(task), task.cancelled()

    settled, cancelled = asyncio.run(scenario())
    assert settled is False
    assert cancelled is True


def test_settle_swallows_an_announcer_fault() -> None:
    """A broken announcer must not turn a good build into a failed one."""
    async def scenario() -> bool:
        async def boom() -> bool:
            raise RuntimeError("telegram fell over")
        task = asyncio.ensure_future(boom())
        await asyncio.sleep(0)
        return await _settle(task)

    assert asyncio.run(scenario()) is False


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def test_ready_message_says_prep_is_still_coming() -> None:
    outcome = Outcome(status=DONE, folder="2026/Acme/2026-08-04 - Data Scientist",
                      documents=("X - CV.pdf", "X - Cover Letter.pdf"))
    text = ready_message("<b>Acme</b>", outcome)
    assert "Application ready" in text
    assert "still rendering" in text
    # The folder path is what the user clicks; it belongs in this message.
    assert "2026-08-04 - Data Scientist" in text


def test_completion_after_an_announcement_does_not_repeat_the_folder() -> None:
    """The ready message already carried the path — this one closes the loop."""
    outcome = Outcome(status=DONE, folder="2026/Acme/2026-08-04 - Data Scientist",
                      documents=("X - CV.pdf", "X - Interview Prep.pdf"),
                      tracker_row=True, announced=True)
    text = result_message("<b>Acme</b>", outcome, _log())
    assert "Run complete" in text
    assert "2026-08-04 - Data Scientist" not in text


def test_completion_without_an_announcement_is_unchanged() -> None:
    outcome = Outcome(status=DONE, folder="2026/Acme/2026-08-04 - Data Scientist",
                      documents=("X - CV.pdf",), tracker_row=True)
    text = result_message("<b>Acme</b>", outcome, _log())
    assert "Built" in text
    assert "2026-08-04 - Data Scientist" in text


@pytest.mark.parametrize("status", [FAILED, INCOMPLETE])
def test_a_failure_after_an_announcement_retracts_it(status: str) -> None:
    """Silently contradicting a "ready" message would be the worst outcome.

    If the run fell over after the documents appeared, the user has already been
    told the application was finished. The failure has to say it is taking that
    back, not just report itself.
    """
    outcome = Outcome(status=status, folder="", detail="prep never rendered",
                      cleaned="removed the folder", announced=True)
    text = result_message("<b>Acme</b>", outcome, _log())
    assert "retracts" in text.lower()
    assert "prep never rendered" in text


@pytest.mark.parametrize("status", [FAILED, INCOMPLETE])
def test_a_failure_with_no_announcement_retracts_nothing(status: str) -> None:
    outcome = Outcome(status=status, detail="no folder appeared",
                      cleaned="nothing to remove")
    text = result_message("<b>Acme</b>", outcome, _log())
    assert "retracts" not in text.lower()


def _log():
    from pathlib import Path
    return Path("20260804-190411-acme-data-scientist.log")


# --------------------------------------------------------------------------
# the poll itself
# --------------------------------------------------------------------------

class _Recorder:
    """Stands in for the Builder: just enough for _announce_ready to run."""

    def __init__(self) -> None:
        self.config = object()
        self.sent: list[str] = []

    async def _reply(self, job, text: str) -> None:
        self.sent.append(text)


def _drive(monkeypatch, tmp_path, sizes_over_time):
    """Run the announcer against a folder whose PDFs change size per poll.

    `sizes_over_time` is one (cv_bytes, letter_bytes) pair per poll.
    """
    from watcher import builder

    folder = tmp_path / "2026-08-04 - Data Scientist"
    folder.mkdir()
    names = ("X - CV.pdf", "X - Cover Letter.pdf")

    monkeypatch.setattr(builder, "READY_POLL_SECONDS", 0)
    monkeypatch.setattr(builder.dedupe, "required_pdfs", lambda: names)
    monkeypatch.setattr(
        builder, "locate_output",
        lambda company, title, config: Outcome(
            status=DONE, folder=str(folder), documents=names),
    )
    monkeypatch.setattr(builder, "ready_message", lambda label, outcome: "READY")

    steps = iter(sizes_over_time)

    async def fake_sleep(_seconds):
        # Each poll sees the next snapshot of a folder being written into.
        try:
            cv, letter = next(steps)
        except StopIteration:
            raise asyncio.CancelledError from None
        (folder / names[0]).write_bytes(b"x" * cv)
        (folder / names[1]).write_bytes(b"y" * letter)

    monkeypatch.setattr(builder.asyncio, "sleep", fake_sleep)

    recorder = _Recorder()

    async def scenario():
        try:
            return await builder.Builder._announce_ready(
                recorder, job=None, company="Acme",
                title="Data Scientist", label="<b>Acme</b>")
        except asyncio.CancelledError:
            return False

    return asyncio.run(scenario()), recorder


def test_announcer_waits_for_the_pdfs_to_stop_growing(monkeypatch, tmp_path) -> None:
    """A PDF still being flushed grows between polls; that is not "ready"."""
    fired, recorder = _drive(monkeypatch, tmp_path, [
        (10, 10),    # first sighting — no baseline yet
        (500, 200),  # still being written
        (900, 400),  # still being written
    ])
    assert fired is False
    assert recorder.sent == []


def test_announcer_fires_once_the_sizes_hold_steady(monkeypatch, tmp_path) -> None:
    fired, recorder = _drive(monkeypatch, tmp_path, [
        (10, 10),
        (900, 400),
        (900, 400),  # quiet interval — the render is done
    ])
    assert fired is True
    assert recorder.sent == ["READY"]


def test_announcer_ignores_a_zero_byte_placeholder(monkeypatch, tmp_path) -> None:
    """latexmk touches its output before filling it; an empty file is not a PDF."""
    fired, recorder = _drive(monkeypatch, tmp_path, [
        (0, 0),
        (0, 0),
        (0, 0),
    ])
    assert fired is False
    assert recorder.sent == []
