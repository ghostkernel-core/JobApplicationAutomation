"""Telegram output: instant pings, the evening digest, and health notices.

Message formatting lives here; the reply handling that turns a message into a
decision lives in `run_watcher.py`, because it needs the running application.

One rule shapes everything below: a notification is only ever sent once per
posting. `store.unnotified_in_band` does the filtering and every send is
recorded, so a poll that re-surfaces the same role — or a restart mid-poll —
cannot produce a second ping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Sequence

from telegram import Bot
from telegram.constants import ParseMode

from . import roles, store
from .config import Config, load_config, require_env

log = logging.getLogger("watcher.notify")

MAX_INSTANT_PER_POLL = 6  # anything beyond this waits for the digest


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


def format_digest(rows: Sequence[sqlite3.Row]) -> str:
    lines = [f"📋 <b>{len(rows)} posting(s) worth a look</b>", ""]
    for index, row in enumerate(rows, start=1):
        where = row["location"] or row["country"] or "?"
        lines.append(
            f"{index}. <b>{int(row['score'])}</b> {_escape(row['title'])}\n"
            f"    {_escape(row['company'])} — {_escape(where)}\n"
            f"    {_escape(row['url'])}"
        )
    lines += ["", "<i>Reply to this message with “build 2” — or “2, add German”.</i>"]
    return "\n".join(lines)


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

    async def send(self, text: str, reply_to: int | None = None) -> int:
        message = await self._bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
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
                message_id = await self.send(format_instant(row))
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

        message_id = await self.send(format_digest(rows))
        with store.connect() as conn:
            for row in rows:
                store.record_notification(conn, row["id"], self.chat_id,
                                          message_id, "digest")
        return len(rows)

    async def send_notice(self, text: str) -> None:
        """Operational message — source failure, heartbeat, build result."""
        try:
            await self.send(text)
        except Exception as exc:
            log.error("failed to send notice: %s", exc)

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

    async def send_source_recovered(self, recovered: Sequence[str]) -> None:
        if not recovered:
            return
        names = "\n".join(f"• {_escape(name)}" for name in recovered)
        await self.send_notice(
            f"🟢 <b>Source recovered</b>\n{names}\n\n"
            "<i>Back in the rotation — no action needed.</i>"
        )

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
