"""Run the existing CV pipeline headlessly, one approved posting at a time.

The watcher deliberately owns none of the application logic. When a posting is
approved by Telegram reply, this spawns `claude -p` in the workspace with a
prompt that is exactly what the interactive trigger receives — a URL plus any
free-form note — so `CLAUDE.md`'s eleven steps, the subagents, the model
assignment, and the tracker row all fire unchanged. Nothing here knows what a
cover letter is, and it should stay that way.

Three things it does own:

  duplicates    a role already applied to must not be rebuilt, but a folder
                left behind by an abandoned run must not block one either
  containment   cwd pinned to the workspace, guard hook armed, full NDJSON log
  reporting     what came out is read off the filesystem and the tracker, never
                parsed from the model's own account of itself
  cleanup       a build that failed or came out short leaves nothing behind —
                folder, tracker row, and scratch all go

Concurrency is 1. `CLAUDE.md` requires PDF rendering to stay sequential, and
two pipelines writing LaTeX build artifacts at once is exactly the collision it
warns about.

    python -m watcher.builder --posting <id> --dry-run
    python -m watcher.builder --company Deluxe --title "AI Engineer" --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from . import dedupe, progress, store
from .claude_cli import ClaudeError, resolve_bin
from .config import (BUILD_LOG_DIR, BUILD_SETTINGS_PATH, REPO_ROOT, Config,
                     ensure_dirs, load_config, load_identity,
                     sync_build_settings, to_absolute)
from .notifier import TELEGRAM_MAX_CHARS

# Imported after .config, which is what puts scripts/ on sys.path.
import no_console  # noqa: E402
from .logsetup import force_utf8

log = logging.getLogger("watcher.build")

# Statuses written to the `builds` table.
DONE = "done"
DUPLICATE = "duplicate"
FAILED = "failed"
INCOMPLETE = "incomplete"
INTERRUPTED = "interrupted"
# The run ended cleanly without producing documents. Almost always `CLAUDE.md`'s
# stop-and-ask: an unsupported claim, a skills gap, an ambiguous company name.
# That is a question, not a failure, and reporting it as one lost the question —
# one build stopped to ask whether to claim agent-framework experience the
# canonical profile does not carry, and all the user was told is that two PDFs
# were missing.
NEEDS_DECISION = "needs_decision"
# Stopped by the user with `/cancel`, not by anything that went wrong.
CANCELLED = "cancelled"

#: How many turn-ending messages are kept as a build's closing words. The
#: orchestrator ends a turn every time it hands off to background subagents, so
#: a run emits one of these per handoff and the last is rarely the interesting
#: one: the build above asked its question a turn before the end, and signed off
#: with "still holding on your decision from above".
CLOSING_TURNS = 3

#: Ceiling on those closing words, leaving room for the heading and the folder
#: line inside `TELEGRAM_MAX_CHARS`.
CLOSING_CHARS = 2000

# Per-line ceiling for the CLI's NDJSON stream, 64 MB. asyncio defaults to
# 64 KiB, which a single tool result blows past routinely — an image Read comes
# back base64-encoded on one line.
STREAM_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class Job:
    posting_id: str
    note: str
    reply_message_id: int | None
    build_id: int
    # Set when this job continues a run that stopped to ask. `answer` is the
    # user's reply, verbatim; `resume_session` is the CLI session it belongs to,
    # empty when that session is gone and the answer has to start a fresh build.
    # `resume_log` is that session's own log, which is where the steps it already
    # finished are recorded — without it the continuation's checklist starts
    # blank and stays blank for everything the first half did.
    answer: str = ""
    resume_session: str = ""
    resume_log: str = ""
    # Set by an explicit "yes anyway" to a duplicate decline. The check is not
    # re-run — the user has already seen its verdict and overruled it.
    override_duplicate: bool = False

    @property
    def is_answer(self) -> bool:
        return bool(self.answer)


@dataclass
class RunResult:
    """What one call to the CLI came back with.

    A tuple until it needed a fourth and fifth field. `session_id` is the one
    that earns the class: it is read off the event stream for every run and
    matters only for the runs that stop without finishing, which is exactly
    when a positional tuple would have been extended and mis-unpacked.
    """

    ok: bool = False
    detail: str = ""
    closing: str = ""
    session_id: str = ""
    log_file: Path | None = None


@dataclass
class Outcome:
    status: str
    folder: str = ""
    detail: str = ""
    documents: tuple[str, ...] = ()
    tracker_row: bool = False
    cleaned: str = ""
    # Whether the "application ready" message already went out for this build.
    # A later failure has to admit it is retracting something the user was told
    # was finished, rather than silently contradicting it.
    announced: bool = False
    # Set when the run itself failed but the required documents were already on
    # disk: the application survives, and this is what went wrong around it.
    salvaged: str = ""


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

def build_prompt(url: str, note: str) -> str:
    """What the interactive trigger would have received, and nothing more.

    Resist the urge to add instructions here. Every sentence of scaffolding is
    a second source of truth competing with CLAUDE.md, and the moment the two
    disagree the pipeline behaves differently depending on who started it.
    """
    return f"{url}\n{note}".strip()


def answer_prompt(answer: str) -> str:
    """The user's reply to a question the run stopped to ask, and nothing more.

    Separate from `build_prompt`, and deliberately just as bare. This message
    continues an existing session: the run already holds `CLAUDE.md`, the Match
    Brief, the Research Note and the folder it created, so anything added here
    would be a second copy of context the model already has — and the first one
    to go stale.
    """
    return (answer or "").strip()


# --------------------------------------------------------------------------
# spawning
# --------------------------------------------------------------------------

def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return (cleaned[:limit] or "build").lower()


def log_path_for(company: str, title: str, attempt: int = 1,
                 suffix: str = "") -> Path:
    """Where one build attempt writes its NDJSON transcript.

    The attempt suffix is not cosmetic: the timestamp only resolves to the
    second, and the log is opened for writing, so two attempts that started
    close together would otherwise have the second overwrite the first — losing
    exactly the transcript of the failure that caused the retry.

    `suffix` marks a run that is not a plain first attempt — currently only
    `answer`, for a session resumed with a reply to its own question.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    tail = "" if attempt <= 1 else f"-retry{attempt - 1}"
    if suffix:
        tail = f"-{_slug(suffix, 12)}{tail}"
    return BUILD_LOG_DIR / f"{stamp}-{_slug(company, 24)}-{_slug(title)}{tail}.log"


def command_for(config: Config, resume: str = "") -> list[str]:
    # Re-render the settings file from its template first. It is generated, and
    # its content depends on where this clone sits, so this is what makes an
    # edited template — or a workspace that has been moved since the file was
    # written — apply to the next build rather than the next restart. It also
    # refuses outright if the deny rules would lock the build out of the
    # workspace, which is a far better failure than a 45-minute timeout spent
    # being denied one tool call at a time.
    try:
        status = sync_build_settings()
        if status != "up to date":
            log.info("build_settings.json %s", status)
    except FileNotFoundError:
        pass  # no template in this clone; the exists() check below still applies
    except Exception as exc:  # noqa: BLE001 — surfaced to Telegram as-is
        raise ClaudeError(f"build settings are unusable: {exc}") from exc

    cmd = [
        resolve_bin(config.claude_bin), "-p",
        "--model", config.build_model,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json", "--verbose",
    ]
    if resume:
        # Continue the run that stopped to ask instead of starting a new one.
        # The session already holds CLAUDE.md, the Match Brief and the folder,
        # which is the whole reason an answer is cheap to deliver this way.
        cmd += ["--resume", resume]
    if BUILD_SETTINGS_PATH.exists():
        cmd += ["--settings", str(BUILD_SETTINGS_PATH)]
    else:
        # Refusing to run unguarded is the only safe reading of a missing
        # settings file — bypassPermissions with no deny list and no hook is
        # strictly more dangerous than not building at all.
        raise ClaudeError(
            f"{BUILD_SETTINGS_PATH} is missing; refusing to run an unguarded build"
        )
    return cmd


