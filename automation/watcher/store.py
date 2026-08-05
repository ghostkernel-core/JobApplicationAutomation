"""SQLite persistence for the watcher.

This is the piece the workspace has never had: a record of which postings have
been *seen*, independent of which ones were applied to. Before this, the only
"have I dealt with this" state was the folder tree and the tracker workbook,
both written only after a completed run — so a posting that was looked at and
skipped left no trace and would resurface forever.

Schema is created on demand and migrated forward by `_MIGRATIONS`; the database
is disposable in the sense that deleting it costs you notification history, not
any application.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import DB_PATH, ensure_dirs
from .normalize import Posting

log = logging.getLogger("watcher.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id              TEXT PRIMARY KEY,
    loose_key       TEXT NOT NULL,
    source          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    source_job_id   TEXT,
    url             TEXT NOT NULL,
    canonical_url   TEXT NOT NULL,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT,
    country         TEXT,
    remote          INTEGER DEFAULT 0,
    posted_at       TEXT,
    first_seen_at   TEXT NOT NULL,
    description     TEXT,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_loose ON postings(loose_key);
CREATE INDEX IF NOT EXISTS idx_postings_seen  ON postings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_postings_url   ON postings(canonical_url);

CREATE TABLE IF NOT EXISTS verdicts (
    posting_id    TEXT PRIMARY KEY REFERENCES postings(id),
    score         INTEGER NOT NULL,
    verdict       TEXT NOT NULL,
    why_json      TEXT,
    gaps_json     TEXT,
    stop_and_ask  INTEGER DEFAULT 0,
    stop_reason   TEXT,
    model         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_score ON verdicts(score);

-- How many times the scorer has failed to judge a posting. A posting only ever
-- appears here while it is between attempts; a successful score clears the row.
CREATE TABLE IF NOT EXISTS score_attempts (
    posting_id  TEXT PRIMARY KEY REFERENCES postings(id),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id          TEXT NOT NULL REFERENCES postings(id),
    chat_id             TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    sent_at             TEXT NOT NULL
);
-- One digest message covers many postings, so the message id alone is not
-- unique. The posting is what must not be notified about twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_msg
    ON notifications(chat_id, telegram_message_id, posting_id);
CREATE INDEX IF NOT EXISTS idx_notif_posting ON notifications(posting_id);

CREATE TABLE IF NOT EXISTS decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id       TEXT NOT NULL REFERENCES postings(id),
    action           TEXT NOT NULL,
    note             TEXT,
    reply_message_id INTEGER,
    snooze_until     TEXT,
    decided_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_posting ON decisions(posting_id);

CREATE TABLE IF NOT EXISTS builds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_id   TEXT NOT NULL REFERENCES postings(id),
    status       TEXT NOT NULL,
    folder       TEXT,
    log_path     TEXT,
    detail       TEXT,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_builds_posting ON builds(posting_id);

CREATE TABLE IF NOT EXISTS source_health (
    source               TEXT PRIMARY KEY,
    last_ok_at           TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    disabled             INTEGER DEFAULT 0,
    notified_disabled    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Forward-only migrations, applied in order once each. Append, never edit.
_MIGRATIONS: list[str] = [
    # idx_notif_msg was unique on (chat_id, message_id) alone, which let only the
    # first posting of a digest be recorded. Databases created before this ran
    # already have the narrow index; CREATE ... IF NOT EXISTS in _SCHEMA will not
    # widen it, so drop and rebuild.
    """
    DROP INDEX IF EXISTS idx_notif_msg;
    CREATE UNIQUE INDEX idx_notif_msg
        ON notifications(chat_id, telegram_message_id, posting_id);
    """,
    # City, seniority band, and years-of-experience bar, so `--replay` and the
    # notifier can show what the filters saw. Added here rather than in _SCHEMA
    # even for fresh databases: _SCHEMA runs first and unconditionally, so a
    # column declared in both places would make this ALTER fail on every new db.
    """
    ALTER TABLE postings ADD COLUMN city TEXT DEFAULT '';
    ALTER TABLE postings ADD COLUMN level TEXT DEFAULT '';
    ALTER TABLE postings ADD COLUMN years_required INTEGER;
    """,
    # When a source switched itself off, so a poll can decide whether the retry
    # cooldown has elapsed. NULL on rows disabled before this migration; the
    # cooldown check reads that as "due now", so old installs recover on the
    # next poll rather than staying stuck forever.
    """
    ALTER TABLE source_health ADD COLUMN disabled_at TEXT;
    """,
    # Retry bookkeeping. `retry_attempts` counts failed probes since the source
    # went down and drives the exponential backoff, so a source that has been
    # dead for a week is probed daily rather than hourly. `parked` marks the
    # failures that retrying cannot fix — a moved endpoint, a withdrawn key —
    # which get no auto-retry at all and one escalated message instead.
    """
    ALTER TABLE source_health ADD COLUMN retry_attempts INTEGER DEFAULT 0;
    ALTER TABLE source_health ADD COLUMN parked INTEGER DEFAULT 0;
    ALTER TABLE source_health ADD COLUMN park_reason TEXT;
    """,
    # Language bar, contract type, and work arrangement, read out of the body by
    # `terms`. Stored rather than recomputed so the ping, the digest, and a later
    # `--replay` all show the same reading, and so a posting whose description is
    # later trimmed does not silently lose the facts that were shown about it.
    """
    ALTER TABLE postings ADD COLUMN languages TEXT DEFAULT '';
    ALTER TABLE postings ADD COLUMN contract TEXT DEFAULT '';
    ALTER TABLE postings ADD COLUMN arrangement TEXT DEFAULT '';
    """,
    # The Telegram message carrying a build's live step checklist. Kept in the
    # database rather than only in the running process because the build queue is
    # in memory: a restart strands every build it was running, and without the id
    # their checklists stay frozen at whatever step they had reached, reading as
    # still-in-progress for good. With it, boot can edit them to say so.
    """
    ALTER TABLE builds ADD COLUMN progress_message_id INTEGER;
    """,
]


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    # 30s, not the 5s default. The watcher, a manual `--dry-run`, and the reply
    # handler can all be live at once, and under WAL a writer only ever waits on
    # another writer — so the wait is short unless something is genuinely holding
    # a write transaction, and then giving up after five seconds just loses the
    # write instead.
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(_SCHEMA)
        applied = {
            row["key"] for row in conn.execute(
                "SELECT key FROM meta WHERE key LIKE 'migration:%'"
            )
        }
        for index, statement in enumerate(_MIGRATIONS):
            key = f"migration:{index}"
            if key in applied:
                continue
            conn.executescript(statement)
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, _now()))


# --------------------------------------------------------------------------
# postings
# --------------------------------------------------------------------------

def known_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    ids = list(ids)
    if not ids:
        return set()
    out: set[str] = set()
    # Chunked to stay under SQLite's variable limit on large first polls.
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id FROM postings WHERE id IN ({placeholders})", chunk
        )
        out.update(row["id"] for row in rows)
    return out


def recent_loose_keys(conn: sqlite3.Connection, days: int = 30) -> set[str]:
    """Employer+role keys seen recently, for cross-source collapsing.

    A role on a company's Greenhouse board and the same role mirrored on an
    aggregator have different URLs and therefore different fingerprints. This
    is what stops the second one arriving as a fresh notification.
    """
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT loose_key FROM postings WHERE first_seen_at >= ?", (cutoff,)
    )
    return {row["loose_key"] for row in rows}


def insert_posting(conn: sqlite3.Connection, posting: Posting) -> bool:
    """Insert if new. Returns True when this was the first sighting."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO postings
           (id, loose_key, source, provider, source_job_id, url, canonical_url,
            company, title, location, country, city, level, years_required,
            languages, contract, arrangement,
            remote, posted_at, first_seen_at, description, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            posting.fingerprint, posting.loose_key, posting.source, posting.provider,
            posting.source_job_id, posting.url, posting.canonical_url,
            posting.company, posting.title, posting.location, posting.country,
            posting.city, posting.level, posting.years_required,
            ", ".join(posting.languages), posting.contract, posting.arrangement,
            int(posting.remote),
            posting.posted_at.isoformat() if posting.posted_at else None,
            _now(), posting.description,
            json.dumps(posting.raw, ensure_ascii=False, default=str)[:200_000],
        ),
    )
    return cur.rowcount > 0


