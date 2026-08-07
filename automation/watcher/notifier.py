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

from . import roles, store, terms
from .config import (DB_PATH, TOPIC_KINDS, Config, clock, load_config,
                     require_env)

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


def _terms(row: sqlite3.Row) -> tuple[str, str, str]:
    """(arrangement, contract, languages) for one posting row.

    The stored columns win. When all three are blank the reading is recomputed
    from the description, because those columns arrived in a later migration and
    every posting already in the database has them empty — without the fallback
    the language bar would only ever appear on jobs found after the upgrade, and
    `/recheck` on an existing posting would show less than a fresh one.
    """
    langs = str(_field(row, "languages", "") or "").strip()
    contract = str(_field(row, "contract", "") or "").strip()
    arrangement = str(_field(row, "arrangement", "") or "").strip()
    if langs or contract or arrangement:
        return arrangement, contract, langs

    description = str(_field(row, "description", "") or "")
    if not description:
        return "", "", ""
    title = str(_field(row, "title", "") or "")
    return (
        terms.arrangement(title, description,
                          remote_flag=bool(_field(row, "remote", 0))),
        terms.contract(title, description),
        ", ".join(terms.languages(title, description)),
    )


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
    arrangement, contract, langs = _terms(row)

    # City first, falling back to whatever coarser place the source gave. The
    # canonical city is the backstop rather than the preference: a source's own
    # "Köln, Nordrhein-Westfalen" says more than the one word `geo` resolves.
    where = (row["location"] or _field(row, "city", "") or row["country"]
             or "location unknown")
    if arrangement:
        where += f" · {arrangement}"
    elif row["remote"]:
        where += " · remote"

    lines = [
        f"{_band_icon(score, stop)} <b>{score}</b> · {_escape(row['title'])}",
        f"{_escape(row['company'])} — {_escape(where)} · {_escape(row['provider'])}",
    ]
    # Rank, the stated experience bar, and the contract, when the posting said
    # any of them. Shown rather than filtered on: an out-of-reach bar is
    # information for the reply, not grounds for never seeing the job.
    rank = roles.describe(_field(row, "level", "") or "", _field(row, "years_required"))
    facts = [p for p in (rank, contract) if p]
    if facts:
        lines.append(f"· {_escape(' · '.join(facts))}")
    # The language bar gets its own line and its own marker. It is the single
    # fact most likely to disqualify a posting outright and the one buried
    # deepest in the ad, so it must survive a glance at a notification preview.
    if langs:
        lines.append(f"🗣 {_escape(langs)}")
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


#: Misses named individually in the recall message. Beyond this the count is
#: still right; only the list is trimmed, because the message goes to a phone.
RECALL_TOP_MISSES = 5


def format_recall_audit(report: Any) -> str:
    """The weekly miss rate, as the one number and then where it came from.

    The per-stage table is not detail — it is the finding. "2 of 40 drops
    would have pinged" says something is miscalibrated; only the split says
    *what*, and the answers are opposite: misses concentrated in `triage`
    mean the prompt is too eager, misses in `location` mean the geography
    config is wrong and no prompt change will help.

    A `<pre>` block for the same reason `_source_funnel_lines` uses one —
    Telegram's default font is proportional and the columns only mean
    anything lined up.

    Ends by saying nothing was written. The message reads like a list of
    postings, and a list of postings in this chat has always been something
    to reply to; this one is not.
    """
    scored = int(getattr(report, "scored", 0) or 0)
    pinged = int(getattr(report, "would_ping", 0) or 0)
    rate = round((pinged / scored) * 100) if scored else 0

    lines = [
        f"🔎 <b>Recall audit</b> — {report.sampled} of {report.population:,} "
        f"drops re-scored",
        f"   would have pinged: <b>{pinged}</b> ({rate}%)  ·  "
        f"would have made the digest: {report.would_digest}",
    ]
    if report.no_body:
        lines.append(f"   no description available: {report.no_body}")

    rows = [(stage, counts) for stage, counts in report.by_stage.items()
            if counts.get("sampled")]
    if rows:
        rows.sort(key=lambda item: (-item[1]["sampled"], item[0]))
        width = min(max((len(stage) for stage, _ in rows), default=6), 12)
        table = [f"{stage[:width].ljust(width)}  {counts['sampled']:>4} → "
                 f"{counts['pinged']}" for stage, counts in rows]
        lines += ["", "<b>By stage (sampled → would have pinged):</b>",
                  "<pre>" + _escape("\n".join(table)) + "</pre>"]

    misses = list(report.misses or [])
    if misses:
        lines += ["", "<b>Top misses:</b>"]
        for miss in misses[:RECALL_TOP_MISSES]:
            lines.append(
                f"   <b>{miss['score']}</b>  {_escape(str(miss['company']))} — "
                f"{_escape(str(miss['title']))}   "
                f"<i>[{_escape(str(miss['stage']))}]</i>")
        if len(misses) > RECALL_TOP_MISSES:
            lines.append(f"   <i>+{len(misses) - RECALL_TOP_MISSES} more</i>")

    lines += ["", "<i>Nothing was written.</i>"]
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


