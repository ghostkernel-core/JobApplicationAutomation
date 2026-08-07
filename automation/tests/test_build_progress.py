"""The live step checklist a build edits into its own Telegram message.

Everything here is read out of the stream `claude -p` already produces, so the
tests are written against the shapes that stream really takes rather than a
tidied idea of them. Two of those shapes cost a debugging session each and are
the reason most of this file exists:

* a subagent's own tool calls carry `parent_tool_use_id`, and outnumber the
  orchestrator's by roughly fourteen to one — unfiltered, the checklist is every
  `Read` and `Grep` of every agent instead of the pipeline;
* an agent launched in the background answers its `tool_result` in milliseconds
  with "Async agent launched successfully", and only reports finishing much later
  in a `system` / `task_notification`. Closing the row on the acknowledgement is
  the obvious reading and it is wrong in a way that looks right: every parallel
  step shows `0m 00s` and the checklist races to the end while the build is still
  on step 01.

The other half is the failure behaviour. This runs on a timer for the length of a
forty-minute build, against a message the user can delete at any moment, and it
is a convenience sitting on top of something that is not. So the last two tests
are about a build surviving its progress reporting, not about the checklist.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys

import pytest

from watcher import progress


# --------------------------------------------------------------------------
# stream fixtures
# --------------------------------------------------------------------------

def _at(second: int) -> str:
    """A stream timestamp. The CLI emits UTC with a trailing Z."""
    return f"2026-08-05T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _epoch(second: int) -> float:
    """The same instant as epoch seconds, which is what `render` is given."""
    return progress.event_time({"timestamp": _at(second)})


def _use(tool: str, use_id: str, second: int, *, parent: str | None = None,
         **inputs) -> dict:
    return {
        "type": "assistant",
        "timestamp": _at(second),
        "parent_tool_use_id": parent,
        "message": {"content": [
            {"type": "tool_use", "id": use_id, "name": tool, "input": inputs},
        ]},
    }


def _agent(name: str, use_id: str, second: int, **kw) -> dict:
    return _use("Agent", use_id, second, subagent_type=name, **kw)


def _result(use_id: str, second: int, *, text: str = "done",
            is_error: bool = False, parent: str | None = None) -> dict:
    return {
        "type": "user",
        "timestamp": _at(second),
        "parent_tool_use_id": parent,
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": use_id,
            "content": [{"type": "text", "text": text}],
            "is_error": is_error,
        }]},
    }


def _launch_ack(use_id: str, second: int, *, parent: str | None = None) -> dict:
    """What a backgrounded agent answers with, immediately, before doing anything."""
    return _result(use_id, second, parent=parent, text=(
        "Async agent launched successfully. (This tool result is internal "
        "metadata.)\nagentId: task-1"))


def _task_progress(task_id: str, use_id: str, ms: int) -> dict:
    """A background task reporting how long it has been running. No timestamp."""
    return {
        "type": "system", "subtype": "task_progress",
        "task_id": task_id, "tool_use_id": use_id,
        "usage": {"duration_ms": ms},
    }


def _task_done(task_id: str, use_id: str, status: str = "completed",
               ms: int | None = None) -> dict:
    """The agent's own last word. `ms` is its final duration, when it reports one."""
    event = {
        "type": "system", "subtype": "task_notification",
        "task_id": task_id, "tool_use_id": use_id, "status": status,
    }
    if ms is not None:
        event["usage"] = {"duration_ms": ms}
    return event


def _feed(tracker: progress.Tracker, events: list[dict]) -> list[bool]:
    return [tracker.feed(event) for event in events]


@pytest.fixture()
def tracker() -> progress.Tracker:
    # live=False so unstamped system events fall back to the stream's own clock
    # rather than to the wall clock, which is what replaying a file needs and
    # what makes durations here assertable at all.
    return progress.Tracker(live=False)


# --------------------------------------------------------------------------
# what counts as a step
# --------------------------------------------------------------------------