def update_description(conn: sqlite3.Connection, posting_id: str, text: str) -> None:
    conn.execute("UPDATE postings SET description = ? WHERE id = ?", (text, posting_id))


def get_posting(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()


def recent_postings(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM postings ORDER BY first_seen_at DESC LIMIT ?", (limit,)
    ))


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

# The single phrase the matcher puts in `gaps` when it could not judge a posting
# at all. It is the only marker that survives into the database, so it is what
# `degraded_verdict_ids` matches on and what the matcher's fallback must use
# verbatim — a real scoring result never produces it.
DEGRADED_GAP = "scoring failed — judge from the posting"


def save_verdict(conn: sqlite3.Connection, posting_id: str, verdict: dict[str, Any],
                 model: str) -> None:
    conn.execute(
        """INSERT INTO verdicts
           (posting_id, score, verdict, why_json, gaps_json, stop_and_ask,
            stop_reason, model, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(posting_id) DO UPDATE SET
             score=excluded.score, verdict=excluded.verdict,
             why_json=excluded.why_json, gaps_json=excluded.gaps_json,
             stop_and_ask=excluded.stop_and_ask, stop_reason=excluded.stop_reason,
             model=excluded.model, created_at=excluded.created_at""",
        (
            posting_id,
            int(verdict.get("score", 0)),
            str(verdict.get("verdict", "no")),
            json.dumps(verdict.get("why", []), ensure_ascii=False),
            json.dumps(verdict.get("gaps", []), ensure_ascii=False),
            int(bool(verdict.get("stop_and_ask"))),
            verdict.get("stop_reason"),
            model,
            _now(),
        ),
    )


