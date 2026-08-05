"""Telegram output: instant pings, the evening digest, and health notices.

Message formatting lives here; the reply handling that turns a message into a
decision lives in `run_watcher.py`, because it needs the running application.

One rule shapes everything below: a notification is only ever sent once per
posting. `store.unnotified_in_band` does the filtering and every send is
recorded, so a poll that re-surfaces the same role — or a restart mid-poll —
cannot produce a second ping.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from typing import Any, Sequence

from telegram import Bot
from telegram.constants import ParseMode

from . import roles, store
from .config import TOPIC_KINDS, Config, load_config, require_env

log = logging.getLogger("watcher.notify")

MAX_INSTANT_PER_POLL = 6  # anything beyond this waits for the digest

TELEGRAM_MAX_CHARS = 4096  # hard API limit; a longer message is rejected, not trimmed
# Room for the header and footer. Packing decides how many parts there are and
# the part count decides the header's width, so the allowance is reserved up
# front rather than measured.
DIGEST_OVERHEAD = 320
DIGEST_ENTRY_MAX = TELEGRAM_MAX_CHARS - DIGEST_OVERHEAD


def _escape(text: str) -> str:
    """Escape for Telegram HTML parse mode."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _phrases(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _field(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    """Read a column that may not be in this query's SELECT.

    `sqlite3.Row` raises IndexError on an unknown key, and the level / years
    columns arrived in a later migration — a formatting helper must not be able
    to take down a notification over a column that is merely absent.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _band_icon(score: int, stop_and_ask: bool) -> str:
    if stop_and_ask:
        return "🟡"
    return "🟢" if score >= 75 else "🔵"


def format_instant(row: sqlite3.Row) -> str:
    """One posting, formatted for a phone screen.

    Deliberately compact: the header carries the decision (score, role, place),
    the ✓/⚠ lines carry the reasoning, and the URL is last so it does not push
    everything else off the preview.
    """
    score = int(row["score"])
    stop = bool(row["stop_and_ask"])
    where = row["location"] or row["country"] or "location unknown"
    if row["remote"]:
        where += " · remote"

    lines = [
        f"{_band_icon(score, stop)} <b>{score}</b> · {_escape(row['title'])}",
        f"{_escape(row['company'])} — {_escape(where)} · {_escape(row['provider'])}",
    ]
    # Rank and the stated experience bar, when the posting said either. Shown
    # rather than filtered on: an out-of-reach bar is information for the reply,
    # not grounds for never seeing the job.
    rank = roles.describe(_field(row, "level", "") or "", _field(row, "years_required"))
    if rank:
        lines.append(f"· {_escape(rank)}")
    why, gaps = _phrases(row["why_json"]), _phrases(row["gaps_json"])
    if why:
        lines.append(f"✓ {_escape('; '.join(why))}")
    if gaps:
        lines.append(f"⚠ {_escape('; '.join(gaps))}")
    if stop and row["stop_reason"]:
        lines.append(f"❗ Needs a decision: {_escape(row['stop_reason'])}")
    lines += [
        "",
        _escape(row["url"]),
        "",
        "<i>Reply: yes / no / later — extras like “yes, add German” pass through.</i>",
    ]
    return "\n".join(lines)


def _digest_entry(index: int, row: sqlite3.Row) -> str:
    where = row["location"] or row["country"] or "?"
    entry = (
        f"{index}. <b>{int(row['score'])}</b> {_escape(row['title'])}\n"
        f"    {_escape(row['company'])} — {_escape(where)}\n"
        f"    {_escape(row['url'])}"
    )
    if len(entry) > DIGEST_ENTRY_MAX:
        # Only the tail — company and URL — is cut. The one tag pair closes
        # around the score on the first line, so the HTML stays balanced.
        entry = entry[:DIGEST_ENTRY_MAX - 1] + "…"
    return entry


