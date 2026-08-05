"""The always-on process: poll, score, notify, and listen for replies.

One event loop, one scheduler. Telegram's own JobQueue is used for the timers
rather than a second APScheduler instance — it is APScheduler underneath, and
sharing it means the poll, the digest, and the reply handler cannot fall out of
sync with each other.

    python run_watcher.py            # run it
    python run_watcher.py --once     # one poll + score + notify, then exit

Fetching is blocking (requests) and scoring shells out to the CLI, so both run
in worker threads; the loop stays free to answer a reply while a poll is still
in flight.

Two commands are answered from the chat itself — `/status` and `/restart` — but
only outside the posting topics, so a mis-sent command inside one is still read
as a reply to the posting it was sent under.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import subprocess
import sys

from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from watcher import kb, matcher, poll, replies, store
from watcher.builder import Builder
from watcher.config import (RESTART_MARKER_PATH, load_config, load_env,
                            require_env)
from watcher.logsetup import setup
from watcher.notifier import Notifier, format_kb_proposal, format_status

log = logging.getLogger("watcher.run")

BUILD_DISABLED_NOTE = (
    "Recorded. Builds are switched off (<code>[build] enabled = false</code> in "
    "config.toml), so nothing was started."
)


def local_time(hour: int, minute: int) -> dt.time:
    """A daily run time in the machine's own timezone.

    JobQueue treats a naive `dt.time` as UTC, so `digest_hour = 19` in
    config.toml was firing at 21:05 in Berlin while both the config comment and
    `watcherctl status` called it "19:00 local". Two hours is enough to push the
    evening digest past the point anyone is still looking, and the heartbeat out
    of the morning entirely.

    The zone is resolved rather than the current offset frozen: a fixed +02:00
    captured in August would still be claiming summer time in December.
    """
    try:
        from tzlocal import get_localzone  # APScheduler's own dependency
        zone = get_localzone()
    except Exception:  # no tzlocal, or no system zone to read
        # Fixed offset for today. Wrong after the next DST change, but closer
        # than UTC and it keeps the watcher starting.
        zone = dt.datetime.now().astimezone().tzinfo
        log.warning("no local timezone available; daily jobs pinned to %s", zone)
    return dt.time(hour=hour, minute=minute, tzinfo=zone)


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------

async def run_cycle(notifier: Notifier) -> dict[str, int]:
    """Poll every source, score what is new, ping what scores well."""
    config = notifier.config

    report = await asyncio.to_thread(poll.poll_once, False, None, config, None)
    if report.errors:
        for source, error in report.errors.items():
            log.warning("source %s failed: %s", source, error)
    await notifier.send_source_alerts(report.newly_disabled)
    await notifier.send_source_parked(report.newly_parked)
    await notifier.send_source_recovered(report.recovered)

    # Unconditional, not `if report.stored`. A posting whose scoring failed is
    # left unscored on purpose so it can be retried, and gating this on *new*
    # postings arriving would make that retry wait on unrelated activity.
    # `match_pending` is one sqlite query and an early return when there is
    # nothing pending, so the cycle pays nothing for asking.
    match_report = await asyncio.to_thread(matcher.match_pending, config)
    await notifier.send_scoring_degraded(match_report)

    notified = await notifier.send_instant()
    log.info("cycle: fetched %d, new %d, notified %d",
             report.fetched, report.stored, notified)
    return {"fetched": report.fetched, "stored": report.stored,
            "notified": notified}


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    notifier: Notifier = context.application.bot_data["notifier"]
    try:
        context.application.bot_data["last_cycle"] = await run_cycle(notifier)
    except Exception:
        # A crashing scheduled job would otherwise take the whole watcher down
        # silently; the daily heartbeat is the backstop that surfaces it.
        log.exception("poll cycle failed")


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    notifier: Notifier = context.application.bot_data["notifier"]
    try:
        count = await notifier.send_digest()
        log.info("digest sent covering %d posting(s)", count)
    except Exception:
        log.exception("digest failed")


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    notifier: Notifier = context.application.bot_data["notifier"]
    cycle = context.application.bot_data.get("last_cycle", {})
    # Logged on the way out as well as on failure: `send_notice` swallows its
    # errors, so without this a heartbeat that never ran and one that ran fine
    # look identical in the log — which is the wrong pair of things to confuse
    # when the question is "has this been quiet or has it been dead?".
    log.info("heartbeat: last cycle fetched %d, new %d, notified %d",
             cycle.get("fetched", 0), cycle.get("stored", 0),
             cycle.get("notified", 0))
    await notifier.send_heartbeat(cycle)


async def kb_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Weekly: propose condensed matching rules, and wait for a yes.

    Nothing is written here. The proposal is sent, its message id is stored, and
    `on_reply` applies it only if the answer is yes — so the file can never gain
    a rule the user did not see. A proposal still awaiting an answer suppresses
    the next one rather than stacking a second question on top of the first.
    """
    notifier: Notifier = context.application.bot_data["notifier"]
    config = notifier.config
    if not config.kb_enabled:
        return
    if kb.pending():
        log.info("kb: a proposal is still unanswered — skipping this week")
        return
    try:
        proposal = await asyncio.to_thread(kb.propose, config)
    except Exception:
        log.exception("kb consolidation failed")
        return
    if proposal is None or not proposal.get("proposals"):
        log.info("kb: nothing worth proposing")
        return

    message_id = await notifier.send(format_kb_proposal(proposal))
    kb.save_pending(proposal, message_id)
    log.info("kb: proposed %d rule(s), awaiting approval",
             len(proposal["proposals"]))


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------