def get_verdict(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verdicts WHERE posting_id = ?", (posting_id,)
    ).fetchone()


def unnotified_in_band(conn: sqlite3.Connection, low: int, high: int | None = None
                       ) -> list[sqlite3.Row]:
    """Scored postings in a score band that have never been messaged about."""
    upper = high if high is not None else 10_000
    return list(conn.execute(
        """SELECT p.*, v.score, v.verdict, v.why_json, v.gaps_json,
                  v.stop_and_ask, v.stop_reason
           FROM postings p JOIN verdicts v ON v.posting_id = p.id
           WHERE v.score >= ? AND v.score < ?
             AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.posting_id = p.id)
           ORDER BY v.score DESC""",
        (low, upper),
    ))


def unscored(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT * FROM postings
           WHERE NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.posting_id = postings.id)
           ORDER BY first_seen_at DESC"""
    ))


def degraded_verdict_ids(conn: sqlite3.Connection) -> list[str]:
    """Postings whose stored verdict is the scorer's "I could not judge this".

    Matched on the gaps phrase rather than the score, because 45/`maybe` is a
    perfectly ordinary result that a real scoring pass can also produce.
    """
    marker = json.dumps([DEGRADED_GAP], ensure_ascii=False)
    return [row["posting_id"] for row in conn.execute(
        "SELECT posting_id FROM verdicts WHERE gaps_json = ?", (marker,)
    )]


def clear_degraded_verdicts(conn: sqlite3.Connection) -> int:
    """Drop those verdicts so the next cycle re-scores them. Returns the count.

    The attempt counter goes with them: these rows predate the retry logic, or
    were written after the retries ran out, and either way the posting deserves
    a clean set of attempts rather than being re-buried on the first failure.
    """
    return clear_verdicts(conn, degraded_verdict_ids(conn))


def clear_verdicts(conn: sqlite3.Connection,
                   posting_ids: Sequence[str]) -> int:
    """Drop these verdicts so the next cycle re-scores them.

    Deleting the verdict *is* the re-queue: `unscored()` selects on the absence
    of one, so there is no "needs re-scoring" flag to keep in step with it.

    The attempt counter goes with them. Whatever the caller's reason for
    throwing a verdict away, it is a reason the posting was never fairly judged,
    and starting it at two of three attempts would re-bury it on the first
    failure.
    """
    ids = list(dict.fromkeys(posting_ids))
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM verdicts WHERE posting_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM score_attempts WHERE posting_id IN ({marks})", ids)
    return len(ids)


def forget_notifications(conn: sqlite3.Connection, posting_ids: Sequence[str],
                         kind: str | None = None) -> int:
    """Forget that these postings were messaged about, so they can be again.

    The bulk sibling of `forget_notification`, and it carries the same warning:
    `unnotified_in_band` is the only thing standing between the user and a
    second ping for every posting here, so a caller that clears more than it
    means to sends a chat full of duplicates. Narrow it with `kind`.
    """
    ids = list(dict.fromkeys(posting_ids))
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    sql = f"DELETE FROM notifications WHERE posting_id IN ({marks})"
    params: list[object] = list(ids)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    return conn.execute(sql, params).rowcount


# --------------------------------------------------------------------------
# scoring attempts
# --------------------------------------------------------------------------

def bump_score_attempt(conn: sqlite3.Connection, posting_id: str,
                       error: str | None = None) -> int:
    """Record one failed scoring attempt and return the new total."""
    conn.execute(
        """INSERT INTO score_attempts (posting_id, attempts, last_error, updated_at)
           VALUES (?, 1, ?, ?)
           ON CONFLICT(posting_id) DO UPDATE SET
             attempts = score_attempts.attempts + 1,
             last_error = excluded.last_error,
             updated_at = excluded.updated_at""",
        (posting_id, error, _now()),
    )
    row = conn.execute(
        "SELECT attempts FROM score_attempts WHERE posting_id = ?", (posting_id,)
    ).fetchone()
    return int(row["attempts"]) if row else 1


def clear_score_attempts(conn: sqlite3.Connection, posting_id: str) -> None:
    conn.execute("DELETE FROM score_attempts WHERE posting_id = ?", (posting_id,))


# --------------------------------------------------------------------------
# notifications / decisions / builds
# --------------------------------------------------------------------------

def record_notification(conn: sqlite3.Connection, posting_id: str, chat_id: str,
                        message_id: int, kind: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO notifications
           (posting_id, chat_id, telegram_message_id, kind, sent_at)
           VALUES (?,?,?,?,?)""",
        (posting_id, str(chat_id), int(message_id), kind, _now()),
    )