def _pack_digest(rows: Sequence[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Group rows into per-message batches that fit inside the size limit."""
    budget = TELEGRAM_MAX_CHARS - DIGEST_OVERHEAD
    batches: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    used = 0
    for row in rows:
        # Numbering restarts per message, so an entry's width follows its
        # position in the batch being filled, not its position overall.
        width = len(_digest_entry(len(current) + 1, row)) + 1
        if current and used + width > budget:
            batches.append(current)
            current, used = [], 0
            width = len(_digest_entry(1, row)) + 1
        current.append(row)
        used += width
    if current:
        batches.append(current)
    return batches


def format_digest_chunks(
    rows: Sequence[sqlite3.Row],
) -> list[tuple[str, list[sqlite3.Row]]]:
    """The digest as one or more messages, each with the rows it covers.

    The digest is unbounded — it carries the whole mid-band plus every high
    scorer the instant cap held back — while Telegram rejects anything over
    4096 characters outright. One night of backlog is a 21k-character message
    and a BadRequest, and because nothing is recorded when the send raises, the
    band keeps growing and every later digest is larger still. Splitting is
    what stops that from being permanent.

    Line numbers restart at 1 in each message on purpose: `build 3` resolves
    against the postings recorded for the message it replies to
    (`store.postings_for_message`), so continuous numbering across parts would
    send "build 17" looking for a seventeenth entry in a message that has six.
    """
    batches = _pack_digest(list(rows))
    total = sum(len(batch) for batch in batches)
    chunks: list[tuple[str, list[sqlite3.Row]]] = []
    for number, batch in enumerate(batches, start=1):
        head = f"📋 <b>{total} posting(s) worth a look</b>"
        if len(batches) > 1:
            head += f" — part {number}/{len(batches)}"
        lines = [head, ""]
        lines += [_digest_entry(i, row) for i, row in enumerate(batch, start=1)]
        footer = "<i>Reply to this message with “build 2” — or “2, add German”."
        if len(batches) > 1:
            footer += " Numbers count from 1 in each part, so reply to the part"
            footer += " the posting is in.</i>"
        else:
            footer += "</i>"
        lines += ["", footer]
        chunks.append(("\n".join(lines), batch))
    return chunks


def format_digest(rows: Sequence[sqlite3.Row]) -> str:
    """The first digest message. Use `format_digest_chunks` to send them all."""
    chunks = format_digest_chunks(rows)
    return chunks[0][0] if chunks else ""


def format_kb_proposal(proposal: dict[str, Any]) -> str:
    """The weekly consolidation, as something answerable in one glance.

    The `because` line is not decoration: approving a matching rule without
    seeing which decisions produced it is how a one-off "no, too much devops"
    becomes a standing filter nobody remembers agreeing to.
    """
    items = proposal.get("proposals") or []
    lines = [
        f"🧠 <b>Matching rules — {len(items)} proposed</b>",
        f"<i>from {proposal.get('reviewed', 0)} recent decision(s)</i>",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. <b>{_escape(item.get('section', '?'))}</b> — "
                     f"{_escape(item.get('text', ''))}")
        if item.get("because"):
            lines.append(f"    <i>{_escape(item['because'])}</i>")
    if proposal.get("summary"):
        lines += ["", _escape(str(proposal["summary"]))]
    lines += [
        "",
        "<i>Reply “yes” to add all of them to profile_kb.md, “no” to discard. "
        "For anything in between, edit the file directly.</i>",
    ]
    return "\n".join(lines)


def format_targeted(company: str, title: str, url: str, score: int | None,
                    note: str, when: dt.datetime | None = None) -> str:
    """An approved posting, written into the Targeted Build topic as a record.

    Deliberately self-contained rather than a copy of the original ping. The
    ping lives in another topic and says nothing about the approval; what makes
    this row worth keeping is the decision — when it was taken and with what
    instruction — beside the link it was taken on.
    """
    stamp = (when or dt.datetime.now()).strftime("%d %b %H:%M")
    lines = [f"✅ <b>{_escape(company)}</b> — {_escape(title)}"]
    detail = f"approved {stamp}"
    if score is not None:
        detail = f"score {score} · {detail}"
    lines.append(f"<i>{detail}</i>")
    if note:
        lines.append(f"📝 {_escape(note)}")
    if url:
        lines.append(_escape(url))
    return "\n".join(lines)


def _uptime(since: dt.datetime | None) -> str:
    if since is None:
        return "unknown"
    seconds = max(0, int((dt.datetime.now() - since).total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h"
    minutes = rest // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _source_tally() -> tuple[int, int, int, int] | None:
    """(ok, failing, disabled, parked) across every source with health on record.

    None when the database cannot be read. `/status` is answered on demand and
    is often the thing someone reaches for *because* something is wrong, so one
    unreadable line has to cost that line and nothing else.
    """
    ok = failing = disabled = parked = 0
    try:
        with store.connect() as conn:
            rows = store.source_health(conn)
    except Exception:
        log.exception("could not read source health")
        return None
    for row in rows:
        if row["disabled"]:
            if store.row_is_parked(row):
                parked += 1
            else:
                disabled += 1
        elif row["consecutive_failures"]:
            failing += 1
        else:
            ok += 1
    return ok, failing, disabled, parked


def format_status(config: Config, cycle: dict[str, int],
                  builds: dict[str, int], since: dt.datetime | None) -> str:
    """The watcher on a phone screen, for `/status`.

    Not `watcher.status`, which prints a 78-column terminal report of every
    source, URL and setting. That answers "what is this configured to do"; this
    answers "is it alive and what has it been doing", which is the question
    someone asks from their phone.
    """
    tally = _source_tally()
    if tally is None:
        health = ["unreadable — see watcher.log"]
    else:
        ok, failing, disabled, parked = tally
        health = [f"{ok} ok"]
        if failing:
            health.append(f"{failing} failing")
        if disabled:
            health.append(f"{disabled} disabled")
        if parked:
            health.append(f"{parked} parked")

    running = builds.get("running", 0)
    queued = builds.get("queued", 0)
    if not config.build_enabled:
        build_line = "off — approvals recorded, nothing built"
    elif running or queued:
        build_line = f"{running} running, {queued} queued"
    else:
        build_line = "idle"

    lines = [
        f"📡 <b>Watcher</b> · up {_uptime(since)}",
        f"Last cycle: {cycle.get('fetched', 0)} fetched · "
        f"{cycle.get('stored', 0)} new · {cycle.get('notified', 0)} pinged"
        if cycle else "Last cycle: none yet this run",
        f"Sources: {' · '.join(health)}",
        f"Builds: {build_line}",
        f"Every {config.interval_minutes} min · digest "
        f"{config.digest_hour:02d}:00 · heartbeat {config.heartbeat_hour:02d}:00",
        f"Ping at ≥{config.notify_threshold}, digest at "
        f"≥{config.digest_threshold}",
    ]
    if config.topics_enabled:
        lines.append(f"<i>Topics: {len(config.topics)} of "
                     f"{len(TOPIC_KINDS)} routed</i>")
    return "\n".join(lines)


class Notifier:
    def __init__(self, config: Config | None = None) -> None:
        # Held as an override rather than a snapshot: with none passed, `config`
        # resolves live on every access, so an edit to config.toml reaches a
        # watcher that has been up for weeks. An explicit Config still pins the
        # value, which is what the CLI entry points and tests want.
        self._config_override = config
        self.chat_id = require_env("TELEGRAM_CHAT_ID")
        self._bot = Bot(require_env("TELEGRAM_BOT_TOKEN"))

    @property
    def config(self) -> Config:
        return self._config_override or load_config()

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send(self, text: str, reply_to: int | None = None,
                   topic: str | None = None) -> int:
        """Send one message, routed to `topic`'s thread if one is configured.

        `topic` is a kind from `TOPIC_KINDS`, not a thread id — the mapping is
        config's to own, so an unconfigured kind quietly resolves to None and
        the message lands in General exactly as it always did.

        The reply is dropped when a thread is chosen, and only then. Telegram
        rejects `reply_to_message_id` outright when the message being replied to
        lives in a different topic, and it is the whole send that fails, not
        just the threading — so a build report would vanish rather than arrive
        unthreaded. Every message that carries a reply also names its posting in
        the text, which is what identifies it once the two are separated.
        """
        thread = self.config.topic_for(topic)
        message = await self._bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
            message_thread_id=thread,
            reply_to_message_id=None if thread is not None else reply_to,
            disable_web_page_preview=True,
        )
        return message.message_id

    async def send_instant(self, limit: int = MAX_INSTANT_PER_POLL) -> int:
        """Ping for high-scoring postings not yet messaged about."""
        with store.connect() as conn:
            rows = store.unnotified_in_band(conn, self.config.notify_threshold)
        if not rows:
            return 0

        overflow = rows[limit:]
        sent = 0
        for row in rows[:limit]:
            try:
                message_id = await self.send(format_instant(row),
                                             topic="new_posting")
            except Exception as exc:  # network, rate limit, bad markup
                log.error("failed to notify %s (%s): %s", row["company"], row["id"], exc)
                continue
            with store.connect() as conn:
                store.record_notification(conn, row["id"], self.chat_id,
                                          message_id, "instant")
            sent += 1
        if overflow:
            # Left unrecorded on purpose: they stay in the band and go out with
            # the digest rather than being silently dropped.
            log.info("%d high scorer(s) held back for the digest", len(overflow))
        return sent

    async def send_digest(self) -> int:
        """The evening round-up: mid-band plus any instant overflow."""
        with store.connect() as conn:
            rows = store.unnotified_in_band(conn, self.config.digest_threshold)
        if not rows:
            log.info("nothing for the digest")
            return 0

        chunks = format_digest_chunks(rows)
        sent = 0
        for number, (text, batch) in enumerate(chunks, start=1):
            try:
                # Same topic as an instant ping: a digest line is a posting the
                # user has not seen yet, only a quieter one.
                message_id = await self.send(text, topic="new_posting")
            except Exception as exc:  # network, rate limit, bad markup
                # Recorded per part, so a rejected one costs only its own rows:
                # the rest still go out, and these stay in the band for the next
                # digest instead of being marked sent or lost.
                log.error("digest part %d/%d failed: %s", number, len(chunks), exc)
                continue
            with store.connect() as conn:
                for row in batch:
                    store.record_notification(conn, row["id"], self.chat_id,
                                              message_id, "digest")
            sent += len(batch)
        if len(chunks) > 1:
            log.info("digest sent as %d part(s)", len(chunks))
        return sent

    async def send_notice(self, text: str, topic: str | None = None) -> None:
        """Operational message — source failure, heartbeat, build result.

        Untopiced by default, which puts source alerts, the heartbeat and the
        interrupted-build notice in General: none of them is about one posting,
        and General is where a reply reaches the watcher.
        """
        try:
            await self.send(text, topic=topic)
        except Exception as exc:
            log.error("failed to send notice: %s", exc)

    async def send_targeted(self, company: str, title: str, url: str,
                            score: int | None, note: str) -> None:
        """File an approved posting in the Targeted Build topic.

        A no-op unless that topic is configured, and deliberately so: this
        message has no equivalent in the chat-id-only setup, and posting it
        there would add traffic to a chat whose behaviour is supposed to be
        untouched by this feature. The record only makes sense as a topic.

        Failure is logged and swallowed. Losing the record is a nuisance; losing
        the build it was recording, because the record failed to send first,
        would be the actual problem.
        """
        if self.config.topic_for("targeted_build") is None:
            return
        try:
            await self.send(format_targeted(company, title, url, score, note),
                            topic="targeted_build")
        except Exception as exc:
            log.error("failed to file %s — %s in the targeted topic: %s",
                      company, title, exc)

    async def send_source_alerts(self, disabled: Sequence[str]) -> None:
        if not disabled:
            return
        names = "\n".join(f"• {_escape(name)}" for name in disabled)
        cooldown = self.config.retry_after_minutes
        tail = (f"Retrying automatically in ~{cooldown} min. Sooner with: "
                "python -m watcher.health --reset &lt;source&gt;"
                if cooldown > 0 else
                "Auto-recovery is off. Re-enable with: "
                "python -m watcher.health --reset &lt;source&gt;")
        await self.send_notice(
            f"🔴 <b>Source disabled after repeated failures</b>\n{names}\n\n"
            f"<i>{tail}</i>"
        )

    async def send_source_parked(
            self, parked: Sequence[tuple[str, str]]) -> None:
        """Escalation for a source no retry can fix.

        Deliberately louder and more specific than the disable notice: this one
        is a work item, not weather. The error is included because it is the
        whole diagnosis — usually a status code and the URL that produced it —
        and because nothing will send it again.
        """
        if not parked:
            return
        lines = "\n".join(
            f"• {_escape(name)}\n  <code>{_escape(error[:180])}</code>"
            for name, error in parked)
        await self.send_notice(
            f"🟠 <b>Source parked — needs a code change</b>\n{lines}\n\n"
            "<i>The endpoint moved, was withdrawn, or stopped serving JSON. "
            "Retrying reproduces it exactly, so this source is not being "
            "probed. Fix the fetcher, then: "
            "python -m watcher.health --reset &lt;source&gt;</i>"
        )

    async def send_source_recovered(self, recovered: Sequence[str]) -> None:
        if not recovered:
            return
        names = "\n".join(f"• {_escape(name)}" for name in recovered)
        await self.send_notice(
            f"🟢 <b>Source recovered</b>\n{names}\n\n"
            "<i>Back in the rotation — no action needed.</i>"
        )

    async def send_scoring_degraded(self, report: Any) -> None:
        """Say so when the scorer could not judge some of this cycle's postings.

        Without this, a broken scorer and a quiet job market look identical from
        the outside: both are silence. They are not the same thing, and the one
        time they were confused it cost 49 buried postings.
        """
        if not getattr(report, "failed", 0):
            return
        deferred, exhausted = report.deferred, report.exhausted
        lines = [f"🟡 <b>Scoring degraded</b> — {report.failed} posting(s) could "
                 f"not be judged this cycle."]
        if deferred:
            lines.append(f"• {deferred} will be re-scored automatically next cycle.")
        if exhausted:
            lines.append(
                f"• {exhausted} ran out of attempts and are parked at 45/maybe — "
                "below the notify threshold. Recover them with: "
                "<code>python watcherctl.py rescore</code>")
        lines.append("\n<i>Usually a transient upstream failure. Check "
                     "watcher.log for the reason.</i>")
        await self.send_notice("\n".join(lines))

    async def send_heartbeat(self, report: dict[str, Any]) -> None:
        """Daily proof of life, so silence is distinguishable from a crash."""
        with store.connect() as conn:
            health = store.source_health(conn)
        broken = [row["source"] for row in health if row["disabled"]]
        lines = [
            "💚 <b>Watcher alive</b>",
            f"last poll: {report.get('fetched', 0)} fetched, "
            f"{report.get('stored', 0)} new, {report.get('notified', 0)} notified",
            f"sources: {len(health) - len(broken)} ok, {len(broken)} disabled",
        ]
        if broken:
            lines.append("disabled: " + _escape(", ".join(broken)))
        await self.send_notice("\n".join(lines))