def _ailing_sources(limit: int = 4) -> list[str]:
    """Names of the sources that are not currently fine, worst first.

    The counts from `_source_tally` answer "is anything wrong" but not "what",
    and the whole reason to ask from a phone is usually that something is.
    Capped because a bad afternoon can put every source on this list at once
    and the point is a hint, not an inventory — `watcherctl status` has them all.
    """
    try:
        with store.connect() as conn:
            rows = store.source_health(conn)
    except Exception:
        return []  # already logged by `_source_tally`, which reads the same table
    ailing: list[tuple[int, str]] = []
    for row in rows:
        if store.row_is_parked(row):
            rank, note = 0, "parked"
        elif row["disabled"]:
            rank, note = 1, "disabled"
        elif row["consecutive_failures"]:
            rank, note = 2, f"failing ×{row['consecutive_failures']}"
        else:
            continue
        ailing.append((rank, f"{row['source']} ({note})"))
    ailing.sort()
    return [label for _, label in ailing[:limit]]


def _moment(stamp: str | dt.datetime | None) -> dt.datetime | None:
    """A stored ISO timestamp as a datetime, or None if it is not one."""
    if isinstance(stamp, dt.datetime):
        return stamp
    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


def _span(seconds: float) -> str:
    """A duration in the largest unit that still says something useful."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    return f"{hours // 24}d {hours % 24}h"


def _when(stamp: str | dt.datetime | None, now: dt.datetime | None = None) -> str:
    """"14:32 (12m ago)" — a clock time and the distance to it.

    Both halves earn their place. The clock time is what gets compared against
    the schedule; the age is what answers "is this thing stuck", which reading
    a timestamp off a phone screen and doing the subtraction by hand does not.
    """
    moment = _moment(stamp)
    if moment is None:
        return "never"
    now = now or dt.datetime.now()
    delta = (now - moment).total_seconds()
    if delta < 0:  # clock moved, or a stamp from the future — say the time only
        return moment.strftime("%d %b %H:%M")
    shown = (moment.strftime("%H:%M:%S") if delta < 86400
             else moment.strftime("%d %b %H:%M"))
    return f"{shown} ({_span(delta)} ago)"


def _db_size() -> str:
    try:
        return f"{DB_PATH.stat().st_size / 1_048_576:.1f} MB"
    except OSError:
        return "missing"


#: How many source rows `/status` prints before it summarises the rest. Every
#: source is still counted in the totals above the block — this is only about
#: not turning a phone reply into a spreadsheet. `watcherctl status` has them all.
MAX_STATUS_SOURCES = 20


def funnel_rows(sources: dict[str, Any]) -> list[tuple[str, Any, ...]]:
    """Per-source funnel rows, most productive first.

    Sorted by what each source actually contributed rather than alphabetically,
    because the question this block exists to answer — "why is everything
    coming from one board" — is read off the top and the bottom of the list,
    not looked up by name.
    """
    rows = []
    for key, counts in sources.items():
        if not isinstance(counts, dict):
            continue
        rows.append((
            str(key),
            int(counts.get("fetched", 0) or 0),
            int(counts.get("already_known", 0) or 0),
            int(counts.get("filtered", 0) or 0),
            int(counts.get("stored", 0) or 0),
            str(counts.get("error", "") or ""),
        ))
    # Failures first — a source that could not be reached is the most
    # actionable line here — then by what reached the database, then volume.
    rows.sort(key=lambda r: (not r[5], -r[4], -r[1], r[0]))
    return rows


def _source_funnel_lines(cycle: dict[str, Any]) -> list[str]:
    """The funnel again, split by source, as a monospace block.

    A `<pre>` block rather than plain lines because the columns only mean
    anything lined up, and Telegram's default font is proportional — spaces in
    an ordinary message collapse into a ragged mess on a phone.
    """
    sources = cycle.get("sources")
    # Cycles recorded before this block existed have no per-source data. An
    # empty table would read as "no sources configured", so say nothing.
    if not isinstance(sources, dict) or not sources:
        return []
    rows = funnel_rows(sources)
    if not rows:
        return []

    shown, hidden = rows[:MAX_STATUS_SOURCES], rows[MAX_STATUS_SOURCES:]
    width = max(len(name) for name, *_ in shown)
    width = min(max(width, 6), 22)

    table = [f"{'source'.ljust(width)}  fetch   seen   filt    new"]
    for name, fetched, known, filtered, stored, error in shown:
        label = name if len(name) <= width else name[:width - 1] + "…"
        if error:
            # The counts would all be zero and would read as "this board has
            # nothing", which is the opposite of what happened.
            table.append(f"{label.ljust(width)}  failed — {error[:40]}")
            continue
        table.append(f"{label.ljust(width)}  {fetched:5d}  {known:5d}  "
                     f"{filtered:5d}  {stored:5d}")

    lines = ["", "<b>Per source:</b>",
             "<pre>" + _escape("\n".join(table)) + "</pre>"]
    if hidden:
        quiet = sum(1 for row in hidden if not row[4])
        lines.append(f"    <i>+{len(hidden)} more ({quiet} with nothing new) — "
                     f"watcherctl status lists them all</i>")
    return lines


def _cycle_lines(config: Config, cycle: dict[str, Any],
                 now: dt.datetime) -> list[str]:
    """The poll cycle: when, how long, and what happened to each listing.

    This is the block the whole report exists for. A watcher that has quietly
    stopped polling looks exactly like one with nothing to report unless the
    time of the last fetch is on the screen, and the funnel — fetched, already
    known, filtered, stored, scored — is what distinguishes "the boards have
    nothing new" from "everything is being dropped before it reaches scoring".
    """
    if not cycle:
        return ["<b>Last cycle:</b> none on record"]

    fetched = int(cycle.get("fetched", 0) or 0)
    known = int(cycle.get("already_known", 0) or 0)
    filtered = int(cycle.get("filtered", 0) or 0)
    stored = int(cycle.get("stored", 0) or 0)

    head = f"<b>Last cycle:</b> {_when(cycle.get('finished_at'), now)}"
    seconds = cycle.get("seconds")
    if seconds is not None:
        head += f" · took {_span(float(seconds))}"
    lines = [head]

    # Older records predate the wider stats; showing a funnel of zeros for the
    # three fields they lack would read as a broken poller rather than an
    # out-of-date row.
    if "already_known" in cycle:
        lines.append(f"    {fetched} fetched → {known} already seen → "
                     f"{filtered} filtered → <b>{stored} new</b>")
    else:
        lines.append(f"    {fetched} fetched → <b>{stored} new</b>")
    scored = cycle.get("scored")
    tail = [f"{scored} scored"] if scored is not None else []
    if cycle.get("deferred"):
        tail.append(f"{cycle['deferred']} deferred")
    tail.append(f"{int(cycle.get('notified', 0) or 0)} pinged")
    lines.append(f"    {' · '.join(tail)}")
    if cycle.get("sources_failed"):
        failed = ", ".join(str(s) for s in cycle["sources_failed"][:4])
        lines.append(f"    ⚠️ failed: {_escape(failed)}")

    started = _moment(cycle.get("started_at"))
    if started is not None:
        due = started + dt.timedelta(minutes=config.interval_minutes)
        overdue = (now - due).total_seconds()
        if overdue > 60:
            lines.append(f"    <b>Next cycle:</b> {due:%H:%M} — "
                         f"{_span(overdue)} overdue")
        else:
            lines.append(f"    Next cycle: {due:%H:%M} "
                         f"(in {_span(max(0, -overdue))})")
    return lines


def format_status(config: Config, cycle: dict[str, Any],
                  builds: dict[str, int], since: dt.datetime | None) -> str:
    """The watcher on a phone screen, for `/status`.

    Not `watcher.status`, which prints a 78-column terminal report of every
    source, URL and setting. That answers "what is this configured to do"; this
    answers "is it alive, is it still doing the work, and is anything stuck" —
    which is what someone reaching for their phone actually wants to settle.

    `cycle` is the in-memory record from this process, and it wins when there
    is one. With none — a watcher that came back up a minute ago — the last
    cycle is read from the database instead, so a fresh start reports the poll
    it did before the restart rather than the useless "none yet this run".
    """
    now = dt.datetime.now()
    snap: dict[str, Any] = {}
    try:
        with store.connect() as conn:
            if not cycle:
                cycle = store.last_cycle(conn)
            snap = store.snapshot(conn, config.notify_threshold,
                                  config.digest_threshold)
    except Exception:
        log.exception("could not read the status snapshot")

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

    lines = [f"📡 <b>Watcher</b> · up {_uptime(since)}", ""]
    lines += _cycle_lines(config, cycle, now)
    funnel = _source_funnel_lines(cycle)
    lines += funnel

    if snap:
        lines += [
            "",
            f"<b>Database:</b> {snap['postings']} postings · "
            f"{snap['scored']} scored · {snap['pending']} awaiting score"
            + (f" · {snap['retrying']} retrying" if snap["retrying"] else "")
            + (f" · {snap['unjudged']} unjudged" if snap["unjudged"] else ""),
            f"    Today: {snap['seen_today']} new · {snap['scored_today']} scored"
            f" · {snap['notified_today']} pinged",
            f"    Newest posting: {_when(snap['last_new_posting_at'], now)}",
            f"    Last score: {_when(snap['last_scored_at'], now)}",
            f"    Last ping: {_when(snap['last_notified_at'], now)}",
        ]
        if snap["unjudged"]:
            # "Unjudged" reads like a queue, and it is the opposite: these
            # postings are finished, parked at the fallback score because
            # scoring kept failing until the attempts ran out. Nothing picks
            # them up again on its own, however healthy the API is now, so the
            # count is only useful next to the one command that moves it.
            lines.append(f"    <i>{snap['unjudged']} unjudged — scoring failed "
                         f"and the retries ran out; /rescore re-queues them</i>")
        if snap.get("top_recent"):
            lines.append(f"    Latest verdict: {_escape(snap['top_recent'])}")
        lines += [
            "",
            f"<b>Unsent:</b> {snap['waiting_ping']} at ≥{config.notify_threshold}"
            f" · {snap['waiting_digest']} in the digest band",
        ]
        if snap["waiting_ping"]:
            lines.append("    <i>/recheck sends them</i>")

    lines += [
        "",
        f"<b>Sources:</b> {' · '.join(health)}",
    ]
    for label in _ailing_sources():
        lines.append(f"    ⚠️ {_escape(label)}")
    lines += [
        f"<b>Builds:</b> {build_line}"
        + (f" · {snap['builds']} all-time · {snap['decisions']} decisions"
           if snap else ""),
        "",
        f"Every {config.interval_minutes} min · digest "
        f"{clock(config.digest_at)} · heartbeat {clock(config.heartbeat_at)}",
        f"Ping at ≥{config.notify_threshold}, digest at "
        f"≥{config.digest_threshold} — <i>/threshold to change</i>",
        f"<i>{DB_PATH.name} · {_db_size()} · scoring on "
        f"{_escape(config.match_model)}</i>",
    ]
    if config.topics_enabled:
        lines.append(f"<i>Topics: {len(config.topics)} of "
                     f"{len(TOPIC_KINDS)} routed</i>")

    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_CHARS:
        # Telegram rejects an over-length message outright rather than trimming
        # it, so an unusually wide source table would cost the whole reply. The
        # funnel is the newest and least load-bearing part of the report; drop
        # it and answer the question `/status` is actually for.
        log.warning("status report was %d chars — dropping the source funnel",
                    len(text))
        text = "\n".join(line for line in lines if line not in funnel)
    return text


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

    async def edit(self, message_id: int, text: str) -> bool:
        """Rewrite an already-sent message in place. Never raises.

        There is no topic argument to match `send`'s: a message keeps the thread
        it was posted in, and `editMessageText` has no say in it. Whichever topic
        the original went to is where the edit lands.

        This is the live build checklist's only write path, and it runs on a
        timer for the whole length of a 40-minute build — so every way it can
        fail has to be survivable. A progress message is a convenience; the
        build behind it is not, and nothing here may end one.

        Returns whether the message is still there and worth editing again —
        *not* whether this edit changed anything. Only a message Telegram says it
        cannot find gives up its place:

        * *not modified* — nothing to do, but the message exists. True, unlogged,
          because the caller re-rendering to the same text is ordinary.
        * *not found* / *can't be edited* — gone, usually because someone cleared
          the topic. False, and the caller stops trying.
        * anything else, including rate limits and network trouble — True, so the
          next tick has another go. The tick is slow enough that backing off
          further would only mean a staler checklist.
        """
        from telegram.error import BadRequest

        try:
            await self._bot.edit_message_text(
                chat_id=self.chat_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        except BadRequest as exc:
            reason = str(exc).casefold()
            if "not modified" in reason:
                return True
            if "not found" in reason or "can't be edited" in reason:
                log.info("progress message %s is gone; no further edits", message_id)
                return False
            log.warning("could not edit message %s: %s", message_id, exc)
            return True
        except Exception as exc:
            log.warning("could not edit message %s: %s", message_id, exc)
            return True
        return True

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

    async def message_exists(self, message_id: int) -> bool:
        """Whether a message is still in the chat.

        Telegram has no "does this exist" call, so this asks for a no-op edit of
        the message's (absent) inline keyboard and reads the refusal. "Message is
        not modified" and "message can't be edited" both mean it is there;
        "message to edit not found" means it is gone. Nothing is changed either
        way — the messages this sends have no reply markup to begin with.
        """
        from telegram.error import BadRequest

        try:
            await self._bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=message_id, reply_markup=None)
        except BadRequest as exc:
            text = str(exc).casefold()
            if "not found" in text:
                return False
            return True
        except Exception:  # network trouble is not evidence of deletion
            return True
        return True

    async def forget_vanished(self, min_score: int = 0,
                              dry_run: bool = True) -> list[str]:
        """Re-enable pings whose Telegram message no longer exists.

        Clearing a chat — which is what happens when a group is reorganised into
        forum topics — deletes the messages but not the record of them, and the
        two disagreeing is a silent, permanent hole: the posting counts as
        notified, so no cycle and no `/recheck` will ever raise it again.

        Only records Telegram positively reports as missing are dropped, so a
        network failure mid-probe under-reports rather than resending something
        the user already has.
        """
        with store.connect() as conn:
            rows = store.sent_notifications(conn, "instant", min_score)

        vanished: list[tuple[int, str]] = []
        for row in rows:
            if await self.message_exists(int(row["telegram_message_id"])):
                continue
            vanished.append((
                int(row["rowid"]),
                f"{row['score'] or '?'} · {row['company']} — {row['title']}",
            ))

        if not dry_run and vanished:
            with store.connect() as conn:
                for rowid, _ in vanished:
                    store.forget_notification(conn, rowid)
        return [label for _, label in vanished]

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
            # A restart between the last poll and 09:15 leaves the caller with
            # nothing in memory, and "0 fetched, 0 new" is the one thing this
            # message must never say when the watcher is in fact fine.
            if not report:
                report = store.last_cycle(conn)
        broken = [row["source"] for row in health if row["disabled"]]
        lines = [
            "💚 <b>Watcher alive</b>",
            f"last poll: {report.get('fetched', 0)} fetched, "
            f"{report.get('stored', 0)} new, {report.get('notified', 0)} notified",
            f"sources: {len(health) - len(broken)} ok, {len(broken)} disabled",
        ]
        if report.get("finished_at"):
            lines.insert(2, f"           at {_when(report['finished_at'])}")
        if broken:
            lines.append("disabled: " + _escape(", ".join(broken)))
        await self.send_notice("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    """Maintenance entry point. The watcher itself never calls this."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="notifier maintenance")
    parser.add_argument(
        "--forget-vanished", action="store_true",
        help="re-enable pings whose Telegram message no longer exists")
    parser.add_argument(
        "--min-score", type=int, default=0,
        help="only consider pings at or above this score")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually forget them; without this it only reports")
    args = parser.parse_args(argv)

    if not args.forget_vanished:
        parser.print_help()
        return 2

    notifier = Notifier()
    vanished = asyncio.run(
        notifier.forget_vanished(args.min_score, dry_run=not args.apply))
    if not vanished:
        print("Every recorded ping is still in the chat. Nothing to do.")
        return 0

    verb = "Forgot" if args.apply else "Would forget"
    print(f"{verb} {len(vanished)} ping(s) whose message is gone:")
    for label in vanished:
        print(f"  {label}")
    if args.apply:
        print("\nThey are eligible again. They go out on the next cycle, or "
              "immediately with: python watcherctl.py poll")
    else:
        print("\nRe-run with --apply to make these sendable again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