def test_a_subagents_own_tool_calls_are_not_steps(tracker):
    """The 14-to-1 case: nested calls must not reach the checklist."""
    _feed(tracker, [
        _agent("01-experience-matcher", "t1", 0),
        # Everything the matcher then does carries its tool_use_id as parent.
        _use("Read", "n1", 10, parent="t1", file_path="rules/00-canonical-profile.md"),
        _use("Grep", "n2", 20, parent="t1", pattern="PyTorch"),
        _use("Bash", "n3", 30, parent="t1",
             command="python scripts/render_latex_application.py x.json"),
        _result("n3", 40, parent="t1"),
    ])
    running = [row for row in tracker.visible_rows if row.state == progress.RUNNING]
    assert [row.step.key for row in running] == ["match"]
    # The nested render must not have started the render row.
    assert tracker.rows["render_docs"].state == progress.PENDING


def test_a_pipeline_agent_counts_even_when_the_stream_misattributes_it(tracker):
    """The Roche case: the parent id is wrong, and the step happened anyway.

    A resumed run stamps the orchestrator's next calls with the id of whatever
    agent was still open when it stopped to ask. Both these steps ran to
    completion and both were dropped, leaving rows indistinguishable from work
    that never started.
    """
    _feed(tracker, [
        _agent("01-experience-matcher", "m1", 0, parent="toolu_archiver"),
        _launch_ack("m1", 1, parent="toolu_archiver"),
        _task_done("task-m", "m1"),
    ])
    assert tracker.rows["match"].state == progress.DONE


def test_a_nested_script_call_is_still_not_a_step(tracker):
    """Forgiving the parent id for agents must not forgive it for scripts.

    Subagents run the renderer and the QA pass for their own reasons, and those
    are the firehose the parent id exists to keep out.
    """
    changed = _feed(tracker, [
        _use("Bash", "n1", 0, parent="t1",
             command='python scripts/qa_application.py "f" --no-images'),
        _result("n1", 3, parent="t1"),
    ])
    assert changed == [False, False]
    assert tracker.rows["qa_docs"].state == progress.PENDING


def test_a_nested_result_for_a_row_we_never_opened_changes_nothing(tracker):
    """A result is taken only for a row already open — the same rule reversed."""
    assert tracker.feed(_result("never-seen", 5, parent="t1")) is False


def test_unrecognised_orchestrator_work_is_ignored(tracker):
    """The orchestrator reads and greps too; the list stays the pipeline."""
    before = tracker.render("t", "s", 0)
    changed = _feed(tracker, [
        _use("Read", "r1", 0, file_path="CLAUDE.md"),
        _use("TaskCreate", "r2", 5, subject="track the run"),
        _use("Bash", "r3", 10, command="ls -la 2026/"),
    ])
    assert changed == [False, False, False]
    assert tracker.render("t", "s", 0) == before


def test_parallel_steps_both_show_as_running(tracker):
    """Phase A runs 01A and 01B together, and the checklist has to show both."""
    _feed(tracker, [
        _agent("01-experience-matcher", "t1", 0),
        _agent("02-company-role-researcher", "t2", 4),
    ])
    running = {row.step.key for row in tracker.visible_rows
               if row.state == progress.RUNNING}
    assert running == {"match", "research"}


def test_render_and_qa_passes_are_told_apart_by_their_flags(tracker):
    """One script name, two steps. The flags are the only difference."""
    _feed(tracker, [
        _use("Bash", "b1", 0, command=(
            'python scripts/render_latex_application.py p.json "f" '
            "--only cv_payload_en --only letter_payload_en")),
        _result("b1", 20),
        _use("Bash", "b2", 30, command='python scripts/qa_application.py "f" --no-images'),
        _result("b2", 33),
        _use("Bash", "b3", 60, command=(
            'python scripts/render_latex_application.py p.json "f" '
            "--only interview_prep_payload_en")),
        _result("b3", 70),
        _use("Bash", "b4", 80, command=(
            'python scripts/qa_application.py "f" --require-prep --no-images')),
        _result("b4", 83),
    ])
    done = {key: tracker.rows[key].state for key in
            ("render_docs", "qa_docs", "render_prep", "qa_prep")}
    assert done == {key: progress.DONE for key in done}
    assert tracker.rows["render_docs"].elapsed(0) == 20
    assert tracker.rows["render_prep"].elapsed(0) == 10


