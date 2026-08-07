"""Reading a build's own stdout to say which pipeline step it is on.

A headless build takes 25-45 minutes and, until this existed, said almost
nothing while it ran: one "Building …" line, then silence until the PDFs landed
on disk. A build that was working and a build that was wedged looked identical
for half an hour, and the only way to learn where the time went was to read the
NDJSON log by hand afterwards.

Nothing new has to be reported for this to work. `claude -p` is already spawned
with `--output-format stream-json --verbose`, and the orchestrator announces
every pipeline step it starts in that stream:

* a step is an `Agent` tool_use whose `input.subagent_type` is the agent name
  from `.claude/agents/` — `01-experience-matcher` and friends;
* the deterministic steps have no subagent, so they are recognised by the script
  named in a `Bash` command;
* the step ends at the `tool_result` block carrying the same `tool_use_id`.

**`parent_tool_use_id` is what makes this readable rather than a firehose.** It
is null for the orchestrator's own calls and set for everything a subagent does
inside its own turn. In the build this was written against that is 5 events
against 71: without the filter the checklist would be every `Read` and `Grep`
of every agent, and with it, it is exactly the pipeline.

An agent finishes in one of two ways, and the difference is not cosmetic. When
the orchestrator launches it in the background — which is what the parallel
phases in CLAUDE.md produce — the `tool_result` comes back in *milliseconds*
saying "Async agent launched successfully", and the real completion arrives much
later as a `system` / `task_notification` event carrying the same `tool_use_id`.
Closing the row on that acknowledgement is the obvious mistake and it is a
convincing one: every parallel step reports `0m 00s` and the checklist races to
the end while the build is still on step 01. So the launch ack is recognised and
ignored, and both endings are accepted.

Those `system` events carry no timestamp, so their clock comes from
`usage.duration_ms` in the `task_progress` stream — the agent's own measured
runtime — falling back to the last timestamp seen. That is what makes `--replay`
report real historical durations rather than how fast the file was read.

The tracker is deliberately passive. It parses, it never sends, and it holds no
Telegram state — `builder.py` owns the message and decides when an edit is worth
making. That split is what lets `--replay` render a finished build's checklist
from its log file, which is how the layout gets checked without waiting out a
40-minute run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .logsetup import force_utf8

# States a row moves through. `pending` rows are the ones still to come, and
# showing them is the point: the reader can see how much is left, not only what
# has happened.
#
# `skipped` is the one that is not about this run's own progress. A step the
# flow went past without the tracker ever seeing it would otherwise stay
# `pending` for the rest of the build, and a blank row above rows that are
# ticking along reads as "still stuck on step 1" when the truth is "that step is
# not coming". It happens for two ordinary reasons: a resumed run did its early
# steps in the session before this one, and an orchestrator may do a small step
# itself rather than dispatch the agent this watches for. Marked, the row is
# information; blank, it is a wrong answer.
PENDING, RUNNING, DONE, FAILED, SKIPPED = (
    "pending", "running", "done", "failed", "skipped")

_MARK = {PENDING: "⬜", RUNNING: "⏳", DONE: "✅", FAILED: "❌", SKIPPED: "➖"}


@dataclass(frozen=True)
class Step:
    """One row of the checklist.

    `optional` rows are declared in their proper place but stay hidden until the
    run actually reaches them, so an English-only build never shows a German row
    it will never fill in, and a run whose prep step is skipped does not look
    unfinished forever.

    `phase` is the concurrency group from CLAUDE.md, and it is what makes a
    skipped row detectable: only a step in a *later* phase proves the flow has
    left an earlier one behind. Position in this list cannot carry that, because
    much of the pipeline is deliberately parallel — the CV and the cover letter
    are drafted at once, so whichever started first would mark the other skipped
    a second before it began.
    """

    key: str
    label: str
    agent: str = ""          # subagent_type that starts it, if it is a model step
    script: str = ""         # script named in a Bash command, if it is a script step
    flag: str = ""           # substring that must be present for this variant
    anti_flag: str = ""      # substring that must be absent for this variant
    optional: bool = False
    phase: int = 0           # concurrency group: same number means "at the same time"


# The pipeline in CLAUDE.md order. The thirteen non-optional rows are the
# English-only run; everything else appears when it happens.
#
# The phase numbers are CLAUDE.md's own parallel phases, and they are
# deliberately coarse — a wrong number here costs a row briefly marked skipped
# just before it starts, so where the ordering is a matter of the orchestrator's
# discretion the steps share a number rather than guess. Phase 5 is the whole of
# CLAUDE.md's Phase C for that reason: the documents track and the prep track are
# started in the same message and interleave freely, so the prep agent beginning
# says nothing about whether the renderer has.
STEPS: tuple[Step, ...] = (
    Step("archive",       "Archive posting",  agent="00-posting-archiver", phase=0),
    Step("match",         "Match brief",      agent="01-experience-matcher", phase=1),
    Step("research",      "Company research", agent="02-company-role-researcher", phase=1),
    Step("cv_draft",      "CV draft",         agent="03-cv-writer-en", phase=2),
    Step("letter_draft",  "Cover letter",     agent="05-cover-letter-writer-en", phase=2),
    Step("cv_verify",     "Verify CV",        agent="04-cv-verifier-en", phase=3),
    Step("letter_verify", "Verify letter",    agent="06-cover-letter-verifier-en", phase=3),
    Step("german",        "German versions",  agent="07-translator-de", optional=True,
         phase=4),
    # The two renderer calls and the two QA calls are told apart by their own
    # flags. The application pass must not match the prep pass, or a run would
    # look like it had rendered its documents twice and never made the study aid.
    Step("render_docs",   "Render documents", script="render_latex_application.py",
         anti_flag="interview_prep_payload", phase=5),
    Step("qa_docs",       "QA documents",     script="qa_application.py",
         anti_flag="--require-prep", phase=5),
    Step("prep",          "Interview prep",   agent="08-interview-prep", phase=5),
    Step("render_prep",   "Render prep",      script="render_latex_application.py",
         flag="interview_prep_payload", optional=True, phase=5),
    Step("qa_prep",       "QA prep",          script="qa_application.py",
         flag="--require-prep", optional=True, phase=5),
    Step("proofread",     "Proofread",        agent="09-proofreader", phase=6),
    Step("clean",         "Clean folder",     script="clean_deliverable.py",
         optional=True, phase=7),
    Step("final_qa",      "Final QA",         agent="10-final-verifier", phase=8),
    Step("tracker",       "Tracker",          script="append_tracker_entry.py", phase=9),
)

@dataclass
class Row:
    step: Step
    state: str = PENDING
    started: float | None = None
    finished: float | None = None
    seen: bool = False

    @property
    def visible(self) -> bool:
        return self.seen or not self.step.optional

    def elapsed(self, now: float) -> float | None:
        if self.started is None:
            return None
        return (self.finished if self.finished is not None else now) - self.started


def format_duration(seconds: float) -> str:
    """`0m 26s`, `12m 04s`, `1h 04m`.

    Seconds are kept below the hour because the interesting differences between
    pipeline steps are in seconds, and dropped above it because at that point
    the interesting difference is that something is very wrong.
    """
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def event_time(event: dict[str, Any]) -> float | None:
    """Epoch seconds from an event's own `timestamp`, or None if it carries none.

    `assistant` and `user` events are stamped; `system` events are not, which is
    why the tracker keeps a clock of its own rather than asking here.
    """
    raw = str(event.get("timestamp") or "")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# The text a background launch comes back with. It is an acknowledgement, not a
# result: the agent has not started thinking yet.
_LAUNCH_ACK = "Async agent launched"


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return ""


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _events(path: Path) -> Iterable[dict[str, Any]]:
    """Decoded events from a build log on disk.

    The log is the same NDJSON the live tracker sees, with the command line and
    the prompt written above it, so non-JSON lines are skipped rather than
    treated as corruption.
    """
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


class Tracker:
    """Turns a build's event stream into a checklist.

    Feed it every decoded NDJSON event; ask it to render whenever you are ready
    to spend a Telegram edit. It never sends anything itself.
    """

    def __init__(self, now: Callable[[], float] = time.time, live: bool = True) -> None:
        self._now = now
        # Live, the wall clock is the right answer for an unstamped event, since
        # we are reading it as it arrives. Replaying a file it is badly wrong —
        # it would date a build from last week to this second — so replay falls
        # back to the last timestamp the stream itself carried.
        self.live = live
        self.reset()

    # -- ingest ------------------------------------------------------------

    def reset(self) -> None:
        """Start the checklist over, keeping nothing from a previous attempt.

        A retry re-runs the pipeline from step 00, so carrying durations across
        would show the first attempt's timings against the second attempt's
        progress.
        """
        self.rows: dict[str, Row] = {step.key: Row(step) for step in STEPS}
        self._open: dict[str, str] = {}      # tool_use_id -> row key, still running
        self._closed: dict[str, str] = {}    # tool_use_id -> row key, finished
        self._spent: dict[str, float] = {}   # task_id -> seconds it has run for
        self.clock: float = 0.0
        self.started_at: float | None = None
        self.last_event: float | None = None

    def feed(self, event: dict[str, Any]) -> bool:
        """Absorb one event. Returns whether the rendered checklist would change.

        Only the orchestrator's own tool calls count. `parent_tool_use_id` is set
        on everything a subagent does inside its own turn, and those are the
        overwhelming majority of events in any build — with one exception, which
        `_survives_nesting` explains.
        """
        if not isinstance(event, dict):
            return False

        kind = event.get("type")
        if kind == "system":
            return self._system(event)
        if kind not in ("assistant", "user"):
            return False

        blocks = [block for block
                  in (event.get("message") or {}).get("content") or []
                  if isinstance(block, dict)]
        if event.get("parent_tool_use_id") is not None:
            blocks = [block for block in blocks if self._survives_nesting(block)]
            if not blocks:
                return False

        stamp = self._stamp(event)
        self.last_event = stamp
        if self.started_at is None:
            self.started_at = stamp

        changed = False
        for block in blocks:
            if block.get("type") == "tool_use":
                changed |= self._start(block, stamp)
            elif block.get("type") == "tool_result":
                changed |= self._finish(block, stamp)
        return changed

    def _survives_nesting(self, block: dict[str, Any]) -> bool:
        """Whether a block that carries a parent id is still a pipeline step.

        Normally the parent id is the whole filter and nothing survives: it is
        set on everything a subagent does inside its own turn, which outnumbers
        the orchestrator's own calls by roughly fourteen to one.

        It stops being reliable across a resume. When a build stops to ask while
        a background agent is still open, the continuation stamps the
        orchestrator's next calls with *that* agent's id. The Roche run dispatched
        its matcher and researcher exactly as it should have, both ran to
        completion in 2m 09s and 4m 20s, and both arrived looking like work the
        archiver had done inside its own turn — so both were dropped, and the
        rows they should have filled were indistinguishable from steps that never
        happened at all.

        A pipeline agent is safe to take either way, because only the
        orchestrator dispatches the numbered agents: nested, it is a
        misattribution rather than a subagent's own business. A script is not
        safe — subagents run the renderer and the QA pass for their own reasons,
        and taking those is precisely the firehose the parent id keeps out. A
        result is taken only for a row already open, which is the same rule said
        in the other direction.
        """
        if block.get("type") == "tool_result":
            use_id = str(block.get("tool_use_id") or "")
            return use_id in self._open or use_id in self._closed
        return block.get("name") == "Agent" and self._match(block) is not None

    def _stamp(self, event: dict[str, Any]) -> float:
        """The moment an event happened, advancing the tracker's clock."""
        value = event_time(event)
        if value is not None:
            self.clock = value
            return value
        if self.live or not self.clock:
            return self._now()
        return self.clock

    def _system(self, event: dict[str, Any]) -> bool:
        """`task_progress` and `task_notification` — how a background step ends.

        These are the only events that say a backgrounded agent has actually
        finished; its `tool_result` came back the instant it was launched.
        """
        subtype = event.get("subtype")
        task_id = str(event.get("task_id") or "")

        if subtype in ("task_progress", "task_notification"):
            # The notification carries a final figure of its own, and it is the
            # only one that covers the stretch between the last progress ping and
            # the agent actually stopping. Reading the pings alone reported the
            # Roche researcher at 3m 28s for 4m 20s of work — every step
            # undercounted by however long it was quiet before it finished.
            spent = (event.get("usage") or {}).get("duration_ms")
            if task_id and isinstance(spent, (int, float)):
                self._spent[task_id] = max(self._spent.get(task_id, 0.0), float(spent) / 1000)
        if subtype != "task_notification":
            return False
        use_id = str(event.get("tool_use_id") or "")
        key = self._open.pop(use_id, None)
        if key is None:
            return False
        # Kept so a later, properly timestamped `tool_result` for the same call
        # can correct the finish time this event can only guess at.
        self._closed[use_id] = key

        row = self.rows[key]
        row.state = DONE if str(event.get("status") or "completed") == "completed" else FAILED
        stamp = self._stamp(event)
        # The agent's own reported runtime beats the surrounding stream, which
        # may have been silent the whole time the agent was thinking — the
        # orchestrator says nothing at all while it waits on a parallel phase.
        if row.started is not None and self._spent.get(task_id):
            stamp = max(stamp, row.started + self._spent[task_id])
        row.finished = stamp
        self.last_event = max(self.last_event or stamp, stamp)
        return True

    def _start(self, block: dict[str, Any], stamp: float) -> bool:
        key = self._match(block)
        if key is None:
            return False
        row = self.rows[key]
        row.seen = True
        row.state = RUNNING
        row.started = stamp
        row.finished = None
        use_id = block.get("id")
        if use_id:
            self._open[str(use_id)] = key
        self._mark_skips()
        return True

    def _mark_skips(self) -> None:
        """Mark what the flow has left behind, now that it has reached this step.

        Everything still untouched in an *earlier* phase than the furthest one
        seen is not waiting its turn — its turn has been and gone. Marking it is
        the whole difference between a checklist that says "three steps happened
        elsewhere and the fourth is running" and one that says nothing has
        started at all.

        Nothing here is final. A step that begins after it was written off simply
        starts, and `_start` puts it back to running with a fresh clock, which is
        why the phases can afford to be coarse.
        """
        reached = max((step.phase for step in STEPS if self.rows[step.key].seen),
                      default=-1)
        for step in STEPS:
            row = self.rows[step.key]
            if row.state == PENDING and step.phase < reached:
                row.state = SKIPPED

    def prime(self, path: Path) -> None:
        """Absorb an earlier session's log before this run's own events arrive.

        Answering a stop-and-ask resumes the same CLI session, but the tracker is
        new and sees only what happens after the resume. The steps the first
        session finished therefore sat blank for the rest of the build, under a
        header reading `0/13 steps` while the CV was being drafted.

        Feeding that session's log in first restores those rows with the
        durations it recorded. Whatever it left running is marked skipped rather
        than carried over: the second session either redoes it, which starts the
        row again, or it does not — and a row left open by a session that has
        ended would measure its age against this build's clock and report an hour
        that nobody spent.
        """
        live, self.live = self.live, False
        try:
            for event in _events(path):
                self.feed(event)
        finally:
            self.live = live

        for row in self.rows.values():
            if row.state == RUNNING:
                row.state = SKIPPED
                row.started = row.finished = None
        # Those tool_use ids belong to a session that has ended. Kept, a live
        # result could close a row against one of them.
        self._open.clear()
        self._closed.clear()
        self._spent.clear()
        # The clock restarts with the resumed run; only the rows carry over.
        self.clock = 0.0
        self.started_at = None
        self.last_event = None
        self._mark_skips()

    def _finish(self, block: dict[str, Any], stamp: float) -> bool:
        use_id = str(block.get("tool_use_id") or "")
        key = self._open.get(use_id)

        if key is None:
            # A backgrounded script is announced finished by an unstamped
            # `task_notification` first and only then hands back its result. The
            # result knows what time it is; the notification does not, so let it
            # correct the row rather than dropping it as a duplicate.
            key = self._closed.get(use_id)
            row = self.rows[key] if key else None
            if row is None or row.finished is None or stamp <= row.finished:
                return False
            row.finished = stamp
            return True

        # A background launch acknowledges itself in milliseconds. Treating that
        # as the result marks every parallel step done the moment it starts.
        if _LAUNCH_ACK in _result_text(block):
            return False

        del self._open[use_id]
        self._closed[use_id] = key
        row = self.rows[key]
        row.state = FAILED if block.get("is_error") else DONE
        row.finished = stamp
        return True

    @staticmethod
    def _match(block: dict[str, Any]) -> str | None:
        """Which checklist row a tool_use block belongs to, if any.

        Unrecognised work returns None rather than growing the list: the
        orchestrator also reads files and greps, and a checklist that reported
        those would be the firehose this exists to avoid.
        """
        name = block.get("name")
        inp = block.get("input") or {}
        if not isinstance(inp, dict):
            return None

        if name == "Agent":
            agent = str(inp.get("subagent_type") or "")
            for step in STEPS:
                if step.agent and step.agent == agent:
                    return step.key
            return None

        if name in ("Bash", "PowerShell"):
            command = str(inp.get("command") or "")
            for step in STEPS:
                if not step.script or step.script not in command:
                    continue
                if step.flag and step.flag not in command:
                    continue
                if step.anti_flag and step.anti_flag in command:
                    continue
                return step.key
        return None

    # -- render ------------------------------------------------------------

    @property
    def visible_rows(self) -> list[Row]:
        return [self.rows[step.key] for step in STEPS if self.rows[step.key].visible]

    def counts(self) -> tuple[int, int]:
        """Rows that will not change again, over rows shown.

        Skipped counts as settled. It is nothing to be proud of, but leaving it
        out pinned the header at `0/13 steps` on a resumed build that was four
        steps in, and a counter that cannot move is worse than one that credits a
        step the run was right to pass over.
        """
        rows = self.visible_rows
        settled = (DONE, FAILED, SKIPPED)
        return sum(1 for r in rows if r.state in settled), len(rows)

    def total_elapsed(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, now - self.started_at)

    def render(self, title: str, status: str, now: float) -> str:
        """The whole message: an HTML header over a monospace checklist.

        The body is `<pre>` because Telegram renders message text in a
        proportional font, where padded columns do not line up at all. Inside a
        pre block they do, which is the difference between a table and a list of
        ragged fragments on a phone.
        """
        rows = self.visible_rows
        width = max((len(r.step.label) for r in rows), default=0)

        lines = []
        for row in rows:
            spent = row.elapsed(now)
            stamp = format_duration(spent) if spent is not None else ""
            lines.append(f"{_MARK[row.state]} {row.step.label.ljust(width)}  {stamp}".rstrip())

        body = _escape("\n".join(lines))
        return f"{title}\n{status}\n\n<pre>{body}</pre>"


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------