def _kill_tree(pid: int) -> None:
    """Kill the CLI and everything it started.

    A bare terminate() leaves the node process and any latexmk children alive
    on Windows, which then hold the deliverable folder open and make the next
    attempt fail for an unrelated reason.
    """
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30, **no_console.kwargs())
    except Exception:
        log.exception("could not kill build process tree %s", pid)


def _tidy(text: object) -> str:
    """Squeeze a model message down without flattening it into one line.

    Runs of spaces and blank lines go; the paragraph breaks stay, because a
    stop-and-ask is a question with options under it and reads as noise once
    those are gone.
    """
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip()


def _closing_words(turns: list[str]) -> str:
    """The last few things a build said, for a run that has to explain itself."""
    text = "\n\n".join(turns[-CLOSING_TURNS:])
    if len(text) <= CLOSING_CHARS:
        return text
    return text[:CLOSING_CHARS - 1].rstrip() + "…"


def _result_detail(event: dict, limit: int = 300) -> str:
    """Why a failing result event failed, in a form worth putting in Telegram.

    Not `subtype`. That field describes how the turn *ended* — it reads
    "success" even for a turn that returned nothing but an API error, which is
    how two builds came to be recorded as failed with the detail "success" and
    no trace of the 503 that actually killed them. The message is in `result`.

    A build also emits more than one of these: the orchestrator ends a turn
    whenever it hands off to background subagents, so a long run produces a
    result event per turn and the last one is the run's real outcome.
    """
    text = " ".join(_tidy(event.get("result")).split())
    if not text:
        # Nothing usable in `result` — subtype is a poor answer but it beats an
        # empty failure, and "error_during_execution" and friends do say
        # something.
        return str(event.get("subtype") or "the CLI reported an error")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# Failures that say "the far end was briefly unwell", as opposed to "this build
# is wrong". Deliberately a list of upstream symptoms rather than a catch-all:
# retrying a build that timed out, crashed, or wrote the wrong files just spends
# another 45 minutes arriving at the same answer.
#
# Note what is *not* here. `timed out after 45 min` is the watcher's own ceiling,
# not the API's — "gateway time-out" is spelled out separately so the two cannot
# be confused. 401/403 are omitted on purpose: bad credentials do not heal.
#
# `server error` is deliberately bare rather than `internal server error`. The
# CLI reports a dropped response as `API Error: Server error mid-response`, with
# no status code and no "internal", so the narrower pattern classified the most
# common upstream failure there is as permanent and skipped the retry it had
# been granted. Matching the two words costs at most one extra attempt on a
# build that used them in some other sense; missing them cost a whole run.
_TRANSIENT = re.compile(
    r"\b(?:429|500|502|503|504)\b"
    r"|overloaded"
    r"|rate.?limit"
    r"|temporarily unavailable"
    r"|service unavailable"
    r"|server error"
    r"|bad gateway"
    r"|gateway time-?out"
    r"|upstream"
    r"|connection (?:error|reset|refused|closed)"
    r"|econnreset|etimedout|enotfound"
    r"|fetch failed",
    re.IGNORECASE,
)


def is_transient(detail: str) -> bool:
    """Whether a failure detail is worth a second attempt.

    Reads the message the CLI itself reported (see `_result_detail`), which is
    why that function had to stop reporting the word "success": a build killed by
    `API Error: 503 All accounts are temporarily unavailable` is indistinguishable
    from a build that produced garbage if the reason never reaches this far.
    """
    return bool(detail) and bool(_TRANSIENT.search(detail))


async def _spawn(prompt: str, config: Config, log_file: Path,
                 on_event: Callable[[dict], None] | None = None,
                 resume: str = "",
                 on_session: Callable[[str], None] | None = None,
                 ) -> RunResult:
    """Run one build, or continue one with `resume`.

    `RunResult.closing` is the last few things the run said before it stopped,
    and is only of interest when it stopped without writing an application — see
    `NEEDS_DECISION`. It is read from the same result events as `detail`, so it
    costs nothing to collect and is discarded on every ordinary run.

    `RunResult.session_id` is what makes a stop-and-ask answerable: the CLI puts
    it on the `system`/`init` event and repeats it on every `result`, and passing
    it back as `--resume` continues this exact run rather than starting a second
    one that knows nothing about the folder this one created.

    The NDJSON stream is written through to the log line by line rather than
    buffered, so a hung build can be inspected while it is still hung.

    `on_event` sees every decoded event and is how the live progress checklist
    is fed. It must be cheap and synchronous — this runs inside the loop draining
    the CLI's stdout, and the comment further down spells out what happens to a
    build whose stdout stops being read. Anything it raises is logged and
    swallowed: a defect in progress reporting must not cost anyone an
    application.

    `on_session` fires once, as soon as the id is known, so a build that is later
    killed or times out is still resumable — waiting for the return value would
    mean losing the id in exactly the cases where it is worth most.
    """
    cmd = command_for(config, resume=resume)
    # no_console: the watcher is detached under pythonw and has no console, so
    # without this the CLI — a .CMD, hence cmd.exe — opens a window and keeps it
    # in front of whatever you are doing for the whole build.
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        limit=STREAM_LIMIT,
        **no_console.kwargs(),
    )

    detail = ""
    ok = False
    session = ""
    turns: list[str] = []

    async def pump() -> None:
        nonlocal detail, ok, session
        assert process.stdout is not None
        with log_file.open("w", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(cmd)}\ncwd: {REPO_ROOT}\n\n{prompt}\n\n---\n")
            handle.flush()
            async for raw in _lines(process.stdout, handle):
                line = raw.decode("utf-8", errors="replace")
                handle.write(line)
                handle.flush()
                event = _parse_event(line)
                if not event:
                    continue
                found = str(event.get("session_id") or "")
                if found and found != session:
                    # Last one wins: a resumed run reports the id it is
                    # continuing under, which is the one to store for next time.
                    session = found
                    if on_session is not None:
                        try:
                            on_session(session)
                        except Exception:
                            log.exception("session hook failed; build continues")
                if event.get("type") == "result":
                    ok = not event.get("is_error", False)
                    detail = "" if ok else _result_detail(event)
                    said = _tidy(event.get("result"))
                    if said and (not turns or turns[-1] != said):
                        turns.append(said)
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        log.exception("progress hook failed; build continues")

    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    timeout = config.build_timeout_minutes * 60
    try:
        await asyncio.wait_for(asyncio.gather(pump(), process.wait()), timeout)
    except asyncio.TimeoutError:
        _kill_tree(process.pid)
        return RunResult(
            ok=False,
            detail=f"timed out after {config.build_timeout_minutes} min",
            closing=_closing_words(turns),
            session_id=session,
            log_file=log_file,
        )
    except (Exception, asyncio.CancelledError):
        # Anything that stops us reading stdout strands the CLI: it blocks on a
        # pipe nobody drains and sits there until the machine reboots. Kill the
        # tree before the exception continues on to the worker.
        _kill_tree(process.pid)
        raise

    # Append rather than fill only-if-empty: a non-zero exit after a result
    # event that already explained itself is worth recording alongside the
    # explanation, not instead of it and not silently dropped.
    if process.returncode != 0:
        note = f"exit {process.returncode}"
        detail = f"{detail} ({note})" if detail else note
    return RunResult(
        ok=ok and process.returncode == 0,
        detail=detail,
        closing=_closing_words(turns),
        session_id=session,
        log_file=log_file,
    )


# A `--resume` that names a session the CLI no longer has. Worth telling apart
# from every other failure: it is the one where retrying the same way is
# guaranteed to fail again, and starting fresh is guaranteed to work.
_NO_SESSION = re.compile(
    r"no conversation found|session .{0,40}not found|invalid session|"
    r"could not (?:find|resume) session|unknown session",
    re.IGNORECASE,
)


def session_is_gone(detail: str) -> bool:
    """Whether a failed resume failed because the session itself is missing."""
    return bool(detail) and bool(_NO_SESSION.search(detail))