# --------------------------------------------------------------------------
# how a step ends
# --------------------------------------------------------------------------

def test_a_background_launch_is_not_a_finished_step(tracker):
    """The acknowledgement comes back in milliseconds. It is not the result."""
    _feed(tracker, [
        _agent("01-experience-matcher", "t1", 0),
        _launch_ack("t1", 1),
    ])
    assert tracker.rows["match"].state == progress.RUNNING
    assert tracker.rows["match"].finished is None


def test_a_backgrounded_agent_finishes_on_its_task_notification(tracker):
    """…and is timed by what it reports, not by the silence around it.

    Nothing else is on the stream while a parallel phase runs, so the last
    timestamp seen is the launch — six minutes short of the truth.
    """
    _feed(tracker, [
        _agent("01-experience-matcher", "t1", 0),
        _launch_ack("t1", 1),
        _task_progress("task-1", "t1", 120_000),
        _task_progress("task-1", "t1", 384_000),
        _task_done("task-1", "t1"),
    ])
    row = tracker.rows["match"]
    assert row.state == progress.DONE
    assert row.elapsed(0) == 384  # 6m 24s, not the 1s the stream clock knew about


def test_a_step_is_timed_by_its_final_report_not_its_last_ping(tracker):
    """The pings stop before the agent does, and the gap belongs to the step.

    Reading the pings alone put the Roche researcher at 3m 28s for 4m 20s of
    work: every step was short by however long it was quiet before it finished.
    """
    _feed(tracker, [
        _agent("02-company-role-researcher", "r1", 0),
        _launch_ack("r1", 1),
        _task_progress("task-r", "r1", 208_399),
        _task_done("task-r", "r1", ms=260_507),
    ])
    row = tracker.rows["research"]
    assert row.state == progress.DONE
    assert row.elapsed(0) == pytest.approx(260.507)


def test_a_synchronous_agent_still_finishes_on_its_tool_result(tracker):
    """Both launch modes occur in real logs; neither may be the only one handled."""
    _feed(tracker, [
        _agent("03-cv-writer-en", "t1", 0),
        _result("t1", 127, text="cv_payload_en written"),
    ])
    row = tracker.rows["cv_draft"]
    assert row.state == progress.DONE
    assert row.elapsed(0) == 127


def test_a_timestamped_result_corrects_an_unstamped_notification(tracker):
    """A backgrounded script is announced done before it says when.

    `task_notification` carries no timestamp, so on its own it dates the finish
    to whenever the stream last spoke — the launch. The `tool_result` that
    follows knows better and is allowed to say so.
    """
    _feed(tracker, [
        _use("Bash", "b1", 0, command='python scripts/qa_application.py "f"'),
        _task_done("bash-1", "b1"),
        _result("b1", 23),
    ])
    assert tracker.rows["qa_docs"].elapsed(0) == 23


def test_a_failed_step_is_marked_failed(tracker):
    _feed(tracker, [
        _use("Bash", "b1", 0, command='python scripts/qa_application.py "f"'),
        _result("b1", 9, is_error=True, text="QA FAIL"),
        _agent("09-proofreader", "t1", 20),
        _launch_ack("t1", 21),
        _task_done("task-9", "t1", status="failed"),
    ])
    assert tracker.rows["qa_docs"].state == progress.FAILED
    assert tracker.rows["proofread"].state == progress.FAILED


# --------------------------------------------------------------------------
# what the message looks like
# --------------------------------------------------------------------------

def test_the_english_checklist_is_thirteen_rows(tracker):
    """A run that asked for no German never shows a row it will not reach."""
    assert len(tracker.visible_rows) == 13
    assert "German" not in tracker.render("t", "s", 0)


def test_optional_rows_appear_only_once_reached(tracker):
    _feed(tracker, [_agent("07-translator-de", "t1", 0)])
    keys = [row.step.key for row in tracker.visible_rows]
    assert "german" in keys
    # …and in its declared place, not appended to the end.
    assert keys.index("german") == keys.index("letter_verify") + 1


