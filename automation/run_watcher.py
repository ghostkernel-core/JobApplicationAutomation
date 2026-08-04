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
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from watcher import kb, matcher, poll, replies, store
from watcher.builder import Builder
from watcher.config import load_config, load_env, require_env
from watcher.logsetup import setup
from watcher.notifier import Notifier, format_kb_proposal

log = logging.getLogger("watcher.run")

BUILD_DISABLED_NOTE = (
    "Recorded. Builds are switched off (<code>[build] enabled = false</code> in "
    "config.toml), so nothing was started."
)


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
    await notifier.send_heartbeat(context.application.bot_data.get("last_cycle", {}))


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
    ahead = await builder.enqueue(posting_id, reply.note, message.message_id)
    if ahead:
        await message.reply_html(
            f"🛠 Queued {label} — {ahead} build{'s' if ahead > 1 else ''} ahead.{tail}")
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
# wiring
# --------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    """Start the build queue once the event loop exists.

    The worker task and the interrupted-build recovery both need a running
    loop, so neither can happen in `build_app`.
    """
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
    if config.build_enabled:
        app.bot_data["builder"] = Builder(notifier, config)

    chat_only = filters.Chat(int(notifier.chat_id))
    app.add_handler(MessageHandler(chat_only & filters.REPLY & filters.TEXT, on_reply))
    app.add_handler(MessageHandler(chat_only & filters.TEXT & ~filters.COMMAND
                                   & ~filters.REPLY, on_stray))

    queue = app.job_queue
    assert queue is not None
    # first=10 so a restart proves itself quickly instead of going quiet for
    # the whole interval.
    queue.run_repeating(poll_job, interval=config.interval_minutes * 60, first=10)
    queue.run_daily(digest_job, time=dt.time(hour=config.digest_hour, minute=5))
    queue.run_daily(heartbeat_job, time=dt.time(hour=config.heartbeat_hour, minute=15))
    if config.kb_enabled:
        # `[kb] weekday` follows `date.weekday()` (0 = Monday), because that is
        # what Python means by a weekday everywhere else in this codebase.
        # JobQueue.run_daily counts 0 = Sunday, so it is shifted here rather
        # than leaving the config off by one day.
        queue.run_daily(kb_job, time=dt.time(hour=config.kb_hour, minute=25),
                        days=((config.kb_weekday + 1) % 7,))
    return app


async def _once(notifier: Notifier) -> None:
    await run_cycle(notifier)


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
    build_app(notifier).run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