def sent_notifications(conn: sqlite3.Connection, kind: str = "instant",
                       min_score: int = 0) -> list[sqlite3.Row]:
    """Recorded notifications, newest first, with the posting they announced."""
    return list(conn.execute(
        """SELECT n.rowid AS rowid, n.posting_id, n.chat_id,
                  n.telegram_message_id, n.sent_at,
                  p.company, p.title, v.score
           FROM notifications n
           JOIN postings p ON p.id = n.posting_id
           LEFT JOIN verdicts v ON v.posting_id = n.posting_id
           WHERE n.kind = ? AND COALESCE(v.score, 0) >= ?
           ORDER BY n.telegram_message_id DESC""",
        (kind, min_score),
    ))


def forget_notification(conn: sqlite3.Connection, rowid: int) -> None:
    """Drop one notification record, making its posting sendable again.

    `unnotified_in_band` excludes anything with a notifications row, so a ping
    whose Telegram message no longer exists is otherwise unreachable forever —
    the record says it was delivered and the chat says it was not. Deleting the
    record is the only way back, and it is deliberately not something the poll
    loop can do on its own.
    """
    conn.execute("DELETE FROM notifications WHERE rowid = ?", (rowid,))


def posting_for_message(conn: sqlite3.Connection, chat_id: str,
                        message_id: int) -> str | None:
    row = conn.execute(
        """SELECT posting_id FROM notifications
           WHERE chat_id = ? AND telegram_message_id = ?""",
        (str(chat_id), int(message_id)),
    ).fetchone()
    return row["posting_id"] if row else None