def test_a_running_step_keeps_counting_and_a_finished_one_does_not(tracker):
    _feed(tracker, [
        _agent("01-experience-matcher", "t1", 0),
        _result("t1", 60),
        _agent("02-company-role-researcher", "t2", 10),
    ])
    ten_minutes_in = _epoch(600)
    assert tracker.rows["match"].elapsed(ten_minutes_in) == 60      # frozen at its finish
    assert tracker.rows["research"].elapsed(ten_minutes_in) == 590  # still running


def test_render_fits_a_telegram_message(tracker):
    """Every row filled, longest labels, still nowhere near the 4096 cap."""
    from watcher.notifier import TELEGRAM_MAX_CHARS

    for index, step in enumerate(progress.STEPS):
        tracker.feed(_agent(step.agent or "x", f"t{index}", index * 60)
                     if step.agent else
                     _use("Bash", f"t{index}", index * 60,
                          command=f"python scripts/{step.script} {step.flag}"))
    text = tracker.render("🛠 <b>A Very Long Company Name GmbH</b> — "
                          "Senior Machine Learning Engineer, Foundation Models",
                          "Building · 41m 12s · 9/17 steps", _epoch(59 * 60))
    assert len(tracker.visible_rows) == len(progress.STEPS)
    assert len(text) < TELEGRAM_MAX_CHARS


def test_render_escapes_the_checklist_body():
    """`<pre>` is what makes the columns line up; its contents are still text."""
    tracker = progress.Tracker(live=False)
    tracker.feed(_agent("01-experience-matcher", "t1", 0))
    body = tracker.render("title", "status", 0).split("<pre>")[1]
    assert "<" not in body.replace("</pre>", "")


@pytest.mark.parametrize("seconds,expected", [
    (0, "0m 00s"), (9, "0m 09s"), (65, "1m 05s"), (3599, "59m 59s"),
    (3600, "1h 00m"), (5400, "1h 30m"), (-5, "0m 00s"),
])
def test_duration_formatting(seconds, expected):
    assert progress.format_duration(seconds) == expected


# --------------------------------------------------------------------------
# a retry starts over
# --------------------------------------------------------------------------

def test_reset_clears_the_previous_attempt(tracker):
    """A retry re-runs the pipeline from step 00.

    Carried over, the first attempt's ticks would show a build much further
    along than it is — and its durations against the second attempt's steps.
    """
    _feed(tracker, [
        _agent("01-experience-matcher", "t1", 0),
        _result("t1", 60),
        _agent("02-company-role-researcher", "t2", 65),
    ])
    tracker.reset()
    assert all(row.state == progress.PENDING for row in tracker.visible_rows)
    assert tracker.counts() == (0, 13)
    # And a stale tool_result from the killed attempt closes nothing.
    assert tracker.feed(_result("t2", 200)) is False


# --------------------------------------------------------------------------
# steps the flow went past
# --------------------------------------------------------------------------

def test_a_step_the_flow_went_past_is_marked_rather_than_left_blank(tracker):
    """The complaint this section answers: three blank rows above a running one.

    A resumed build showed `⬜ Archive posting`, `⬜ Match brief`,
    `⬜ Company research` over a CV draft that was ticking, and a header reading
    `0/13 steps`. Every one of those steps had happened; none of them was ever
    going to fill in. Blank is the same mark as "still to come", so the message
    said the run had not started while it was two thirds of the way through.
    """
    _feed(tracker, [_agent("03-cv-writer-en", "t1", 0)])
    passed = {key: tracker.rows[key].state
              for key in ("archive", "match", "research")}
    assert passed == {key: progress.SKIPPED for key in passed}
    # …and what is genuinely still to come keeps saying so.
    assert tracker.rows["cv_verify"].state == progress.PENDING
    assert tracker.rows["tracker"].state == progress.PENDING