def _resolve(chat_id: str, message_id: int, index: int | None) -> tuple[str | None, str]:
    """Which posting a reply refers to. Returns (posting_id, complaint)."""
    with store.connect() as conn:
        ids = store.postings_for_message(conn, chat_id, message_id)
    if not ids:
        return None, ("That message isn't one of mine, or it predates the current "
                      "database. Reply to a posting notification.")
    if len(ids) == 1:
        return ids[0], ""
    if index is None:
        return None, (f"That digest covers {len(ids)} postings — say which one, "
                      f"e.g. <code>build 2</code>.")
    if not 1 <= index <= len(ids):
        return None, f"There is no line {index}; that digest has {len(ids)}."
    return ids[index - 1], ""


async def _handle_kb_reply(message, reply: replies.Reply) -> bool:
    """Answer a reply to the weekly proposal. True if it was one.

    An unrecognised answer leaves the proposal pending rather than discarding
    it — a stray message must not be able to silently drop rules that were
    already reviewed, and it must not be able to write them either.
    """
    proposal = kb.pending()
    if not proposal or message.reply_to_message.message_id != proposal.get("message_id"):
        return False

    if reply.action == replies.APPROVE:
        written = kb.apply_proposal(proposal)
        kb.clear_pending(proposal.get("consumed_through"))
        await message.reply_html(
            f"🧠 Added {written} rule(s) to profile_kb.md."
            if written else "Nothing to add — the proposal was empty.")
        return True

    if reply.action in (replies.SKIP, replies.SNOOZE):
        # The mark moves on a decline too: the same decisions must not produce
        # the same rejected proposal again next week.
        kb.clear_pending(proposal.get("consumed_through"))
        await message.reply_html("👌 Discarded. profile_kb.md is unchanged.")
        return True

    await message.reply_html(
        "That's the weekly matching-rules proposal — reply <code>yes</code> to "
        "add them or <code>no</code> to discard. It stays pending until then."
    )
    return True