def postings_for_message(conn: sqlite3.Connection, chat_id: str,
                         message_id: int) -> list[str]:
    """All postings a message covered, in the order they were listed.

    A digest is one message covering many postings, so `build 3` needs the
    third one. Insertion order is the display order.
    """
    rows = conn.execute(
        """SELECT posting_id FROM notifications
           WHERE chat_id = ? AND telegram_message_id = ? ORDER BY id""",
        (str(chat_id), int(message_id)),
    )
    return [row["posting_id"] for row in rows]


def record_decision(conn: sqlite3.Connection, posting_id: str, action: str,
                    note: str = "", reply_message_id: int | None = None,
                    snooze_until: dt.date | None = None) -> None:
    conn.execute(
        """INSERT INTO decisions
           (posting_id, action, note, reply_message_id, snooze_until, decided_at)
           VALUES (?,?,?,?,?,?)""",
        (posting_id, action, note, reply_message_id,
         snooze_until.isoformat() if snooze_until else None, _now()),
    )


def last_decision(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM decisions WHERE posting_id = ? ORDER BY id DESC LIMIT 1",
        (posting_id,),
    ).fetchone()


def queue_build(conn: sqlite3.Connection, posting_id: str) -> int:
    """Record the intent to build, before the queue gets to it.

    Separate from `start_build` so a crash between approval and spawn is
    visible: the row sits at 'queued' and startup recovery reports it, rather
    than the approval vanishing without trace.
    """
    cur = conn.execute(
        """INSERT INTO builds (posting_id, status, started_at)
           VALUES (?, 'queued', ?)""",
        (posting_id, _now()),
    )
    return int(cur.lastrowid)


def start_build(conn: sqlite3.Connection, posting_id: str, log_path: str) -> int:
    cur = conn.execute(
        """INSERT INTO builds (posting_id, status, log_path, started_at)
           VALUES (?, 'running', ?, ?)""",
        (posting_id, log_path, _now()),
    )
    return int(cur.lastrowid)


def mark_build_running(conn: sqlite3.Connection, build_id: int, log_path: str) -> None:
    conn.execute(
        "UPDATE builds SET status = 'running', log_path = ?, started_at = ? WHERE id = ?",
        (log_path, _now(), build_id),
    )


def set_build_progress_message(conn: sqlite3.Connection, build_id: int,
                               message_id: int | None) -> None:
    """Remember which Telegram message carries this build's step checklist."""
    conn.execute(
        "UPDATE builds SET progress_message_id = ? WHERE id = ?",
        (message_id, build_id),
    )