async def _lines(stream: asyncio.StreamReader, handle) -> AsyncIterator[bytes]:
    """Iterate NDJSON lines without letting one huge line kill the build.

    The CLI puts whole tool results on a single line, and the proofreader reads
    rendered PDF pages — a 385 KB PNG arrives base64-encoded as one line, which
    is why `limit` is measured in megabytes rather than the 64 KiB default.

    The buffer alone is not enough, though. Whatever the ceiling, some day a
    line will exceed it, and `readline()` signals that by *raising* — which
    previously propagated out of the worker, marked a nearly-finished build
    failed, and left the CLI blocked on a pipe with no reader. So an oversized
    line is drained and skipped instead. A dropped log line costs a little
    fidelity in the transcript; dropping the build costs the whole run.
    """
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # readline() leaves the partial data in the buffer; read() empties
            # it so the CLI is not left blocked mid-write.
            try:
                await stream.read(STREAM_LIMIT)
            except Exception:
                pass
            note = f"[watcher: skipped an oversized output line — {exc}]\n"
            handle.write(note)
            handle.flush()
            log.warning("skipped an oversized stream line: %s", exc)
            continue
        if not raw:
            return
        yield raw


def _parse_event(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------------------
# before and after
# --------------------------------------------------------------------------

def check_duplicate(company: str, title: str, config: Config
                    ) -> tuple[dedupe.ExistingApplication | None, str]:
    """(blocking application, note about a partial one).

    A complete prior application blocks the build. An incomplete folder does
    not — it is reported and then rebuilt over, because otherwise one abandoned
    run locks that role out permanently.
    """
    hits = dedupe.collect_existing(
        company, title,
        lookback_days=config.duplicate_lookback_days,
        ratio=config.duplicate_title_ratio,
    )
    partial = ""
    for hit in hits:
        if dedupe.is_complete(hit):
            return hit, partial
        if not partial and hit.folder:
            missing = ", ".join(dedupe.missing_documents(hit.folder))
            partial = (f"Previous attempt at {hit.folder} looks incomplete "
                       f"(no {missing}) — rebuilding.")
    return None, partial


def locate_output(company: str, title: str, config: Config) -> Outcome:
    """What the pipeline actually produced, read off disk — not from the model.

    A build that reports success while writing nothing is a real failure mode,
    and the model is the last thing that should be trusted to notice it.
    """
    hits = dedupe.collect_existing(
        company, title, lookback_days=2,
        ratio=min(config.duplicate_title_ratio, 0.6),
    )
    folder_hit = next((h for h in hits if h.folder and Path(h.folder).is_dir()), None)
    tracker_row = any(h.origin == "tracker" for h in hits)

    if folder_hit is None:
        return Outcome(status=FAILED, tracker_row=tracker_row,
                       detail="the build reported success but no dated folder appeared")

    documents = tuple(dedupe.present_documents(folder_hit.folder))
    missing = dedupe.missing_documents(folder_hit.folder)
    status = INCOMPLETE if missing else DONE
    detail = f"missing {', '.join(missing)}" if missing else ""
    return Outcome(status=status, folder=folder_hit.folder, detail=detail,
                   documents=documents, tracker_row=tracker_row)


CLEANUP_SCRIPT = REPO_ROOT / "scripts" / "cleanup_application.py"

# How often to look for the finished CV and cover letter while a build runs.
# Short enough that the ready message is not itself a delay, long enough that a
# 40-minute build costs a couple of hundred cheap directory reads.
READY_POLL_SECONDS = 15


async def _settle(task: "asyncio.Task[bool]") -> bool:
    """Stop the ready-announcer and report whether its message got out.

    Never raises: the announcer is a courtesy on top of the build, and a fault
    in it must not change the build's own outcome.
    """
    if not task.done():
        task.cancel()
    try:
        return bool(await task)
    except asyncio.CancelledError:
        return False
    except Exception:  # noqa: BLE001
        log.exception("ready announcer failed")
        return False


def clean_up(folder: str, reason: str) -> str:
    """Erase what a failed run left behind. Returns a line for the reply.

    A half-built application is worse than none: the folder reads as "already
    applied" to anyone scanning the tree, and a tracker row written at step 10
    by a run that then fell over claims an application that was never sent.

    The deletion lives in `scripts/cleanup_application.py` rather than here, so
    the interactive pipeline (CLAUDE.md) and the watcher undo a bad run exactly
    the same way. This never raises: a cleanup that fails is worth reporting,
    not worth turning into a second failure on top of the first.
    """
    if not folder or not Path(folder).is_dir():
        # Nothing on disk means nothing to key a tracker row off either — step 10
        # only ever runs against a folder it just filled.
        return ""

    cmd = [sys.executable, str(CLEANUP_SCRIPT), "--folder", folder,
           "--reason", reason or "build failed"]
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=180, **no_console.kwargs())
    except Exception:
        log.exception("cleanup could not run for %s", folder)
        return "cleanup could not run — see watcher.log"

    report = (proc.stdout or "").strip()
    if proc.returncode != 0:
        log.error("cleanup failed for %s: %s", folder,
                  (proc.stderr or report or "").strip())
        return "cleanup failed — see watcher.log"

    log.info("cleanup: %s", report.replace("\n", " · "))
    if "Nothing to clean up" in report:
        return ""
    removed = [line.strip() for line in report.splitlines()[1:] if line.strip()]
    return f"cleaned up: {'; '.join(removed)}" if removed else "cleaned up"


