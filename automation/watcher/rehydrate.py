"""Re-fetch the bodies of postings that were stored as teasers.

`poll.py` used to hydrate only postings whose `description` came back empty.
stepstone and hiringcafe both ship the search tile's one-line snippet — around
300 characters, non-empty, and enough to satisfy that test — so their bodies
were never fetched, and 198 of the first 226 stored postings were prefiltered,
scored and reported on the opening sentence of the ad. The poll guard now
compares against `poll.TEASER_CHARS`; this module is the other half, applying
the same fix backwards over what is already in the database.

It is a maintenance sweep, not part of a cycle. Run it once and it should find
nothing to do ever again.

    .venv\\Scripts\\python.exe -m watcher.rehydrate --dry-run
    .venv\\Scripts\\python.exe -m watcher.rehydrate

**Resumable by construction.** A row that gets a real body no longer matches
the query that selects work, so an interrupted sweep picks up exactly where it
stopped. Rows that fail are remembered in `meta` and skipped on the next run,
because otherwise a posting that has been taken down would be re-fetched on
every attempt forever; `--retry-failed` clears that memory.

**Runs alongside the watcher.** Chromium locks its profile directory to one
process and the watcher holds `state/browser/` for as long as it is up, so this
sweep gets a copy of that profile — cookies and any solved challenge included —
and points the browser at it. See `config.browser_profile_dir()`.

The only providers listed here are the two that need it. Every other source
either sends the body with the search result or sets a `detail_url` that is not
the posting URL and was hydrated correctly at poll time; reconstructing those
API URLs from a stored row is guesswork this does not need to do.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, store
from .fetchers import hydrate
from .logsetup import setup
from .normalize import Posting
from .poll import TEASER_CHARS

log = logging.getLogger("watcher.rehydrate")

# provider -> how to rebuild `detail_url` from the stored row. For both of
# these the detail URL *is* the posting URL (stepstone.py:124, hiringcafe.py:91),
# which is why they can be recovered at all — `detail_url` itself is not a
# column.
DETAIL_URL = {
    "stepstone": lambda row: row["url"],
    "hiringcafe": lambda row: row["url"],
}

# Postings that could not be re-fetched, so a second run does not spend a page
# load on each of them again. Value is a JSON list of posting ids.
FAILED_KEY = "rehydrate:failed"

# Seconds between page loads. These are scraped portals being asked for two
# hundred pages in a row by a headless browser; there is no deadline here worth
# looking like a crawler for.
DEFAULT_DELAY = 4.0

# Where the copied browser profile lives. Kept rather than deleted after a run:
# it holds whatever challenge state the sweep accumulated, and a second run
# starting warm is worth a few dozen megabytes.
PROFILE_COPY = config.STATE_DIR / "browser-rehydrate"


@dataclass
class Report:
    considered: int = 0
    filled: int = 0
    failed: int = 0
    skipped: int = 0
    grew: list[tuple[str, int, int]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"considered {self.considered}", f"filled {self.filled}"]
        if self.failed:
            parts.append(f"failed {self.failed}")
        if self.skipped:
            parts.append(f"skipped {self.skipped}")
        if self.grew:
            before = sum(b for _, b, _ in self.grew)
            after = sum(a for _, _, a in self.grew)
            parts.append(f"{before // len(self.grew)} -> {after // len(self.grew)} chars avg")
        return ", ".join(parts)


# --------------------------------------------------------------------------
# the browser profile
# --------------------------------------------------------------------------

def seed_profile(source: Path, target: Path) -> None:
    """Copy the watcher's browser profile so this process can open one too.

    Best-effort on purpose. The source profile belongs to a live Chromium and
    some of its files are open; anything that cannot be read is skipped and
    named in the log rather than aborting the sweep. A profile missing a lock
    file or a cache shard still carries the cookie jar, which is the part that
    matters.
    """
    if target.exists():
        log.info("reusing browser profile at %s", target)
        return
    if not source.exists():
        log.info("no browser profile at %s — starting cold", source)
        return

    log.info("copying browser profile %s -> %s", source, target)
    try:
        shutil.copytree(source, target, dirs_exist_ok=True)
    except shutil.Error as exc:
        # copytree does not stop at the first unreadable file — it copies
        # everything it can and raises once at the end with the list.
        failures = exc.args[0] if exc.args else []
        names = sorted({Path(entry[0]).name for entry in failures})
        log.info("%d profile file(s) were in use and were skipped: %s",
                 len(names), ", ".join(names[:6]))


# --------------------------------------------------------------------------
# work selection
# --------------------------------------------------------------------------

def _failed_ids(conn: sqlite3.Connection) -> set[str]:
    raw = store.get_meta(conn, FAILED_KEY)
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (ValueError, TypeError):
        log.warning("%s is not readable JSON — treating it as empty", FAILED_KEY)
        return set()


def _remember_failure(conn: sqlite3.Connection, posting_id: str) -> None:
    ids = _failed_ids(conn)
    ids.add(posting_id)
    store.set_meta(conn, FAILED_KEY, json.dumps(sorted(ids)))


def candidates(conn: sqlite3.Connection, provider: str | None = None,
               limit: int | None = None) -> list[sqlite3.Row]:
    """Stored postings whose description is still a teaser."""
    providers = [provider] if provider else sorted(DETAIL_URL)
    placeholders = ",".join("?" * len(providers))
    sql = (f"SELECT * FROM postings "
           f"WHERE provider IN ({placeholders}) "
           f"  AND LENGTH(description) < ? "
           f"  AND COALESCE(url, '') <> '' "
           f"ORDER BY first_seen_at DESC")
    params: list[object] = [*providers, TEASER_CHARS]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def _posting_from_row(row: sqlite3.Row) -> Posting:
    """Enough of a Posting to hydrate and to re-read the derived columns.

    `posted_at` is left off deliberately: nothing downstream of here reads it,
    and re-parsing a date to throw it away is one more thing to get wrong.
    """
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except (ValueError, TypeError):
        raw = {}
    return Posting(
        source=row["source"],
        provider=row["provider"],
        source_job_id=row["source_job_id"] or "",
        url=row["url"],
        company=row["company"],
        title=row["title"],
        location=row["location"] or "",
        country=row["country"] or "",
        remote=bool(row["remote"]),
        description=row["description"] or "",
        detail_url=DETAIL_URL[row["provider"]](row),
        raw=raw,
    )


def _write_back(conn: sqlite3.Connection, posting_id: str, posting: Posting) -> None:
    """Store the body and everything that is read off it.

    The five derived columns are written in the same statement as the
    description because they are a *cache* of it — `insert_posting` fills them
    from exactly these properties, and leaving them behind would mean a ping
    quoting a seniority and a language bar taken from the teaser next to a body
    that says something else.
    """
    conn.execute(
        """UPDATE postings
              SET description = ?, level = ?, years_required = ?,
                  languages = ?, contract = ?, arrangement = ?
            WHERE id = ?""",
        (posting.description, posting.level, posting.years_required,
         ", ".join(posting.languages), posting.contract, posting.arrangement,
         posting_id),
    )


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

def run(dry_run: bool = False, provider: str | None = None,
        limit: int | None = None, delay: float = DEFAULT_DELAY,
        retry_failed: bool = False, timeout: int = 45) -> Report:
    report = Report()
    store.init_db()

    with store.connect() as conn:
        if retry_failed:
            store.set_meta(conn, FAILED_KEY, "[]")
        skip = set() if retry_failed else _failed_ids(conn)
        rows = candidates(conn, provider=provider, limit=limit)

    report.considered = len(rows)
    todo = [row for row in rows if row["id"] not in skip]
    report.skipped = len(rows) - len(todo)
    log.info("%d posting(s) still hold a teaser; %d to fetch, %d previously failed",
             len(rows), len(todo), report.skipped)
    if not todo:
        return report

    if not dry_run:
        seed_profile(config.BROWSER_PROFILE_DIR, PROFILE_COPY)

    for index, row in enumerate(todo, start=1):
        posting = _posting_from_row(row)
        before = len(posting.description)
        label = f"[{index}/{len(todo)}] {posting.company} — {posting.title}"

        if dry_run:
            log.info("%s: would fetch %s (%d chars now)", label,
                     posting.detail_url, before)
            continue

        try:
            posting.description = hydrate(posting, timeout)
        except Exception as exc:  # noqa: BLE001 — one dead page must not end the sweep
            log.warning("%s: hydrate raised %s: %s", label, type(exc).__name__, exc)
            posting.description = row["description"] or ""

        after = len(posting.description)
        # `hydrate` swallows its own failures and hands back what it was given,
        # so a length that did not move is the failure signal, not an exception.
        if after < TEASER_CHARS:
            report.failed += 1
            log.warning("%s: still %d chars — recording it as failed", label, after)
            with store.connect() as conn:
                _remember_failure(conn, row["id"])
            time.sleep(delay)
            continue

        with store.connect() as conn:
            _write_back(conn, row["id"], posting)
        report.filled += 1
        report.grew.append((row["id"], before, after))
        log.info("%s: %d -> %d chars, %s, %s", label, before, after,
                 posting.level or "level unknown",
                 ", ".join(posting.languages) or "no language stated")
        time.sleep(delay)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be fetched, touch nothing")
    parser.add_argument("--provider", choices=sorted(DETAIL_URL),
                        help="limit to one provider")
    parser.add_argument("--limit", type=int,
                        help="stop after this many postings")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"seconds between page loads (default {DEFAULT_DELAY})")
    parser.add_argument("--timeout", type=int, default=45,
                        help="per-page timeout in seconds (default 45)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="forget previous failures and try them again")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    # Set before anything opens the browser. The watcher owns state/browser/ for
    # as long as it runs, and two Chromium processes cannot share one profile.
    os.environ[config.BROWSER_PROFILE_ENV] = str(PROFILE_COPY)

    report = run(dry_run=args.dry_run, provider=args.provider, limit=args.limit,
                 delay=args.delay, retry_failed=args.retry_failed,
                 timeout=args.timeout)
    log.info("rehydrate: %s", report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