def unfinished_builds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Builds left mid-flight by a crash or a kill.

    Nothing survives a restart — the queue is in memory — so any row still
    'queued' or 'running' at boot is by definition abandoned.
    """
    return conn.execute(
        """SELECT b.*, p.company, p.title
             FROM builds b JOIN postings p ON p.id = b.posting_id
            WHERE b.status IN ('queued', 'running')
            ORDER BY b.id"""
    ).fetchall()


def finish_build(conn: sqlite3.Connection, build_id: int, status: str,
                 folder: str = "", detail: str = "") -> None:
    conn.execute(
        """UPDATE builds SET status = ?, folder = ?, detail = ?, finished_at = ?
           WHERE id = ?""",
        (status, folder, detail, _now(), build_id),
    )


def build_for_posting(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM builds WHERE posting_id = ? ORDER BY id DESC LIMIT 1",
        (posting_id,),
    ).fetchone()


# --------------------------------------------------------------------------
# source health
# --------------------------------------------------------------------------

def mark_source_ok(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        """INSERT INTO source_health
             (source, last_ok_at, last_error, consecutive_failures, disabled,
              notified_disabled, disabled_at)
           VALUES (?, ?, NULL, 0, 0, 0, NULL)
           ON CONFLICT(source) DO UPDATE SET
             last_ok_at=excluded.last_ok_at, last_error=NULL,
             consecutive_failures=0, disabled=0, notified_disabled=0,
             disabled_at=NULL, retry_attempts=0, parked=0, park_reason=NULL""",
        (source, _now()),
    )


def mark_source_failed(conn: sqlite3.Connection, source: str, error: str,
                       threshold: int, structural: bool = False) -> str | None:
    """Record a failure. Returns what this failure tripped, or None.

    Returns "parked" for a structural failure that has now exhausted the
    threshold, "disabled" for an ordinary one, and None when the source is still
    within its allowance or has already reported. Only the *first* trip returns
    a value — that guard is what keeps a source failing its retry probe from
    re-announcing itself on every cooldown.

    A structural failure still has to clear the threshold rather than parking on
    the first sighting. A single 404 can be one bad deploy on the far end; three
    in a row is an endpoint that moved.
    """
    conn.execute(
        """INSERT INTO source_health (source, last_error, consecutive_failures)
           VALUES (?, ?, 1)
           ON CONFLICT(source) DO UPDATE SET
             last_error=excluded.last_error,
             consecutive_failures=source_health.consecutive_failures + 1""",
        (source, error[:500]),
    )
    row = conn.execute(
        "SELECT consecutive_failures, disabled FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if not row or row["consecutive_failures"] < threshold or row["disabled"]:
        return None
    conn.execute(
        """UPDATE source_health
           SET disabled = 1, disabled_at = ?, parked = ?, park_reason = ?
           WHERE source = ?""",
        (_now(), 1 if structural else 0, error[:500] if structural else None,
         source),
    )
    return "parked" if structural else "disabled"


def park_source(conn: sqlite3.Connection, source: str, reason: str) -> bool:
    """Park an already-disabled source that has since failed structurally.

    A source can go down transiently, get disabled, and only then have its
    endpoint disappear — at which point continuing to probe it hourly is the
    exact waste parking exists to stop. Returns True if this changed anything,
    so the caller knows whether to escalate.
    """
    row = conn.execute(
        "SELECT parked FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    if not row or row["parked"]:
        return False
    conn.execute(
        "UPDATE source_health SET parked = 1, park_reason = ? WHERE source = ?",
        (reason[:500], source),
    )
    return True


def bump_source_cooldown(conn: sqlite3.Connection, source: str) -> None:
    """Restart the retry clock after a retry probe failed again.

    Each failed probe also advances `retry_attempts`, which widens the next
    cooldown — see `retry_due_at`.
    """
    conn.execute(
        """UPDATE source_health
           SET disabled_at = ?, retry_attempts = retry_attempts + 1
           WHERE source = ?""",
        (_now(), source),
    )


def retry_attempts(conn: sqlite3.Connection, source: str) -> int:
    """Failed retry probes since this source went down."""
    row = conn.execute(
        "SELECT retry_attempts FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    return int(_column(row, "retry_attempts", 0) or 0) if row else 0


def is_source_parked(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT parked FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    return bool(row and _column(row, "parked", 0))


def is_source_disabled(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT disabled FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    return bool(row and row["disabled"])


def _column(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    """Read a column that may predate the migration that added it."""
    return row[name] if name in row.keys() else default


def row_is_parked(row: sqlite3.Row | None) -> bool:
    """Whether a health row is parked, tolerating pre-migration rows."""
    return bool(row is not None and _column(row, "parked", 0))


def backoff_minutes(attempts: int, base_minutes: int, factor: float,
                    max_minutes: int) -> int:
    """Cooldown for the next probe after `attempts` failed ones.

    Exponential and capped: with the defaults (60 min, x2, 24 h) a source is
    probed after 1 h, 2 h, 4 h, 8 h, 16 h, then daily. A source that recovers
    quickly is still picked up within the hour, while one that has been dead for
    a week stops spending 24 pointless requests a day to confirm it.
    """
    if base_minutes <= 0:
        return 0
    delay = base_minutes * (max(factor, 1.0) ** max(0, attempts))
    return int(min(delay, max(max_minutes, base_minutes)))


def retry_due_at(row: sqlite3.Row | None, retry_after_minutes: int,
                 backoff_factor: float = 1.0, max_minutes: int = 0,
                 ) -> dt.datetime | None:
    """When a disabled source is next eligible for a retry probe.

    None means "never": the row is not disabled, auto-recovery is switched off
    with `retry_after_minutes <= 0`, or the source is parked. A parked source
    failed in a way retrying cannot fix, so it waits for `--reset` after a
    person has changed the fetcher.

    A disabled row with no `disabled_at` (written before that column existed, or
    by a hand-edited db) is due immediately.
    """
    if row is None or not row["disabled"] or retry_after_minutes <= 0:
        return None
    if _column(row, "parked", 0):
        return None
    stamp = _column(row, "disabled_at")
    if not stamp:
        return dt.datetime.now()
    cooldown = backoff_minutes(
        int(_column(row, "retry_attempts", 0) or 0),
        retry_after_minutes, backoff_factor, max_minutes)
    try:
        return dt.datetime.fromisoformat(stamp) + dt.timedelta(minutes=cooldown)
    except ValueError:
        return dt.datetime.now()


def retry_is_due(conn: sqlite3.Connection, source: str,
                 retry_after_minutes: int, backoff_factor: float = 1.0,
                 max_minutes: int = 0) -> bool:
    """True when a disabled source has sat out its cooldown and may be probed."""
    row = conn.execute(
        "SELECT * FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    due = retry_due_at(row, retry_after_minutes, backoff_factor, max_minutes)
    return due is not None and dt.datetime.now() >= due


def source_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM source_health ORDER BY source"))


def reset_source(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        """UPDATE source_health
           SET disabled = 0, consecutive_failures = 0, notified_disabled = 0,
               disabled_at = NULL, retry_attempts = 0, parked = 0,
               park_reason = NULL
           WHERE source = ?""",
        (source,),
    )


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO meta(key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )


# --------------------------------------------------------------------------
# the last cycle
# --------------------------------------------------------------------------

LAST_CYCLE_KEY = "last_cycle"


def save_cycle(conn: sqlite3.Connection, stats: dict[str, Any]) -> None:
    """Record what the poll cycle just did, for `/status` to read back.

    In the database rather than in `bot_data` because the interesting version
    of "when did it last fetch" is the one that survives a restart. Held only
    in memory, a watcher that came back up thirty seconds ago answered "none
    yet this run" — which is indistinguishable, on a phone, from one that has
    not polled in a day.
    """
    set_meta(conn, LAST_CYCLE_KEY, json.dumps(stats, ensure_ascii=False))


def last_cycle(conn: sqlite3.Connection) -> dict[str, Any]:
    """The stats `save_cycle` wrote, or {} if it has never run.

    A malformed value reads as "never": this feeds a status report, and one
    bad row should cost the cycle line, not the whole answer.
    """
    raw = get_meta(conn, LAST_CYCLE_KEY)
    if not raw:
        return {}
    try:
        stats = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ignoring unreadable %s in meta", LAST_CYCLE_KEY)
        return {}
    return stats if isinstance(stats, dict) else {}


# --------------------------------------------------------------------------
# the whole picture, for a status report
# --------------------------------------------------------------------------

def snapshot(conn: sqlite3.Connection, notify_threshold: int,
             digest_threshold: int) -> dict[str, Any]:
    """Every count `/status` needs, in one pass.

    Gathered here rather than in the notifier so the report is a formatting
    problem and this is a query problem, and so that `watcherctl status` and
    the Telegram command cannot drift into counting different things.

    The band counts deliberately exclude anything already messaged about:
    "17 waiting" has to mean seventeen postings the user has not seen, or the
    number is just a restatement of how long the watcher has been running.
    """
    def count(sql: str, *params: Any) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    today = dt.date.today().isoformat()
    scored = count("SELECT COUNT(*) FROM verdicts")
    out: dict[str, Any] = {
        "postings": count("SELECT COUNT(*) FROM postings"),
        "scored": scored,
        "pending": count(
            """SELECT COUNT(*) FROM postings p
               WHERE NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.posting_id = p.id)"""),
        # Only the postings that will genuinely be tried again. An attempt row
        # outlives the retry loop: `_persist` stops deleting it once the
        # attempts run out, so a raw count of the table keeps calling postings
        # "retrying" long after the matcher gave up on them. The verdict row is
        # what settles it — a posting still in the loop has none, which is
        # exactly why `unscored()` will re-select it. Twenty-five postings sat
        # at "25 retrying · 25 unjudged" after one outage, the two halves of
        # that line contradicting each other about the same postings.
        "retrying": count(
            """SELECT COUNT(*) FROM score_attempts a
               WHERE NOT EXISTS (SELECT 1 FROM verdicts v
                                 WHERE v.posting_id = a.posting_id)"""),
        "seen_today": count(
            "SELECT COUNT(*) FROM postings WHERE substr(first_seen_at, 1, 10) = ?",
            today),
        "scored_today": count(
            "SELECT COUNT(*) FROM verdicts WHERE substr(created_at, 1, 10) = ?",
            today),
        "notified": count("SELECT COUNT(*) FROM notifications"),
        "notified_today": count(
            "SELECT COUNT(*) FROM notifications WHERE substr(sent_at, 1, 10) = ?",
            today),
        "decisions": count("SELECT COUNT(*) FROM decisions"),
        "builds": count("SELECT COUNT(*) FROM builds"),
        # What a threshold change would act on, which is the same query
        # `/recheck` runs — see `unnotified_in_band`.
        "waiting_ping": count(
            """SELECT COUNT(*) FROM postings p JOIN verdicts v ON v.posting_id = p.id
               WHERE v.score >= ?
                 AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.posting_id = p.id)""",
            notify_threshold),
        "waiting_digest": count(
            """SELECT COUNT(*) FROM postings p JOIN verdicts v ON v.posting_id = p.id
               WHERE v.score >= ? AND v.score < ?
                 AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.posting_id = p.id)""",
            digest_threshold, notify_threshold),
    }
    out["unjudged"] = len(degraded_verdict_ids(conn))

    row = conn.execute(
        "SELECT MAX(sent_at) AS at FROM notifications").fetchone()
    out["last_notified_at"] = row["at"] if row else None
    row = conn.execute("SELECT MAX(created_at) AS at FROM verdicts").fetchone()
    out["last_scored_at"] = row["at"] if row else None
    row = conn.execute(
        "SELECT MAX(first_seen_at) AS at FROM postings").fetchone()
    out["last_new_posting_at"] = row["at"] if row else None
    row = conn.execute(
        """SELECT score, company, title FROM verdicts v
           JOIN postings p ON p.id = v.posting_id
           ORDER BY v.created_at DESC, v.score DESC LIMIT 1""").fetchone()
    out["top_recent"] = (f"{row['company']} — {row['title']} ({row['score']})"
                         if row else None)
    return out