async def on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.reply_to_message:
        return

    notifier: Notifier = context.application.bot_data["notifier"]
    config = notifier.config
    reply = replies.parse(message.text or "")

    # A reply to the weekly consolidation is answered before any posting lookup:
    # that message has no posting behind it, so `_resolve` would reject it as
    # "not one of mine".
    if await _handle_kb_reply(message, reply):
        return

    posting_id, complaint = _resolve(
        str(message.chat_id), message.reply_to_message.message_id, reply.index
    )
    if complaint:
        await message.reply_html(complaint)
        return
    assert posting_id is not None

    with store.connect() as conn:
        row = store.get_posting(conn, posting_id)
        verdict = store.get_verdict(conn, posting_id)
    if row is None:
        await message.reply_html("That posting is no longer in the database.")
        return

    label = f"<b>{row['company']}</b> — {row['title']}"

    if reply.action == replies.UNKNOWN:
        await message.reply_html(
            f"Not sure what to do with that for {label}.\n"
            "Reply <code>yes</code>, <code>no</code>, or <code>later</code> — "
            "anything after it is passed through as an instruction."
        )
        return

    snooze_until = None
    if reply.action == replies.SNOOZE:
        snooze_until = dt.date.today() + dt.timedelta(days=config.snooze_days)

    with store.connect() as conn:
        store.record_decision(conn, posting_id, reply.action, reply.note,
                              message.message_id, snooze_until)

    posting = {"id": posting_id, "company": row["company"], "title": row["title"],
               "score": verdict["score"] if verdict else None}
    kb.log_decision(posting, reply.action, reply.note)
    learned = kb.append_note(row["company"], row["title"], reply.action, reply.note)
    tail = "\n<i>Noted in profile_kb.md.</i>" if learned else ""

    if reply.action == replies.SKIP:
        await message.reply_html(f"👌 Skipped {label}.{tail}")
        return
    if reply.action == replies.SNOOZE:
        await message.reply_html(
            f"⏳ Snoozed {label} until {snooze_until:%d %b}.{tail}")
        return

    # approve
    if not config.build_enabled:
        await message.reply_html(f"📝 {label}\n{BUILD_DISABLED_NOTE}{tail}")
        return

    builder = context.application.bot_data.get("builder")
    if builder is None:
        await message.reply_html(f"📝 {label}\nNo builder is wired up yet.{tail}")
        return

    # File the approval before queueing. The record is what makes the Targeted
    # topic a log of what was decided rather than of what happened to succeed,
    # so it has to survive a build that then fails — and it is a no-op unless
    # that topic is configured.
    await notifier.send_targeted(
        row["company"], row["title"], row["url"],
        verdict["score"] if verdict else None, reply.note)

    ahead = await builder.enqueue(posting_id, reply.note, message.message_id)
    if ahead:
        # Sent through the notifier rather than `reply_html` so it lands in the
        # Processing topic with the rest of the build's progress. Still one
        # message carrying the same text, so the chat-id-only case is unchanged.
        await notifier.send(
            f"🛠 Queued {label} — {ahead} build{'s' if ahead > 1 else ''} ahead.{tail}",
            reply_to=message.message_id, topic="processing_build")
    elif tail:
        # Nothing to announce about the queue, but the note that went into
        # profile_kb.md still has to be reported somewhere.
        await message.reply_html(tail.strip())
    # Otherwise stay quiet: the worker sends "🛠 Building …" within a second,
    # and two messages a second apart saying the same thing is just noise.


