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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from . import dedupe, store
from .claude_cli import ClaudeError, resolve_bin
from .config import (BUILD_LOG_DIR, BUILD_SETTINGS_PATH, REPO_ROOT, Config,
                     ensure_dirs, load_config, load_identity)
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


def log_path_for(company: str, title: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return BUILD_LOG_DIR / f"{stamp}-{_slug(company, 24)}-{_slug(title)}.log"


def command_for(config: Config) -> list[str]:
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
                       capture_output=True, timeout=30)
    except Exception:
        log.exception("could not kill build process tree %s", pid)


async def _spawn(prompt: str, config: Config, log_file: Path) -> tuple[bool, str]:
    """Run one build. Returns (ok, detail).

    The NDJSON stream is written through to the log line by line rather than
    buffered, so a hung build can be inspected while it is still hung.
    """
    cmd = command_for(config)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        limit=STREAM_LIMIT,
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
                    detail = str(event.get("subtype") or "")

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

    if process.returncode != 0 and not detail:
        detail = f"exit {process.returncode}"
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


def result_message(label: str, outcome: Outcome, log_file: Path) -> str:
    where = _escape(relative(outcome.folder)) if outcome.folder else ""
    prefix = f"{load_identity().file_prefix} - "
    docs = " · ".join(
        name.removeprefix(prefix).replace(".pdf", "")
        for name in outcome.documents
    )
    tracker = "tracker row ✓" if outcome.tracker_row else "no tracker row"

    if outcome.status == DONE:
        return (f"✅ <b>Built</b> · {label}\n<code>{where}</code>\n{docs} · {tracker}")
    if outcome.status == INCOMPLETE:
        return (f"⚠️ <b>Built with issues</b> · {label}\n<code>{where}</code>\n"
                f"{_escape(outcome.detail)}\n{docs or 'no documents'} · {tracker}\n"
                f"<i>log: {_escape(log_file.name)}</i>")
    return (f"❌ <b>Build failed</b> · {label}\n{_escape(outcome.detail)}\n"
            f"<i>log: {_escape(log_file.name)}</i>")


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

class Builder:
    """One worker, one build at a time, for the lifetime of the process."""

    def __init__(self, notifier, config: Config | None = None) -> None:
        self.notifier = notifier
        self.config = config or notifier.config
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        # A job that has left the queue but has not finished is still "ahead" of
        # anything arriving now; qsize alone would report an empty queue while a
        # build is running.
        self._busy = False

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
                await self._reply(job, "❌ <b>Build failed</b>\nSee watcher.log.")
            finally:
                self._busy = False
                self._queue.task_done()

    async def _reply(self, job: Job, text: str) -> None:
        await self.notifier.send(text, reply_to=job.reply_message_id)

    async def _handle(self, job: Job) -> None:
        with store.connect() as conn:
            row = store.get_posting(conn, job.posting_id)
        if row is None:
            with store.connect() as conn:
                store.finish_build(conn, job.build_id, FAILED,
                                   detail="posting vanished from the database")
            await self._reply(job, "❌ That posting is no longer in the database.")
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

        log_file = log_path_for(company, title)
        with store.connect() as conn:
            store.mark_build_running(conn, job.build_id, str(log_file))

        opener = f"🛠 Building {label}…"
        if partial:
            opener += f"\n<i>{_escape(partial)}</i>"
        await self._reply(job, opener)

        log.info("build start: %s — %s", company, title)
        ok, detail = await _spawn(build_prompt(url, job.note), self.config, log_file)

        outcome = (locate_output(company, title, self.config) if ok
                   else Outcome(status=FAILED, detail=detail or "the CLI reported an error"))
        with store.connect() as conn:
            store.finish_build(conn, job.build_id, outcome.status,
                               folder=outcome.folder, detail=outcome.detail)
        log.info("build %s: %s — %s", outcome.status, company, title)
        await self._reply(job, result_message(label, outcome, log_file))


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