def relative(folder: str) -> str:
    """`2026\\Deluxe\\2026-08-02 - AI Engineer` — the shape the user asked for."""
    try:
        return str(Path(folder).resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return folder


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fit(text: str, log_file: Path) -> str:
    """Add the log line and keep the whole message inside Telegram's ceiling.

    Only the stop-and-ask message can approach it — everything else here is a
    handful of lines. Cutting escaped HTML risks landing inside an entity, which
    Telegram rejects outright, so a trailing `&…` fragment is dropped with it.
    """
    tail = f"\n<i>log: {_escape(log_file.name)}</i>"
    room = TELEGRAM_MAX_CHARS - len(tail)
    if len(text) > room:
        text = text[:room - 1]
        head, sep, last = text.rpartition("&")
        if sep and ";" not in last:
            text = head
        text = text.rstrip() + "…"
    return text + tail


def duplicate_message(label: str, existing: dedupe.ExistingApplication) -> str:
    """A decline the user can overrule, not a verdict.

    The match is a similarity score, so it is sometimes wrong — and when it is,
    the cost is a missed application that nobody ever sees. Saying how to build
    it anyway turns that silent loss into one reply.
    """
    when = existing.applied_on.isoformat() if existing.applied_on else "an unknown date"
    where = relative(existing.folder) if existing.folder else "logged in the tracker"
    return (f"⚠️ <b>Duplicate</b> · {label}\n"
            f"Already applied {when} — {_escape(where)}\n"
            f"<i>Nothing was built. Reply \"yes anyway\" to build it regardless.</i>")


def _doc_label(name: str) -> str:
    """One document name for a report, with the owner's file prefix stripped.

    Stripping it is cosmetic: `<file_prefix> - CV.pdf` reads better as "CV" in a
    Telegram message that already says whose workspace this is. It is not
    worth an exception. `load_identity` raises when identity.toml is missing or
    still full of FILL IN, and this feeds the function that reports a build —
    including the build that failed *because* the workspace is incomplete. A
    report that cannot render is strictly worse than one that spells a filename
    out in full, and raising here replaces the real failure with this one.
    """
    try:
        prefix = f"{load_identity().file_prefix} - "
    except Exception:  # missing, unreadable, or still a placeholder
        prefix = ""
    return name.removeprefix(prefix).replace(".pdf", "")


def _doc_list(documents: tuple[str, ...]) -> str:
    return " · ".join(_doc_label(name) for name in documents)


#: Missing keywords and style tells named in full before the line turns into a
#: count. Three is what fits one phone line beside the percentages.
QA_LINE_ITEMS = 3


def read_qa_summary(folder: str) -> dict:
    """The two report-only halves of QA for a finished build, or `{}`.

    `qa_application` writes them to `_tmp/payloads/<Company> <date> <Role>/`,
    outside the deliverable folder — a stray `.json` inside it is an unexpected
    file, and unexpected files fail the folder inventory. That path is unchanged
    by step 09, which is what lets one lookup serve both the ready message, sent
    before the cleanup, and the run-complete message, sent after it.

    Everything here fails open, to `{}`. This sits on the announce path: a
    scorer that cannot parse its own output must never cost the user the message
    saying their application is ready. The imports are local for the same
    reason — both modules read `identity.toml` at import time, and a build that
    failed because the workspace is incomplete still has to be able to report
    that.
    """
    if not folder:
        return {}
    try:
        from clean_deliverable import payload_dir
        from qa_application import QA_SUMMARY_NAME

        root = to_absolute(folder)
        target = payload_dir(root)
        for path in ([target / QA_SUMMARY_NAME] if target else []) + \
                    [root / QA_SUMMARY_NAME]:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
    except Exception:  # unreadable, malformed, or the module would not import
        log.debug("no QA summary for %s", folder, exc_info=True)
    return {}


def _ats_line(summary: dict) -> str:
    """Keyword coverage of the built CV, as one line — or nothing at all.

    This is the number an ATS-shaped reader would arrive at, not the one a
    recruiter sees; no vendor publishes that formula. Saying `brief` and
    `posting` separately is what keeps it honest: the first is coverage of the
    keywords step 01A judged truthfully usable, the second a proxy over the raw
    ad, and they answer different questions.
    """
    ats = summary.get("ats")
    if not isinstance(ats, dict):
        return ""
    try:
        from ats_report import summary_line
    except Exception:
        return ""
    try:
        return summary_line(ats, max_missing=QA_LINE_ITEMS)
    except Exception:
        return ""


def _style_line(summary: dict) -> str:
    """The AI-tell count, named down to the first few.

    Report-only, exactly as in the QA report: rule 07 routes these to a human
    reader, and a finished application is not withdrawn over the word "robust".
    Naming them is the point — a bare count sends the user looking.
    """
    style = summary.get("style")
    if not isinstance(style, dict):
        return ""
    hits = [h for h in (style.get("hits") or []) if isinstance(h, dict)]
    metrics = [m for m in (style.get("metrics") or [])
               if isinstance(m, dict) and m.get("em_dash_overuse")]
    runs = [r for r in (style.get("bullet_runs") or []) if isinstance(r, dict)]
    total = len(hits) + len(metrics) + len(runs)
    if not total:
        return ""

    named = [f'"{h.get("match", "?")}" ({_doc_label(str(h.get("document", "")))})'
             for h in hits[:QA_LINE_ITEMS]]
    named += [f'em-dashes ({_doc_label(str(m.get("document", "")))})'
              for m in metrics[:max(0, QA_LINE_ITEMS - len(named))]]
    named += [f'repeated bullet opener "{r.get("word", "?")}" '
              f'({_doc_label(str(r.get("document", "")))})'
              for r in runs[:max(0, QA_LINE_ITEMS - len(named))]]
    line = f"⚠ {total} style tell{'' if total == 1 else 's'}: " + ", ".join(named)
    if total > len(named):
        line += f" +{total - len(named)} more"
    return line


def qa_lines(folder: str) -> list[str]:
    """The ATS and style lines for a build report, ready to append.

    Empty when there is no summary to read, which is what makes every message
    that uses it byte-identical to the one sent before any of this existed.
    """
    summary = read_qa_summary(folder)
    return [_escape(line) for line in (_ats_line(summary), _style_line(summary))
            if line]


def ready_message(label: str, outcome: Outcome) -> str:
    """Sent the moment the CV and cover letter exist, before the run ends.

    Interview Prep is a private study aid and renders after the documents an
    employer actually sees. Holding this message until the process exits made
    every run look as long as its slowest optional step.
    """
    where = _escape(relative(outcome.folder)) if outcome.folder else ""
    lines = [f"✅ <b>Application ready</b> · {label}",
             f"<code>{where}</code>",
             _doc_list(outcome.documents)]
    lines += qa_lines(outcome.folder)
    lines.append("<i>Interview prep is still rendering — it will land in the "
                 "same folder.</i>")
    return "\n".join(lines)


def result_message(label: str, outcome: Outcome, log_file: Path) -> str:
    where = _escape(relative(outcome.folder)) if outcome.folder else ""
    docs = _doc_list(outcome.documents)
    tracker = "tracker row ✓" if outcome.tracker_row else "no tracker row"

    if outcome.status == DONE:
        if outcome.announced:
            # The user already has the ready message with the folder path; this
            # only needs to close the loop on what arrived afterwards.
            head = f"📎 <b>Run complete</b> · {label}\n{docs} · {tracker}"
        else:
            head = (f"✅ <b>Built</b> · {label}\n<code>{where}</code>\n"
                    f"{docs} · {tracker}")
            # Only when the ready message never went out. Repeating the same
            # two lines a minute after the user has already read them is noise,
            # and neither number can have moved: both are measured off the CV,
            # which was final before that message was sent.
            for line in qa_lines(outcome.folder):
                head += f"\n{line}"
        if outcome.salvaged:
            # Everything an employer sees is finished and still on disk, so
            # this is not a failure — but it is not a clean run either, and the
            # missing piece is usually Interview Prep or the tracker row. Both
            # are named above, so say what broke and let the user read them.
            head += (f"\n⚠️ <i>The run itself did not finish cleanly: "
                     f"{_escape(outcome.salvaged)}. The documents above are "
                     f"complete and were kept.</i>")
        return head

    if outcome.status == NEEDS_DECISION:
        # The whole point of this branch is the quoted text, so it goes in
        # whole rather than as a one-line "detail" — it is a question, and half
        # a question is no use. `_spawn` has already capped it.
        lines = [f"⏸ <b>Stopped to ask</b> · {label}"]
        if outcome.detail:
            lines.append(_escape(outcome.detail))
        lines.append("<i>Nothing was drafted yet, and the folder is being kept "
                     "while this is open. Answer in this topic — as a reply or "
                     "a plain message — and the run picks up where it stopped. "
                     "Say \"no\" to drop it.</i>")
        return _fit("\n\n".join(lines), log_file)

    # Anything short of DONE has been cleaned up by now, so the folder path and
    # the document list describe something that no longer exists — say what was
    # missing and what was removed instead of pointing at a dead path.
    heading = ("⚠️ <b>Built with issues</b>" if outcome.status == INCOMPLETE
               else "❌ <b>Build failed</b>")
    lines = [f"{heading} · {label}"]
    if outcome.announced:
        lines.append("<b>This retracts the ready message above</b> — the run "
                     "fell over after the documents appeared.")
    if outcome.detail:
        lines.append(_escape(outcome.detail))
    lines.append(f"🧹 {_escape(outcome.cleaned)}" if outcome.cleaned
                 else "<i>Nothing was left behind.</i>")
    lines.append(f"<i>log: {_escape(log_file.name)}</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

class _ProgressReporter:
    """Keeps one Telegram message showing where a build has got to.

    The opener that used to say `🛠 Building …` and then nothing for forty
    minutes becomes this: the same message, edited in place as each pipeline step
    starts and finishes, with what every step cost. When the build ends it is
    edited once more and left alone, so the message stays as that build's timing
    record.

    Two rules shape the design, both learned the hard way elsewhere in this file:

    * **`feed` never awaits.** It is called from inside the loop draining the
      CLI's stdout, and anything that stalls that loop strands the CLI on a pipe
      nobody is reading. So `feed` updates the tracker, sets an event, and
      returns; every Telegram call happens on a separate task.
    * **Nothing here may end a build.** Every failure path returns rather than
      raises, and a message the user has deleted simply stops being edited.

    The refresh task wakes on whichever comes first: a step transition, or the
    tick that keeps the running step's clock moving. A floor between edits keeps
    a burst of transitions from turning into a burst of API calls.
    """

    def __init__(self, notifier, config: Config, job: Job, label: str,
                 message_id: int, footer: str = "") -> None:
        self._notifier = notifier
        self._config = config
        self._job = job
        self._label = label
        self._footer = footer
        self.message_id: int | None = message_id
        self.tracker = progress.Tracker()
        self.started = time.time()
        self.attempt = 1
        self.attempts = 1
        self._bump = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sent = ""
        self._last_edit = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._refresh())

    def begin_attempt(self, attempt: int, attempts: int) -> None:
        """A fresh attempt starts the checklist over.

        A retry re-runs the pipeline from step 00, so keeping the previous
        attempt's ticks would show a build further along than it is.
        """
        self.attempt, self.attempts = attempt, attempts
        if attempt > 1:
            self.tracker.reset()
            self.started = time.time()
        self._bump.set()

    def feed(self, event: dict) -> None:
        if self.tracker.feed(event):
            self._bump.set()

    def prime(self, path: Path) -> None:
        """Start the checklist from what an earlier session of this run did.

        Only a resumed build has one. A log that has been moved or deleted is not
        worth a word to the user and certainly not worth a failed build — the
        checklist simply starts where it would have anyway.
        """
        try:
            self.tracker.prime(path)
        except OSError as exc:
            log.info("no earlier checklist for %s: %s", path.name, exc)

    async def stop(self, status: str) -> None:
        """Stop refreshing and leave the final checklist in place."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._flush(status)

    # -- internals ---------------------------------------------------------

    async def _refresh(self) -> None:
        tick = max(1.0, float(self._config.build_progress_refresh_seconds))
        floor = max(0.0, float(self._config.build_progress_min_interval_seconds))
        while True:
            try:
                await asyncio.wait_for(self._bump.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
            self._bump.clear()
            waited = time.monotonic() - self._last_edit
            if waited < floor:
                await asyncio.sleep(floor - waited)
            await self._flush()

    async def _flush(self, status: str = "") -> None:
        if self.message_id is None:
            return
        text = self.render(status)
        # Re-sending identical text would only earn a "message is not modified"
        # from Telegram, so the comparison is what keeps the idle case free.
        if text == self._sent:
            return
        self._last_edit = time.monotonic()
        self._sent = text
        try:
            alive = await self._notifier.edit(self.message_id, text)
        except Exception:  # noqa: BLE001 — belt and braces; edit swallows its own
            log.exception("progress edit failed; build continues")
            return
        if not alive:
            self.message_id = None

    def render(self, status: str = "") -> str:
        now = time.time()
        done, total = self.tracker.counts()
        if not status:
            status = "Building" if done < total else "Finishing"
        bits = [status, progress.format_duration(now - self.started),
                f"{done}/{total} steps"]
        if self.attempts > 1:
            bits.append(f"attempt {self.attempt}/{self.attempts}")
        text = self.tracker.render(f"🛠 {self._label}", " · ".join(bits), now)
        if self._footer:
            text += f"\n<i>{_escape(self._footer)}</i>"
        # A checklist this size runs to well under a thousand characters, but the
        # label comes from a job board and its length is nobody's to promise.
        return text[:TELEGRAM_MAX_CHARS]


class Builder:
    """One worker, one build at a time, for the lifetime of the process."""

    def __init__(self, notifier, config: Config | None = None) -> None:
        self.notifier = notifier
        # See Notifier.config: an override pins the value, otherwise every read
        # picks up the current config.toml. A build queued before a settings
        # change and started after it therefore runs under the new settings,
        # which is the only reading of "on the fly" that is not a trap.
        self._config_override = config
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        # A job that has left the queue but has not finished is still "ahead" of
        # anything arriving now; qsize alone would report an empty queue while a
        # build is running.
        self._busy = False
        # The in-flight build, so `/cancel` has something to cancel, and whether
        # its cancellation was asked for — a user cancel and the loop shutting
        # down arrive as the same exception and must not be settled the same way.
        self._current: asyncio.Task | None = None
        self._cancelled = False

    @property
    def config(self) -> Config:
        return self._config_override or self.notifier.config

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Boot-time entry point. Call once, from `_post_init`.

        Recovery must not be reachable from `enqueue`: it declares every build
        row still `queued`/`running` abandoned, which is only true of rows a
        *previous* process left behind. Run inside a live process it sweeps the
        build currently in flight — and the row enqueue itself just wrote.
        """
        ensure_dirs()
        await self._recover()
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        """Guarantee a live consumer without touching recovery.

        `done()` covers the case where the worker died on something its own
        try/except could not catch; a queue with no consumer accepts approvals
        forever and builds none of them.
        """
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    async def _recover(self) -> None:
        """Report builds the last process was in the middle of.

        The queue lives in memory, so nothing survives a restart. Without this
        an approval that was killed mid-build is indistinguishable from one
        still running, and the user waits forever for a reply that is not coming.
        """
        with store.connect() as conn:
            stale = store.unfinished_builds(conn)
            for row in stale:
                store.finish_build(conn, row["id"], INTERRUPTED,
                                   detail="watcher restarted mid-build")
        if not stale:
            return
        for row in stale:
            await self._close_progress(row)
        names = "\n".join(f"• {_escape(r['company'])} — {_escape(r['title'])}"
                          for r in stale)
        await self.notifier.send_notice(
            f"↩️ <b>{len(stale)} build(s) interrupted by a restart</b>\n{names}\n\n"
            "<i>Reply “yes” again to any of them to retry.</i>"
        )

    async def _close_progress(self, row) -> None:
        """Settle the checklist of a build the last process was killed during.

        The reporter lives in memory with the queue, so a kill leaves its message
        reading `Building · 18m 20s` for good — the one state this feature exists
        to distinguish from a build that is actually working. The log on disk is
        the same stream the tracker was reading, so replaying it rebuilds exactly
        the checklist that was on screen, and the message ends up saying where the
        run had got to when it died.

        Best effort throughout: a build from before the column existed, a log that
        was rotated away, a message the user deleted. None of that is worth
        holding up boot for.
        """
        message_id = row["progress_message_id"]
        log_path = row["log_path"]
        if not message_id or not log_path:
            return
        try:
            # Straight off the row rather than through `store.build_log_path`,
            # so the stored relative value is resolved here instead.
            path = to_absolute(log_path)
            if not path.exists():
                return
            tracker = progress.replay(path)
            done, total = tracker.counts()
            status = " · ".join(["Interrupted by a restart",
                                 progress.format_duration(tracker.total_elapsed(
                                     tracker.last_event or tracker.started_at or 0.0)),
                                 f"{done}/{total} steps"])
            label = f"<b>{_escape(row['company'])}</b> — {_escape(row['title'])}"
            text = tracker.render(f"🛠 {label}", status, tracker.last_event or 0.0)
            await self.notifier.edit(message_id, text[:TELEGRAM_MAX_CHARS])
        except Exception:  # noqa: BLE001 — a stale message must not block boot
            log.exception("could not settle progress message %s", message_id)

    # -- api ---------------------------------------------------------------

    async def enqueue(self, posting_id: str, note: str,
                      reply_message_id: int | None = None,
                      override_duplicate: bool = False) -> int:
        """Accept a job. Returns how many builds are ahead of it.

        The count is what the caller needs to decide whether to acknowledge at
        all: the worker announces "Building …" the moment it picks a job up, so
        when nothing is ahead that announcement lands a second later and a
        separate "Queued …" is the same news twice. Only a non-zero count says
        something the worker's own message will not.

        `override_duplicate` is an explicit "yes anyway" to a decline the user
        has already read. The check is not re-run — they have seen its verdict
        and overruled it, and running it again could only reach the same answer.
        """
        ahead = self._queue.qsize() + (1 if self._busy else 0)
        with store.connect() as conn:
            build_id = store.queue_build(conn, posting_id)
        await self._queue.put(Job(posting_id, note, reply_message_id, build_id,
                                  override_duplicate=override_duplicate))
        self._ensure_worker()
        return ahead

    async def answer(self, question, text: str) -> int:
        """Continue the run that asked `question` with the user's reply.

        Queued like any other job so it takes its turn behind whatever is
        building — a resumed run spawns the same CLI, in the same workspace, and
        two of those at once is exactly what the queue exists to prevent.

        The session id comes from the question rather than from the build row so
        that a question written before the session was known still answers
        cleanly: `_attempt` falls back to a fresh build with the answer as its
        note, and says so.
        """
        session = ""
        posting_id = question["posting_id"]
        try:
            session = question["session_id"] or ""
        except (IndexError, KeyError):
            session = ""
        ahead = self._queue.qsize() + (1 if self._busy else 0)
        with store.connect() as conn:
            build_id = store.queue_build(conn, posting_id)
            # The asking build's log, so the continuation's checklist can start
            # from what that half of the run already did rather than from zero.
            earlier = store.build_log_path(conn, question["build_id"])
        await self._queue.put(Job(
            posting_id, "", question["message_id"], build_id,
            answer=text, resume_session=session, resume_log=earlier))
        self._ensure_worker()
        return ahead

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def cancel_current(self) -> bool:
        """Stop the build that is running now. Returns whether there was one.

        The task is cancelled rather than the process killed directly: `_spawn`
        already kills the CLI and its latexmk children on the way out (see the
        `CancelledError` branch there), so cancelling the task is what makes the
        process cleanup happen. `_cancelled` tells the worker this was asked for
        rather than the loop shutting down, which are otherwise the same
        exception.
        """
        task = self._current
        if task is None or task.done():
            return False
        self._cancelled = True
        task.cancel()
        return True

    def drop_queued(self, posting_id: str = "") -> Job | None:
        """Remove a not-yet-started job from the queue, and return it.

        With no `posting_id`, the job that would have started next goes. That is
        what `/cancel` wants when nothing is running: the queue has no visible
        handles, so "the next one" is the only thing a user can name without
        one, and it is the one they just queued by mistake.

        `asyncio.Queue` has no removal, so this drains and refills — safe here
        because the worker only ever takes from the queue between builds and
        this runs on the same loop, so no `get` can interleave.
        """
        kept: list[Job] = []
        dropped: Job | None = None
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if dropped is None and (not posting_id
                                    or job.posting_id == posting_id):
                dropped = job
                with store.connect() as conn:
                    store.finish_build(conn, job.build_id, CANCELLED,
                                       detail="dropped from the queue")
                continue
            kept.append(job)
        for job in kept:
            self._queue.put_nowait(job)
        return dropped

    # -- worker ------------------------------------------------------------

    async def _run_forever(self) -> None:
        while True:
            job = await self._queue.get()
            self._busy = True
            self._cancelled = False
            try:
                # A task rather than a bare await, so `/cancel` has something to
                # cancel. Awaiting `_handle` inline left the in-flight build with
                # no handle at all, and the only way to stop one was to kill the
                # watcher.
                self._current = asyncio.create_task(self._handle(job))
                await self._current
            except asyncio.CancelledError:
                if not self._cancelled:
                    # The loop is shutting down, not the build being stopped.
                    raise
                log.info("build cancelled by the user: %s", job.posting_id)
                await self._settle_cancelled(job)
            except Exception:
                # One bad build must not take the worker down with it, or every
                # later approval silently queues behind a dead task.
                log.exception("build failed for %s", job.posting_id)
                with store.connect() as conn:
                    store.finish_build(conn, job.build_id, FAILED,
                                       detail="unhandled error, see watcher.log")
                await self._reply(job, "❌ <b>Build failed</b>\nSee watcher.log.",
                                  topic="failed_build")
            finally:
                self._current = None
                self._busy = False
                self._queue.task_done()

    async def _settle_cancelled(self, job: Job) -> None:
        """Record and tidy after a build the user stopped.

        `_finish` never runs on this path, so everything it would have done —
        closing the build row, erasing the half-application — has to happen
        here. A cancelled build leaves the same debris as a failed one, and the
        same rule applies: a folder with no PDFs reads as an application already
        sent.
        """
        with store.connect() as conn:
            row = store.get_posting(conn, job.posting_id)
        company = row["company"] if row else ""
        title = row["title"] if row else ""
        label = f"<b>{_escape(company)}</b> — {_escape(title)}"

        cleaned = ""
        if company and title:
            outcome = await asyncio.to_thread(
                locate_output, company, title, self.config)
            if outcome.folder:
                cleaned = await asyncio.to_thread(
                    clean_up, outcome.folder, "cancelled by the user")
        with store.connect() as conn:
            store.finish_build(conn, job.build_id, CANCELLED, detail=cleaned)
        await self._reply(
            job,
            f"🛑 <b>Cancelled</b> · {label}\n"
            + (f"🧹 {_escape(cleaned)}" if cleaned
               else "<i>Nothing was left behind.</i>"),
            topic="failed_build")

    async def _reply(self, job: Job, text: str,
                     topic: str = "processing_build") -> int | None:
        """Report on a build, in the topic that stage of it belongs to.

        `processing_build` is the default because most of what a build says is
        progress — building, retrying, queued behind something, declined as a
        duplicate. Only the two ends of the run route elsewhere. With no topics
        configured every one of these is an ordinary reply, exactly as before.

        Returns the message id, which only the opener has any use for: it is the
        message the live checklist then edits for the rest of the build.

        Every message is recorded against its posting, which is what makes it a
        valid reply target: `run_watcher._resolve` reads `notifications` to work
        out which posting a reply is about, so a build report that was never
        recorded got the user "that message isn't one of mine" — including the
        one that ended by asking them to reply. The kind is `build` rather than
        `instant` or `digest` precisely so this cannot be mistaken for having
        pinged the posting (see `store.PING_KINDS`).
        """
        message_id = await self.notifier.send(text, reply_to=job.reply_message_id,
                                              topic=topic)
        if message_id:
            try:
                with store.connect() as conn:
                    store.record_notification(conn, job.posting_id,
                                              self.notifier.chat_id,
                                              message_id, kind="build")
            except Exception:  # noqa: BLE001 — the report itself already landed
                log.exception("could not record build message %s", message_id)
        return message_id

    async def _attempt(self, job: Job, company: str, title: str, url: str,
                       label: str,
                       reporter: "_ProgressReporter | None" = None,
                       ) -> RunResult:
        """Run the build, retrying only if the far end was briefly unwell.

        A 503 from the API — or from a gateway in front of it — can land on the
        last turn of a 25-minute run, and with cleanup wired in that erases an
        application that was all but finished. One free retry is worth more than
        the machine time it costs. Anything that is not an upstream failure is
        returned as-is, because repeating it would fail identically.

        Two conditions on that retry, both of them learned from one run:

        **Nothing is retried once the documents exist.** `CLAUDE.md` renders the
        CV and cover letter the moment they pass QA and treats Interview Prep as
        optional after that, and the ready message has already gone out. A drop
        on the prep step is therefore a finished application with a loose end,
        not a build to run again — one such retry rebuilt an announced
        application from step 00 and spent twenty-six minutes arriving back where
        it started. Returning the failure unretried hands it to `_finish`, which
        looks on disk, finds the documents, and salvages the run.

        **A partial folder is erased before the next attempt.** The previous
        design left it for the retry to scaffold over, which holds only while
        both attempts name the folder identically — and the archiver names it
        from the role title, so `AWS AI & Data Engineer` on one attempt and
        `AWS AI Data Engineer` on the next left two folders for one application,
        the abandoned one reading as an application already sent. Re-capturing
        the posting costs a minute; a phantom application costs a real one.

        Each attempt gets its own log file; the returned one is the last, which
        is the one whose failure is being reported.

        An answer to a stop-and-ask runs through the same loop, with the reply as
        the prompt and `--resume` pointing at the session that asked. If that
        session has gone, one fresh build is started with the answer as its note
        — the run has to begin again, but the user's decision is not lost.
        """
        answering = job.is_answer
        prompt = answer_prompt(job.answer) if answering else build_prompt(url, job.note)
        resume = job.resume_session if answering else ""
        suffix = "answer" if answering else ""
        attempts = self.config.build_retries + 1
        log_file = log_path_for(company, title, suffix=suffix)

        def remember(session_id: str) -> None:
            with store.connect() as conn:
                store.set_build_session(conn, job.build_id, session_id)

        attempt = 0
        while attempt < attempts:
            attempt += 1
            log_file = log_path_for(company, title, attempt, suffix=suffix)
            with store.connect() as conn:
                store.mark_build_running(conn, job.build_id, str(log_file))

            log.info("build start: %s — %s (attempt %d/%d)%s",
                     company, title, attempt, attempts,
                     f", resuming {resume}" if resume else "")
            if reporter is not None:
                reporter.begin_attempt(attempt, attempts)
            try:
                run = await _spawn(
                    prompt, self.config, log_file,
                    on_event=reporter.feed if reporter is not None else None,
                    resume=resume, on_session=remember)
            except Exception:
                # Route a crash into the ordinary failure path rather than up to
                # the worker's catch-all, which knows no company and so cannot
                # clean up.
                log.exception("build crashed: %s — %s", company, title)
                return RunResult(ok=False,
                                 detail="the build crashed, see watcher.log",
                                 log_file=log_file)

            if not run.ok and resume and session_is_gone(run.detail):
                # The session the question belongs to is gone — usually because
                # the CLI's history was pruned. Retrying the resume can only fail
                # the same way, so start over once with the answer as the note.
                # This does not spend a retry: nothing was attempted, and the
                # fresh build deserves the same budget any other build gets.
                log.warning("session %s is gone — rebuilding with the answer as "
                            "a note: %s — %s", resume, company, title)
                await self._reply(
                    job,
                    f"↩️ <b>That session has expired</b> · {label}\n"
                    "<i>Starting a fresh build with your answer as the "
                    "instruction.</i>")
                prompt = build_prompt(url, job.answer)
                resume = ""
                suffix = ""
                attempt -= 1
                continue

            if run.ok or attempt == attempts or not is_transient(run.detail):
                return run

            # Ask the disk before spending another half hour. `_finish` runs the
            # same check and salvages what it finds, so this only decides whether
            # there is anything left to do — and if the documents are there, the
            # answer is no.
            done = await asyncio.to_thread(
                locate_output, company, title, self.config)
            if done.status == DONE:
                log.info("documents already complete — not retrying %s: %s — %s",
                         run.detail, company, title)
                return run

            if done.folder:
                # Erase the half-application before the next attempt, so the two
                # cannot end up side by side under slightly different names.
                await asyncio.to_thread(clean_up, done.folder,
                                        f"retrying after {run.detail}")

            delay = self.config.build_retry_delay_seconds
            log.warning("transient failure (%s) — retrying in %ds: %s — %s",
                        run.detail, delay, company, title)
            await self._reply(
                job,
                f"🔁 <b>Retrying</b> · {label}\n{_escape(run.detail)}\n"
                f"<i>Attempt {attempt + 1} of {attempts}, in {delay}s. "
                "The partial folder was removed first.</i>",
            )
            await asyncio.sleep(delay)

        # for-else territory; unreachable while attempts >= 1.
        return RunResult(ok=False, detail="no build attempt ran", log_file=log_file)

    async def _announce_ready(self, job: Job, company: str, title: str,
                              label: str) -> bool:
        """Say the application is ready the moment its required PDFs land.

        The CV and cover letter are rendered, checked, and logged several
        minutes before Interview Prep finishes — on a measured run, 7.5 of 40
        minutes were spent on a private study aid nobody was waiting for.
        Reporting only when the process exits made every run look as long as
        its slowest optional step.

        `locate_output` already returns DONE on the required documents alone,
        so this asks the same question the final check asks, just earlier.
        """
        sizes: dict[str, int] = {}
        while True:
            await asyncio.sleep(READY_POLL_SECONDS)
            outcome = await asyncio.to_thread(
                locate_output, company, title, self.config)
            if outcome.status != DONE or not outcome.folder:
                continue

            # A PDF still being written grows between polls. Require one quiet
            # interval before calling it finished, so a half-flushed file never
            # triggers a ready message for documents that are still moving.
            folder = Path(outcome.folder)
            current: dict[str, int] = {}
            for name in dedupe.required_pdfs():
                try:
                    current[name] = (folder / name).stat().st_size
                except OSError:
                    current = {}
                    break
            if not current or any(size == 0 for size in current.values()):
                continue
            if current != sizes:
                sizes = current
                continue

            # Completed rather than processing: from the user's side this *is*
            # the finished application. Interview Prep still rendering behind it
            # is a study aid, and the message says so itself.
            await self._reply(job, ready_message(label, outcome),
                              topic="completed_build")
            log.info("application ready (prep still rendering): %s — %s",
                     company, title)
            return True

    async def _handle(self, job: Job) -> None:
        with store.connect() as conn:
            row = store.get_posting(conn, job.posting_id)
        if row is None:
            with store.connect() as conn:
                store.finish_build(conn, job.build_id, FAILED,
                                   detail="posting vanished from the database")
            await self._reply(job, "❌ That posting is no longer in the database.",
                              topic="failed_build")
            return

        company, title, url = row["company"], row["title"], row["url"]
        label = f"<b>{_escape(company)}</b> — {_escape(title)}"

        # An answer continues a run that has already been through this check, and
        # an explicit "yes anyway" has already overruled it. Neither is worth
        # asking twice.
        existing, partial = (None, "")
        if not job.is_answer and not job.override_duplicate:
            existing, partial = check_duplicate(company, title, self.config)
        if existing is not None:
            # Logged, because the silence was half of why a declined build read
            # as one that never started: nothing reached watcher.log at all, so
            # the only trace was a Telegram message in a topic the user was not
            # watching.
            log.info("declined as a duplicate: %s — %s (%s)",
                     company, title, existing.describe())
            with store.connect() as conn:
                store.finish_build(conn, job.build_id, DUPLICATE,
                                   folder=existing.folder,
                                   detail=existing.describe())
            # Targeted, not Processing: this is a decision waiting on the user,
            # and the same topic their approval was in. Sending it to Processing
            # put the answer to "yes" in a topic nobody reads for answers.
            message_id = await self._reply(job, duplicate_message(label, existing),
                                           topic="targeted_build")
            if message_id:
                with store.connect() as conn:
                    store.ask_question(
                        conn, job.build_id, job.posting_id,
                        store.QUESTION_DUPLICATE, self.notifier.chat_id,
                        message_id,
                        thread_id=self.config.topic_for("targeted_build"),
                        question=f"Already applied — {existing.describe()}")
                    # No `folder`, deliberately. That column is "what this run
                    # left behind, to be erased if it is abandoned", and this
                    # run left nothing — the folder belongs to a finished
                    # application. Recording it would point `no` and `/cancel`
                    # at a complete application and delete it, along with its
                    # tracker row. `describe()` already names it in the
                    # question text, which is all the user needs.
            return

        if job.is_answer:
            opener = f"💬 Continuing {label}…"
        else:
            opener = f"🛠 Building {label}…"
            if partial:
                opener += f"\n<i>{_escape(partial)}</i>"
        message_id = await self._reply(job, opener)

        # That opener becomes the live checklist, edited in place for the rest of
        # the build. It is stored so a restart can find it: the queue lives in
        # memory, so a build interrupted by one would otherwise leave its
        # checklist frozen mid-run and reading as still in progress for good.
        reporter = None
        if message_id is not None and self.config.build_progress_updates:
            reporter = _ProgressReporter(self.notifier, self.config, job, label,
                                         message_id, footer=partial)
            if job.resume_log:
                # Off the loop: this reads and decodes the asking session's whole
                # event stream, and the loop is about to be the only thing
                # draining the new one's stdout.
                await asyncio.to_thread(reporter.prime, Path(job.resume_log))
            reporter.start()
            with store.connect() as conn:
                store.set_build_progress_message(conn, job.build_id, message_id)

        # Runs alongside the build and reports the CV and cover letter the
        # moment they land, rather than making the user wait out Interview Prep.
        announcer = asyncio.create_task(
            self._announce_ready(job, company, title, label))
        # Set once the outcome is known. The default covers the paths that never
        # get that far — a crash on the way, or a cancellation — and stopping the
        # reporter in a `finally` is what stops it editing a message about a
        # build that no longer exists.
        verdict = "Interrupted"
        try:
            try:
                run = await self._attempt(job, company, title, url, label,
                                          reporter)
            finally:
                announced = await _settle(announcer)
            verdict = await self._finish(job, company, title, label, run,
                                         announced)
        finally:
            if reporter is not None:
                await reporter.stop(verdict)

    async def _finish(self, job: Job, company: str, title: str, label: str,
                      run: RunResult, announced: bool) -> str:
        """Settle a finished run: salvage, clean up, record, report.

        Split out of `_handle` so the live checklist can be stopped in a
        `finally` around the whole of it, with the verdict this returns as its
        closing line.
        """

        # Look on disk either way. A failed run still leaves a folder behind,
        # and finding it is the only way to know what needs erasing — or whether
        # there is anything there worth keeping.
        log_file = run.log_file or log_path_for(company, title)
        outcome = locate_output(company, title, self.config)
        outcome.announced = announced
        if not run.ok:
            failure = run.detail or "the CLI reported an error"
            if outcome.status == DONE:
                # The run died, but every required PDF is on disk. Interview
                # Prep is where a long build spends its last ten minutes, so an
                # upstream drop lands there more often than anywhere else — and
                # erasing the folder for it threw away a CV and a cover letter
                # that had already passed QA and been announced as ready. The
                # study aid is a private extra; the application is the point.
                #
                # `locate_output` asks the disk for the required documents
                # rather than asking the model how it went, so DONE here is a
                # fact about files, which is exactly the evidence worth
                # overruling an exit code with.
                log.warning("build failed after the documents were complete, "
                            "keeping the application: %s — %s (%s)",
                            company, title, failure)
                outcome.salvaged = failure
            else:
                outcome = Outcome(status=FAILED, folder=outcome.folder,
                                  detail=failure,
                                  documents=outcome.documents,
                                  tracker_row=outcome.tracker_row,
                                  announced=announced)
        elif outcome.status != DONE:
            # A clean exit with no application. `CLAUDE.md` gives the pipeline
            # three reasons to stop and ask — a claim the canonical profile does
            # not support, a posting that cannot be captured, an ambiguous
            # company name — and tells it to stop rather than invent an answer.
            # It did exactly that; what failed was this end, which called it
            # "incomplete: missing CV.pdf, Cover Letter.pdf" and threw the
            # question away. Whatever the run's last words were, they explain
            # this better than a list of absent files.
            #
            # No attempt is made to tell a stop-and-ask from a run that quietly
            # produced nothing: both end the same way, both are cleaned up the
            # same way, and quoting what the build said is the more useful
            # report either way.
            log.info("build stopped without documents: %s — %s", company, title)
            outcome = Outcome(status=NEEDS_DECISION, folder=outcome.folder,
                              detail=run.closing or outcome.detail,
                              documents=outcome.documents,
                              tracker_row=outcome.tracker_row,
                              announced=announced)

        if outcome.status in (FAILED, INCOMPLETE):
            outcome.cleaned = await asyncio.to_thread(
                clean_up, outcome.folder, outcome.detail or outcome.status)
        # A stop-and-ask keeps its folder. The run is paused, not abandoned: the
        # archived posting, the Match Brief and the Research Note are most of what
        # makes resuming cheap, and erasing them turned every answer into a
        # rebuild from step 00. Safe against the "a stale folder reads as already
        # applied" rule in CLAUDE.md, because `dedupe.is_complete` requires the
        # PDFs — a folder without them never blocks anything. Cleanup moves to
        # whichever comes first: the user declining, or the question expiring.

        record = " · ".join(
            part for part in (outcome.detail, outcome.salvaged, outcome.cleaned)
            if part)
        with store.connect() as conn:
            store.finish_build(conn, job.build_id, outcome.status,
                               folder=outcome.folder, detail=record)
        log.info("build %s: %s — %s", outcome.status, company, title)
        # A salvaged run counts as completed: its status is DONE and the
        # documents are on disk, which is what the Completed topic is a record
        # of. The caveat about the run not finishing cleanly rides along in the
        # message rather than moving it to Failed, where it would sit next to
        # applications that do not exist.
        # A question belongs where the approval conversation is, not in Failed
        # among applications that broke — the user answered "yes" in that topic
        # and this is the pipeline answering back.
        topic = "failed_build"
        if outcome.status == DONE:
            topic = "completed_build"
        elif outcome.status == NEEDS_DECISION:
            topic = "targeted_build"
        message_id = await self._reply(job, result_message(label, outcome, log_file),
                                       topic=topic)

        if outcome.status == NEEDS_DECISION and message_id:
            # Record what was asked, where it was asked, and which session can
            # answer it. Without this row the message is just text: the reply
            # handler has nothing to match a reply against, and the run's own
            # instruction to "answer here" is a promise nothing keeps.
            with store.connect() as conn:
                store.ask_question(
                    conn, job.build_id, job.posting_id,
                    store.QUESTION_STOP_AND_ASK, self.notifier.chat_id,
                    message_id, thread_id=self.config.topic_for(topic),
                    question=outcome.detail, folder=outcome.folder,
                    session_id=run.session_id)

        if outcome.status == DONE:
            return "Complete" if not outcome.salvaged else "Complete (salvaged)"
        if outcome.status == NEEDS_DECISION:
            return "Waiting on you"
        return "Failed" if outcome.status == FAILED else outcome.status.title()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def _describe_plan(company: str, title: str, url: str, note: str,
                   config: Config) -> int:
    existing, partial = check_duplicate(company, title, config)
    print(f"company : {company}")
    print(f"title   : {title}")
    print(f"url     : {url}")
    print(f"note    : {note or '(none)'}")
    print()
    if existing is not None:
        print(f"DUPLICATE — {existing.describe()}")
        print("No build would run.")
        return 0
    if partial:
        print(f"note    : {partial}")

    try:
        cmd = command_for(config)
    except ClaudeError as exc:
        print(f"cannot build: {exc}")
        return 1
    print("would run:")
    print("  " + " ".join(f'"{part}"' if " " in part else part for part in cmd))
    print(f"  cwd   : {REPO_ROOT}")
    print(f"  stdin : {build_prompt(url, note)!r}")
    print(f"  log   : {log_path_for(company, title)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--posting", help="posting id from the watcher database")
    parser.add_argument("--company", help="instead of --posting, for a dry run")
    parser.add_argument("--title", help="instead of --posting, for a dry run")
    parser.add_argument("--url", default="", help="posting url for a dry run")
    parser.add_argument("--note", default="", help="free-form note, e.g. 'add German'")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the command and the duplicate verdict, run nothing")
    args = parser.parse_args(argv)

    config = load_config()
    if args.posting:
        with store.connect() as conn:
            row = store.get_posting(conn, args.posting)
        if row is None:
            print(f"no posting with id {args.posting}")
            return 1
        company, title, url = row["company"], row["title"], row["url"]
    elif args.company and args.title:
        company, title, url = args.company, args.title, args.url
    else:
        print("give either --posting <id> or --company X --title Y")
        return 2

    if not args.dry_run:
        print("Only --dry-run is supported from the command line; real builds "
              "are started by replying to a Telegram notification.")
        return 2
    return _describe_plan(company, title, url, args.note, config)


if __name__ == "__main__":
    raise SystemExit(main())