async def on_stray(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A message that is not a reply. Approvals must name their posting."""
    message = update.effective_message
    if message:
        await message.reply_html(
            "Reply <i>to a posting notification</i> to act on it — several can "
            "be in flight at once, so a bare message is ambiguous."
        )


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unfinished() -> list[tuple[str, str]]:
    """(status, "Company — Role") for every build not yet finished.

    While this process is alive these rows *are* the in-flight work: the queue
    is in memory and `store` only moves a row off 'queued'/'running' when the
    build ends. (At boot the same query means the opposite — see
    `Builder._recover` — because then no process owns them.)

    Never raises. Both callers are answering the user, and a database hiccup
    should degrade the answer, not swallow the command.
    """
    try:
        with store.connect() as conn:
            return [(row["status"], f"{row['company']} — {row['title']}")
                    for row in store.unfinished_builds(conn)]
    except Exception:
        log.exception("could not read the build queue")
        return []


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/status` — is it alive, and what has it been doing."""
    message = update.effective_message
    if message is None:
        return
    notifier: Notifier = context.application.bot_data["notifier"]

    tally = {"queued": 0, "running": 0}
    for status, _ in _unfinished():
        tally["running" if status == "running" else "queued"] += 1

    await message.reply_html(format_status(
        notifier.config,
        context.application.bot_data.get("last_cycle", {}),
        tally,
        context.application.bot_data.get("started_at"),
    ))


async def on_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/restart` — bring the watcher back up on the current config.

    Nothing supervises this process, so restarting means relaunching itself:
    `stop_running` unwinds the polling loop cleanly and `main` puts the new
    process in its place once it has. Config edits do not need this — they are
    re-read on every access — but a code change, a wedged worker, or a source
    module reloaded from disk does.

    A build in flight is killed by it, and a killed build has its folder erased
    by the next start's recovery. That is worth refusing over, so it is refused
    unless the user says `force`.
    """
    message = update.effective_message
    if message is None:
        return

    args = getattr(context, "args", None) or []
    force = any(arg.strip().lower() == "force" for arg in args)
    unfinished = _unfinished()

    if unfinished and not force:
        names = "\n".join(f"• {_escape(label)}" for _, label in unfinished)
        await message.reply_html(
            f"⚠️ <b>{len(unfinished)} build(s) in flight</b>\n{names}\n\n"
            "Restarting kills them, and a killed build has its folder erased.\n"
            "<i>Send <code>/restart force</code> to do it anyway.</i>"
        )
        return

    # Written before the confirmation is sent, and before the loop is asked to
    # stop: the answer to this command is sent by a different process, and a
    # file is the only thing that survives the gap.
    try:
        RESTART_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESTART_MARKER_PATH.write_text(json.dumps({
            "chat_id": str(message.chat_id),
            "requested_at": dt.datetime.now().isoformat(timespec="seconds"),
            "in_flight": [label for _, label in unfinished],
        }), encoding="utf-8")
    except Exception:
        # Worth continuing without: the restart still happens, the user just
        # does not get told it finished.
        log.exception("could not write the restart marker")

    count = len(unfinished)
    await message.reply_html(
        f"↩️ <b>Restarting</b> — {count} build{'' if count == 1 else 's'} in flight."
    )
    context.application.bot_data["restart"] = True
    context.application.stop_running()


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

async def _report_restart(app: Application) -> None:
    """Close the loop on a `/restart`, from the process that came back.

    The request was answered by a process that no longer exists, so the marker
    file on disk is the only thing that crossed the gap. It is removed whatever
    happens next: a marker that outlives its restart makes the following
    start — a reboot, a manual one — announce itself as a restart it never was.
    """
    if not RESTART_MARKER_PATH.exists():
        return
    try:
        marker = json.loads(RESTART_MARKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.warning("unreadable restart marker — reporting the restart anyway")
        marker = {}
    try:
        RESTART_MARKER_PATH.unlink()
    except OSError:
        log.exception("could not remove %s", RESTART_MARKER_PATH)

    notifier: Notifier = app.bot_data["notifier"]
    asked_from = str(marker.get("chat_id") or notifier.chat_id)
    if asked_from != str(notifier.chat_id):
        # The configured chat changed between the request and now, so whoever
        # asked is not on the other end of this bot any more.
        log.info("restart was requested from chat %s, now talking to %s — "
                 "not reporting", asked_from, notifier.chat_id)
        return
    await notifier.send_notice("👋 <b>Watcher back up.</b>")


async def _post_init(app: Application) -> None:
    """Start the build queue once the event loop exists.

    The worker task and the interrupted-build recovery both need a running
    loop, so neither can happen in `build_app`.
    """
    # Before recovery, so "back up" lands above the list of builds the restart
    # interrupted rather than after it.
    await _report_restart(app)
    builder = app.bot_data.get("builder")
    if builder is not None:
        await builder.start()


def build_app(notifier: Notifier) -> Application:
    config = notifier.config
    app = (Application.builder()
           .token(require_env("TELEGRAM_BOT_TOKEN"))
           .post_init(_post_init)
           .build())
    app.bot_data["notifier"] = notifier
    app.bot_data["started_at"] = dt.datetime.now()
    if config.build_enabled:
        app.bot_data["builder"] = Builder(notifier, config)

    chat_only = filters.Chat(int(notifier.chat_id))
    # Commands are answered from the General topic of a forum, and from an
    # ordinary chat — `is_topic_message` is unset for both. Inside a posting
    # topic they are left to `on_reply`, which knows which posting is meant;
    # `/status` there is far more likely to be a mis-sent reply than a request
    # for a report. Registered ahead of the message handlers so a command sent
    # as a reply is still read as a command.
    general = chat_only & ~filters.IS_TOPIC_MESSAGE
    app.add_handler(CommandHandler("status", on_status, filters=general))
    app.add_handler(CommandHandler("restart", on_restart, filters=general))
    app.add_handler(MessageHandler(chat_only & filters.REPLY & filters.TEXT, on_reply))
    app.add_handler(MessageHandler(chat_only & filters.TEXT & ~filters.COMMAND
                                   & ~filters.REPLY, on_stray))

    queue = app.job_queue
    assert queue is not None
    # first=10 so a restart proves itself quickly instead of going quiet for
    # the whole interval.
    queue.run_repeating(poll_job, interval=config.interval_minutes * 60, first=10)
    queue.run_daily(digest_job, time=local_time(config.digest_hour, 5))
    queue.run_daily(heartbeat_job, time=local_time(config.heartbeat_hour, 15))
    if config.kb_enabled:
        # `[kb] weekday` follows `date.weekday()` (0 = Monday), because that is
        # what Python means by a weekday everywhere else in this codebase.
        # JobQueue.run_daily counts 0 = Sunday, so it is shifted here rather
        # than leaving the config off by one day.
        queue.run_daily(kb_job, time=local_time(config.kb_hour, 25),
                        days=((config.kb_weekday + 1) % 7,))
    return app


async def _once(notifier: Notifier) -> None:
    await run_cycle(notifier)


def relaunch() -> None:
    """Put a fresh process in this one's place, same interpreter, same argv.

    Called only after `run_polling` has returned, so this process has already
    stopped talking to Telegram — two watchers polling the same bot at once get
    409s from `getUpdates`, and the overlap is entirely avoidable by ordering.

    `os.execv` is the obvious tool and is used everywhere it works. On Windows
    it goes through the CRT, which joins the arguments with spaces and quotes
    none of them, so a workspace cloned into a path containing a space would
    come back up with its own script path split across two arguments. Popen's
    list form quotes properly. It inherits stdio and creates no new console, so
    a watcher started under `pythonw` stays windowless and one started in a
    terminal keeps that terminal.
    """
    argv = [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    log.info("restarting: %s", " ".join(argv))
    if os.name == "nt":
        subprocess.Popen(argv, cwd=os.getcwd(), close_fds=False)
        return
    os.execv(argv[0], argv)  # never returns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true",
                        help="run a single poll/score/notify cycle and exit")
    parser.add_argument("--digest", action="store_true",
                        help="send the digest now and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    load_env()
    store.init_db()
    try:
        # No config argument on purpose: passing one pins it for the life of the
        # process, and this process is meant to stay up for weeks across edits
        # to config.toml. Called first anyway so a malformed file is reported
        # here, at startup, rather than mid-cycle.
        load_config()
        notifier = Notifier()
    except RuntimeError as exc:
        # Missing credentials is the expected first-run state, not a crash.
        print(f"{exc}\nCopy automation/.env.example to automation/.env and fill "
              "in both values.")
        return 2

    if args.once:
        asyncio.run(_once(notifier))
        return 0
    if args.digest:
        asyncio.run(notifier.send_digest())
        return 0

    log.info("watcher starting — polling every %d min",
             notifier.config.interval_minutes)
    app = build_app(notifier)
    app.run_polling(drop_pending_updates=True)
    # `/restart` is the only thing that ends run_polling without ending the
    # watcher; every other way out — Ctrl-C, a signal, a crash — leaves this
    # flag unset and falls straight through to the exit.
    if app.bot_data.get("restart"):
        relaunch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