def test_a_parallel_sibling_is_not_written_off_a_second_before_it_starts(tracker):
    """Phase B drafts the CV and the letter at once, in that order more often
    than not. Read by position rather than by phase, the first Agent call would
    write off the second one moment before it arrived."""
    _feed(tracker, [_agent("03-cv-writer-en", "t1", 0)])
    assert tracker.rows["letter_draft"].state == progress.PENDING

    _feed(tracker, [_agent("05-cover-letter-writer-en", "t2", 30)])
    assert tracker.rows["letter_draft"].state == progress.RUNNING


def test_the_prep_track_does_not_write_off_the_documents_track(tracker):
    """CLAUDE.md's Phase C starts both tracks in one message and lets them
    interleave, so interview prep beginning says nothing at all about whether
    the renderer has. Hence one phase number across the whole of it."""
    _feed(tracker, [_agent("08-interview-prep", "t1", 0)])
    still_coming = {key: tracker.rows[key].state
                    for key in ("render_docs", "qa_docs")}
    assert still_coming == {key: progress.PENDING for key in still_coming}


def test_a_step_written_off_that_starts_anyway_is_running_again(tracker):
    """Which is what lets the phases be coarse.

    Being wrong here costs a row marked skipped for as long as it takes the step
    to begin, and then corrects itself. Being wrong the other way — a phase split
    too finely — costs a row that is wrong until the build ends.
    """
    _feed(tracker, [_agent("09-proofreader", "t1", 0)])
    assert tracker.rows["prep"].state == progress.SKIPPED

    _feed(tracker, [_agent("08-interview-prep", "t2", 30)])
    assert tracker.rows["prep"].state == progress.RUNNING
    assert tracker.rows["prep"].elapsed(_epoch(90)) == 60


def test_a_skipped_step_counts_towards_the_header(tracker):
    """`0/13 steps` on a build that is four steps in was half the complaint."""
    _feed(tracker, [_agent("03-cv-writer-en", "t1", 0)])
    assert tracker.counts() == (3, 13)


def test_a_skipped_optional_row_stays_hidden(tracker):
    """German on an English-only run is not a gap; it is a document nobody asked
    for. Optional rows appear when they happen and not otherwise, and being
    passed over is not happening."""
    _feed(tracker, [_agent("09-proofreader", "t1", 0)])
    assert tracker.rows["german"].state == progress.SKIPPED
    assert "German" not in tracker.render("t", "s", 0)
    assert len(tracker.visible_rows) == 13


# --------------------------------------------------------------------------
# a resumed run inherits the first half
# --------------------------------------------------------------------------

def _log(tmp_path, events: list[dict], name: str = "earlier.log"):
    """A build log as it sits on disk: the command line, then the NDJSON."""
    path = tmp_path / name
    path.write_text(
        "$ claude -p --output-format stream-json --verbose\n"
        + "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8")
    return path


def test_priming_restores_what_an_earlier_session_finished(tracker, tmp_path):
    """Answering a stop-and-ask resumes the same CLI session, but not the same
    tracker — so the steps done before the question went out are invisible to it
    unless it is handed that session's log first."""
    tracker.prime(_log(tmp_path, [
        _agent("00-posting-archiver", "a1", 0),
        _result("a1", 33),
    ]))
    assert tracker.rows["archive"].state == progress.DONE
    assert tracker.rows["archive"].elapsed(0) == 33, "its own duration, not ours"

    _feed(tracker, [_agent("03-cv-writer-en", "t1", 600)])
    assert tracker.rows["archive"].state == progress.DONE
    assert tracker.counts() == (3, 13), "one done, two passed over, one running"


def test_priming_does_not_carry_a_row_the_earlier_session_left_running(
        tracker, tmp_path):
    """That session has ended. A row still open in it has no finish time and
    never will, so left running it would measure its age against this build's
    clock and report an hour nobody spent."""
    tracker.prime(_log(tmp_path, [_agent("01-experience-matcher", "m1", 0)]))
    row = tracker.rows["match"]
    assert row.state == progress.SKIPPED
    assert row.elapsed(_epoch(3600)) is None


