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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from . import dedupe, store
from .claude_cli import ClaudeError, resolve_bin
from .config import (BUILD_LOG_DIR, BUILD_SETTINGS_PATH, REPO_ROOT, Config,
                     ensure_dirs, load_config, load_identity,
                     sync_build_settings)

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


# --------------------------------------------------------------------------
# spawning
# --------------------------------------------------------------------------

def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return (cleaned[:limit] or "build").lower()


def log_path_for(company: str, title: str, attempt: int = 1) -> Path:
    """Where one build attempt writes its NDJSON transcript.

    The attempt suffix is not cosmetic: the timestamp only resolves to the
    second, and the log is opened for writing, so two attempts that started
    close together would otherwise have the second overwrite the first — losing
    exactly the transcript of the failure that caused the retry.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "" if attempt <= 1 else f"-retry{attempt - 1}"
    return BUILD_LOG_DIR / f"{stamp}-{_slug(company, 24)}-{_slug(title)}{suffix}.log"


def command_for(config: Config) -> list[str]:
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
    text = " ".join(str(event.get("result") or "").split())
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


async def _spawn(prompt: str, config: Config, log_file: Path) -> tuple[bool, str]:
    """Run one build. Returns (ok, detail).

    The NDJSON stream is written through to the log line by line rather than
    buffered, so a hung build can be inspected while it is still hung.
    """
    cmd = command_for(config)
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

    async def pump() -> None:
        nonlocal detail, ok
        assert process.stdout is not None
        with log_file.open("w", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(cmd)}\ncwd: {REPO_ROOT}\n\n{prompt}\n\n---\n")
            handle.flush()
            async for raw in _lines(process.stdout, handle):
                line = raw.decode("utf-8", errors="replace")
                handle.write(line)
                handle.flush()
                event = _parse_event(line)
                if event and event.get("type") == "result":
                    ok = not event.get("is_error", False)
                    detail = "" if ok else _result_detail(event)

    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()

    timeout = config.build_timeout_minutes * 60
    try:
        await asyncio.wait_for(asyncio.gather(pump(), process.wait()), timeout)
    except asyncio.TimeoutError:
        _kill_tree(process.pid)
        return False, f"timed out after {config.build_timeout_minutes} min"
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
    return ok and process.returncode == 0, detail


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


def duplicate_message(label: str, existing: dedupe.ExistingApplication) -> str:
    when = existing.applied_on.isoformat() if existing.applied_on else "an unknown date"
    where = relative(existing.folder) if existing.folder else "logged in the tracker"
    return (f"⚠️ <b>Duplicate</b> · {label}\n"
            f"Already applied {when} — {_escape(where)}\n"
            f"<i>Nothing was built.</i>")


def _doc_list(documents: tuple[str, ...]) -> str:
    """Document names for a report, with the owner's file prefix stripped.

    Stripping it is cosmetic: `<file_prefix> - CV.pdf` reads better as "CV" in a
    Telegram message that already says whose workspace this is. It is not
    worth an exception. `load_identity` raises when identity.toml is missing or
    still full of FILL IN, and this is the function that reports a build —
    including the build that failed *because* the workspace is incomplete. A
    report that cannot render is strictly worse than one that spells a filename
    out in full, and raising here replaces the real failure with this one.
    """
    try:
        prefix = f"{load_identity().file_prefix} - "
    except Exception:  # missing, unreadable, or still a placeholder
        prefix = ""
    return " · ".join(name.removeprefix(prefix).replace(".pdf", "")
                      for name in documents)


def ready_message(label: str, outcome: Outcome) -> str:
    """Sent the moment the CV and cover letter exist, before the run ends.

    Interview Prep is a private study aid and renders after the documents an
    employer actually sees. Holding this message until the process exits made
    every run look as long as its slowest optional step.
    """
    where = _escape(relative(outcome.folder)) if outcome.folder else ""
    return (f"✅ <b>Application ready</b> · {label}\n"
            f"<code>{where}</code>\n"
            f"{_doc_list(outcome.documents)}\n"
            f"<i>Interview prep is still rendering — it will land in the same "
            f"folder.</i>")


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
        if outcome.salvaged:
            # Everything an employer sees is finished and still on disk, so
            # this is not a failure — but it is not a clean run either, and the
            # missing piece is usually Interview Prep or the tracker row. Both
            # are named above, so say what broke and let the user read them.
            head += (f"\n⚠️ <i>The run itself did not finish cleanly: "
                     f"{_escape(outcome.salvaged)}. The documents above are "
                     f"complete and were kept.</i>")
        return head

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
        names = "\n".join(f"• {_escape(r['company'])} — {_escape(r['title'])}"
                          for r in stale)
        await self.notifier.send_notice(
            f"↩️ <b>{len(stale)} build(s) interrupted by a restart</b>\n{names}\n\n"
            "<i>Reply “yes” again to any of them to retry.</i>"
        )

    # -- api ---------------------------------------------------------------

    async def enqueue(self, posting_id: str, note: str,
                      reply_message_id: int | None = None) -> int:
        """Accept a job. Returns how many builds are ahead of it.

        The count is what the caller needs to decide whether to acknowledge at
        all: the worker announces "Building …" the moment it picks a job up, so
        when nothing is ahead that announcement lands a second later and a
        separate "Queued …" is the same news twice. Only a non-zero count says
        something the worker's own message will not.
        """
        ahead = self._queue.qsize() + (1 if self._busy else 0)
        with store.connect() as conn:
            build_id = store.queue_build(conn, posting_id)
        await self._queue.put(Job(posting_id, note, reply_message_id, build_id))
        self._ensure_worker()
        return ahead

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    # -- worker ------------------------------------------------------------

    async def _run_forever(self) -> None:
        while True:
            job = await self._queue.get()
            self._busy = True
            try:
                await self._handle(job)
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
                self._busy = False
                self._queue.task_done()

    async def _reply(self, job: Job, text: str,
                     topic: str = "processing_build") -> None:
        """Report on a build, in the topic that stage of it belongs to.

        `processing_build` is the default because most of what a build says is
        progress — building, retrying, queued behind something, declined as a
        duplicate. Only the two ends of the run route elsewhere. With no topics
        configured every one of these is an ordinary reply, exactly as before.
        """
        await self.notifier.send(text, reply_to=job.reply_message_id,
                                 topic=topic)

    async def _attempt(self, job: Job, company: str, title: str, url: str,
                       label: str) -> tuple[bool, str, Path]:
        """Run the build, retrying only if the far end was briefly unwell.

        A 503 from the API — or from a gateway in front of it — can land on the
        last turn of a 25-minute run, and with cleanup wired in that erases an
        application that was all but finished. One free retry is worth more than
        the machine time it costs. Anything that is not an upstream failure is
        returned as-is, because repeating it would fail identically.

        The folder from the previous attempt is deliberately left in place: the
        pipeline scaffolds over it, so the archived posting survives, and the
        tracker de-dupes on company+role+date, so a row written by an attempt that
        later fell over is updated rather than doubled.

        Each attempt gets its own log file; the returned one is the last, which
        is the one whose failure is being reported.
        """
        prompt = build_prompt(url, job.note)
        attempts = self.config.build_retries + 1
        log_file = log_path_for(company, title)

        for attempt in range(1, attempts + 1):
            log_file = log_path_for(company, title, attempt)
            with store.connect() as conn:
                store.mark_build_running(conn, job.build_id, str(log_file))

            log.info("build start: %s — %s (attempt %d/%d)",
                     company, title, attempt, attempts)
            try:
                ok, detail = await _spawn(prompt, self.config, log_file)
            except Exception:
                # Route a crash into the ordinary failure path rather than up to
                # the worker's catch-all, which knows no company and so cannot
                # clean up.
                log.exception("build crashed: %s — %s", company, title)
                return False, "the build crashed, see watcher.log", log_file

            if ok or attempt == attempts or not is_transient(detail):
                return ok, detail, log_file

            delay = self.config.build_retry_delay_seconds
            log.warning("transient failure (%s) — retrying in %ds: %s — %s",
                        detail, delay, company, title)
            await self._reply(
                job,
                f"🔁 <b>Retrying</b> · {label}\n{_escape(detail)}\n"
                f"<i>Attempt {attempt + 1} of {attempts}, in {delay}s. "
                "Nothing has been cleaned up yet.</i>",
            )
            await asyncio.sleep(delay)

        # for-else territory; unreachable while attempts >= 1.
        return False, "no build attempt ran", log_file

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

        existing, partial = check_duplicate(company, title, self.config)
        if existing is not None:
            with store.connect() as conn:
                store.finish_build(conn, job.build_id, DUPLICATE,
                                   folder=existing.folder,
                                   detail=existing.describe())
            await self._reply(job, duplicate_message(label, existing))
            return

        opener = f"🛠 Building {label}…"
        if partial:
            opener += f"\n<i>{_escape(partial)}</i>"
        await self._reply(job, opener)

        # Runs alongside the build and reports the CV and cover letter the
        # moment they land, rather than making the user wait out Interview Prep.
        announcer = asyncio.create_task(
            self._announce_ready(job, company, title, label))
        try:
            ok, detail, log_file = await self._attempt(
                job, company, title, url, label)
        finally:
            announced = await _settle(announcer)

        # Look on disk either way. A failed run still leaves a folder behind,
        # and finding it is the only way to know what needs erasing — or whether
        # there is anything there worth keeping.
        outcome = locate_output(company, title, self.config)
        outcome.announced = announced
        if not ok:
            failure = detail or "the CLI reported an error"
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

        if outcome.status in (FAILED, INCOMPLETE):
            outcome.cleaned = await asyncio.to_thread(
                clean_up, outcome.folder, outcome.detail or outcome.status)

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
        await self._reply(
            job, result_message(label, outcome, log_file),
            topic="completed_build" if outcome.status == DONE else "failed_build")


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
