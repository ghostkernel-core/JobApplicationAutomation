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
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DB_PATH, ensure_dirs
from .normalize import Posting

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
            remote, posted_at, first_seen_at, description, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            posting.fingerprint, posting.loose_key, posting.source, posting.provider,
            posting.source_job_id, posting.url, posting.canonical_url,
            posting.company, posting.title, posting.location, posting.country,
            posting.city, posting.level, posting.years_required, int(posting.remote),
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
             disabled_at=NULL""",
        (source, _now()),
    )


def mark_source_failed(conn: sqlite3.Connection, source: str, error: str,
                       threshold: int) -> bool:
    """Record a failure. Returns True when this failure trips the disable.

    Only the *first* trip returns True — the guard below is what keeps a source
    that fails its hourly retry probe from re-announcing itself every hour.
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
    if row and row["consecutive_failures"] >= threshold and not row["disabled"]:
        conn.execute(
            "UPDATE source_health SET disabled = 1, disabled_at = ? WHERE source = ?",
            (_now(), source),
        )
        return True
    return False


def bump_source_cooldown(conn: sqlite3.Connection, source: str) -> None:
    """Restart the retry clock after a retry probe failed again."""
    conn.execute(
        "UPDATE source_health SET disabled_at = ? WHERE source = ?",
        (_now(), source),
    )


def is_source_disabled(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT disabled FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    return bool(row and row["disabled"])


def retry_due_at(row: sqlite3.Row | None, retry_after_minutes: int
                 ) -> dt.datetime | None:
    """When a disabled source is next eligible for a retry probe.

    None means "never" — either the row is not disabled, or auto-recovery is
    switched off with `retry_after_minutes <= 0`. A disabled row with no
    `disabled_at` (written before that column existed, or by a hand-edited db)
    is due immediately.
    """
    if row is None or not row["disabled"] or retry_after_minutes <= 0:
        return None
    stamp = row["disabled_at"] if "disabled_at" in row.keys() else None
    if not stamp:
        return dt.datetime.now()
    try:
        return dt.datetime.fromisoformat(stamp) + dt.timedelta(
            minutes=retry_after_minutes)
    except ValueError:
        return dt.datetime.now()


def retry_is_due(conn: sqlite3.Connection, source: str,
                 retry_after_minutes: int) -> bool:
    """True when a disabled source has sat out its cooldown and may be probed."""
    row = conn.execute(
        "SELECT * FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    due = retry_due_at(row, retry_after_minutes)
    return due is not None and dt.datetime.now() >= due


def source_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM source_health ORDER BY source"))


def reset_source(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        """UPDATE source_health
           SET disabled = 0, consecutive_failures = 0, notified_disabled = 0,
               disabled_at = NULL
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