def test_priming_leaves_no_ids_a_live_result_could_close(tracker, tmp_path):
    """Two sessions, two id spaces. A `tool_result` from this run must not be
    able to close a row against a call made in the last one."""
    tracker.prime(_log(tmp_path, [_agent("01-experience-matcher", "m1", 0)]))
    assert tracker.feed(_result("m1", 900)) is False
    assert tracker.rows["match"].state == progress.SKIPPED


def test_priming_does_not_date_the_resumed_build_from_the_first_session(
        tracker, tmp_path):
    """The rows carry over; the clock does not. A build that started a minute ago
    should not report the three quarters of an hour the first half took."""
    tracker.prime(_log(tmp_path, [
        _agent("00-posting-archiver", "a1", 0),
        _result("a1", 33),
    ]))
    assert tracker.started_at is None

    _feed(tracker, [_agent("03-cv-writer-en", "t1", 600)])
    assert tracker.started_at == _epoch(600)
    assert tracker.total_elapsed(_epoch(660)) == 60


# --------------------------------------------------------------------------
# the reporter must not be able to end a build
# --------------------------------------------------------------------------

class _Config:
    build_progress_updates = True
    build_progress_refresh_seconds = 1
    build_progress_min_interval_seconds = 0


class _Job:
    build_id = 1
    posting_id = "p1"
    reply_message_id = 99


class _Notifier:
    """Records edits. `fail` makes every one of them raise."""

    def __init__(self, *, fail: bool = False, alive: bool = True) -> None:
        self.fail, self.alive, self.edits = fail, alive, []

    async def edit(self, message_id: int, text: str) -> bool:
        if self.fail:
            raise RuntimeError("telegram is having a day")
        self.edits.append((message_id, text))
        return self.alive


def _reporter(notifier) -> "object":
    from watcher.builder import _ProgressReporter
    return _ProgressReporter(notifier, _Config(), _Job(), "<b>Acme</b> — Role", 555)


def test_an_edit_that_raises_does_not_reach_the_build():
    """`_ProgressReporter._flush` swallows what `Notifier.edit` promises not to.

    Belt and braces on purpose: `edit` is written never to raise, and this is
    what happens if that is ever untrue. A progress bug costing an application is
    not a trade anyone would make.
    """
    notifier = _Notifier(fail=True)
    reporter = _reporter(notifier)
    reporter.feed(_agent("01-experience-matcher", "t1", 0))
    asyncio.run(reporter.stop("Complete"))  # must not raise


def test_an_earlier_log_that_is_gone_does_not_break_the_checklist(tmp_path):
    """Logs are files on a disk somebody else owns. A resumed build whose first
    half has been tidied away simply starts its checklist where it would have
    anyway — nothing here is worth failing a build over."""
    reporter = _reporter(_Notifier())
    reporter.prime(tmp_path / "deleted.log")  # must not raise
    assert reporter.tracker.counts() == (0, 13)


def test_a_deleted_message_stops_the_edits():
    notifier = _Notifier(alive=False)
    reporter = _reporter(notifier)

    async def run() -> None:
        await reporter._flush("Building")
        assert reporter.message_id is None
        reporter.feed(_agent("01-experience-matcher", "t1", 0))
        await reporter._flush("Building")

    asyncio.run(run())
    assert len(notifier.edits) == 1


def test_identical_text_is_not_re_sent():
    """The tick fires whether or not anything moved; only changes cost a call."""
    notifier = _Notifier()
    reporter = _reporter(notifier)

    async def run() -> None:
        await reporter._flush("Building")
        await reporter._flush("Building")

    asyncio.run(run())
    assert len(notifier.edits) == 1


def test_the_refresh_task_stops_with_the_build():
    """`stop` leaves the final checklist in place and nothing running behind it."""
    notifier = _Notifier()
    reporter = _reporter(notifier)

    async def run() -> None:
        reporter.start()
        reporter.feed(_agent("01-experience-matcher", "t1", 0))
        await asyncio.sleep(0.05)
        await reporter.stop("Complete")

    asyncio.run(run())
    assert notifier.edits, "the checklist was never sent"
    assert "Complete" in notifier.edits[-1][1]
    assert reporter._task is None