def replay(path: Path, now: Callable[[], float] = time.time) -> Tracker:
    """Rebuild the checklist of a build that has already run, from its log."""
    tracker = Tracker(now=now, live=False)
    for event in _events(path):
        tracker.feed(event)
    return tracker


def _iter_logs(patterns: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.exists():
            found.append(candidate)
            continue
        found.extend(sorted(Path(candidate.parent or ".").glob(candidate.name)))
    return found


def main(argv: list[str] | None = None) -> int:
    # The checklist is drawn with ✅ and ⏳, and the Windows console defaults to
    # cp1252 — which turns the one command for inspecting a past build into a
    # UnicodeEncodeError on the first row it prints.
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay", nargs="+", metavar="LOG", required=True,
                        help="build log(s) under logs/builds/ — globs allowed")
    args = parser.parse_args(argv)

    logs = _iter_logs(args.replay)
    if not logs:
        print("no matching build log")
        return 1

    for path in logs:
        tracker = replay(path)
        now = tracker.last_event or time.time()
        done, total = tracker.counts()
        print(f"\n=== {path.name}")
        print(f"{done}/{total} steps · {format_duration(tracker.total_elapsed(now))}\n")
        for row in tracker.visible_rows:
            spent = row.elapsed(now)
            stamp = format_duration(spent) if spent is not None else ""
            label = row.step.label.ljust(
                max(len(r.step.label) for r in tracker.visible_rows))
            print(f"  {_MARK[row.state]} {label}  {stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