def test_feed_never_awaits():
    """It runs inside the loop draining the CLI's stdout, which must not stall.

    A coroutine returned and dropped here would be a build hung on a full pipe,
    so the check is that `feed` is an ordinary function.
    """
    import inspect

    from watcher.builder import _ProgressReporter

    assert not inspect.iscoroutinefunction(_ProgressReporter.feed)
    assert _reporter(_Notifier()).feed(_agent("01-experience-matcher", "t1", 0)) is None


# --------------------------------------------------------------------------
# a checklist left frozen by a restart
# --------------------------------------------------------------------------

def _stale_row(tmp_path, **over) -> dict:
    """A killed build: two steps done, a third still open, then the log ends."""
    log = tmp_path / "build.log"
    log.write_text("\n".join(json.dumps(e) for e in [
        _agent("00-posting-archiver", "t0", 0),
        _result("t0", 60),
        _agent("01-experience-matcher", "t1", 60),
        _result("t1", 180),
        _agent("02-company-role-researcher", "t2", 180),
    ]), encoding="utf-8")
    row = {"company": "Acme", "title": "Data Scientist",
           "progress_message_id": 555, "log_path": str(log)}
    row.update(over)
    return row


def _recover(notifier, row) -> None:
    from watcher.builder import Builder
    asyncio.run(Builder._close_progress(Builder(notifier), row))


def test_a_restart_settles_the_checklist_it_left_running(tmp_path):
    """The one state this feature exists to rule out is a lying "Building".

    The reporter dies with the process, so without this the message reads
    `Building · 18m 20s` for good. The log is the same stream the tracker was
    reading, so it can be replayed into the checklist that was on screen.
    """
    notifier = _Notifier()
    _recover(notifier, _stale_row(tmp_path))

    assert len(notifier.edits) == 1
    text = notifier.edits[0][1]
    assert "Interrupted by a restart" in text
    assert "2/13 steps" in text
    assert "Acme" in text
    # The step that was still open when the process died stays open, timed to
    # the last thing the log actually saw.
    assert "⏳ Company research" in text
    # Column padding is the renderer's business; the pairing is this test's.
    assert "✅ Match brief 2m 00s" in re.sub(r" +", " ", text)


@pytest.mark.parametrize("over", [
    {"progress_message_id": None},          # built before the column existed
    {"log_path": ""},                       # never got as far as spawning
    {"log_path": "no/such/build.log"},      # log rotated away
])
def test_a_checklist_that_cannot_be_rebuilt_is_left_alone(tmp_path, over):
    notifier = _Notifier()
    _recover(notifier, _stale_row(tmp_path, **over))
    assert notifier.edits == []


def test_a_broken_replay_does_not_hold_up_boot(tmp_path):
    """Recovery runs before the watcher will build anything. It cannot throw."""
    notifier = _Notifier(fail=True)
    _recover(notifier, _stale_row(tmp_path))  # must not raise
    assert notifier.edits == []


# --------------------------------------------------------------------------
# the replay CLI
# --------------------------------------------------------------------------

def test_replay_prints_on_a_cp1252_console(tmp_path, capsys, monkeypatch):
    """The one command for inspecting a past build must survive Windows.

    The checklist is drawn with ✅ and ⏳, and the console here defaults to
    cp1252 — so `--replay` died on the first row it tried to print, on the
    machine the watcher actually runs on.
    """
    log = tmp_path / "20260805-120000-acme-data-scientist.log"
    log.write_text(
        "$ claude -p\ncwd: .\n\n---\n"
        + "\n".join(json.dumps(e) for e in [
            _agent("00-posting-archiver", "t1", 0),
            _result("t1", 30),
        ]) + "\n",
        encoding="utf-8")

    # What `force_utf8` exists to undo: an encoder that cannot spell ✅.
    monkeypatch.setattr(
        sys, "stdout",
        io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict"))

    assert progress.main(["--replay", str(log)]) == 0
    sys.stdout.flush()
    written = sys.stdout.buffer.getvalue().decode("utf-8")
    assert "Archive posting" in written
    assert "✅" in written, "the mark cp1252 could not spell came through"
