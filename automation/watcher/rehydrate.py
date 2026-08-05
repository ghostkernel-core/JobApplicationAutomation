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

**A body arriving throws the old verdict away.** The first version of this
module replaced the description and stopped there, which fixed the record and
left the damage: 185 postings kept scores that had been read off three hundred
characters of preamble, and the sweep's own log said `filled 88` as though the
job were done. `_write_back` now clears the verdict in the same transaction, so
the next cycle re-scores the posting against what it actually says.

That leaves the rows filled before this was true. `--rescore-before` is the
one-off for them — it fetches nothing, and takes the cutoff from the operator
because no row records when its body arrived:

    .venv\\Scripts\\python.exe -m watcher.rehydrate --rescore-before 2026-08-05T15:18:21 --dry-run

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
    deferred: int = 0
    skipped: int = 0
    grew: list[tuple[str, int, int]] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"considered {self.considered}", f"filled {self.filled}"]
        if self.failed:
            parts.append(f"failed {self.failed}")
        if self.deferred:
            parts.append(f"deferred {self.deferred}")
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
    """Store the body, everything read off it, and drop the teaser's verdict.

    The five derived columns are written in the same statement as the
    description because they are a *cache* of it — `insert_posting` fills them
    from exactly these properties, and leaving them behind would mean a ping
    quoting a seniority and a language bar taken from the teaser next to a body
    that says something else.

    The verdict is the same argument carried one step further, and it was
    missed the first time round. A score is read off the description too, so a
    row that keeps its old verdict through a twenty-fold change of body is
    worse than one that keeps a stale seniority: it is a judgement of an ad
    nobody had read, and it looks exactly like a judgement of the real thing.
    Dropping it is the whole re-score — `store.unscored()` selects on the
    absence of a verdict, so the next cycle picks the posting up by itself.
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
    store.clear_verdicts(conn, [posting_id])


# How long to keep trying a write before giving the row up for this run. A poll
# cycle holds its write transaction across minutes of network and browser I/O,
# which is longer than `store.connect`'s own 30s busy timeout, so a sweep that
# treats "database is locked" as fatal dies the first time it overlaps one — it
# did, at posting 105 of 196.
_WRITE_ATTEMPTS = 5
_WRITE_BACKOFF_S = (5, 15, 30, 60)


def _write_with_retry(posting_id: str, posting: Posting) -> bool:
    """Write one posting back, waiting out a poll cycle if it has to.

    Returns False once it has stopped trying. That is deliberately *not* a
    failure of the posting: the row still holds its teaser, so it matches the
    work query and the next run picks it up. Recording it as failed would
    blame the ad for a lock held by this process's own watcher.
    """
    for attempt in range(_WRITE_ATTEMPTS):
        try:
            with store.connect() as conn:
                _write_back(conn, posting_id, posting)
            return True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == _WRITE_ATTEMPTS - 1:
                log.warning("write failed for %s: %s", posting_id, exc)
                return False
            wait = _WRITE_BACKOFF_S[attempt]
            log.info("database busy — retrying the write in %ds", wait)
            time.sleep(wait)
    return False


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
            try:
                with store.connect() as conn:
                    _remember_failure(conn, row["id"])
            except sqlite3.OperationalError as exc:
                # Losing the note costs one re-fetch on the next run. It is not
                # worth waiting out a poll cycle for, the way a body is.
                log.warning("could not record the failure: %s", exc)
            time.sleep(delay)
            continue

        if not _write_with_retry(row["id"], posting):
            report.deferred += 1
            log.warning("%s: fetched %d chars but could not store them — "
                        "left for the next run", label, after)
            time.sleep(delay)
            continue
        report.filled += 1
        report.grew.append((row["id"], before, after))
        log.info("%s: %d -> %d chars, %s, %s", label, before, after,
                 posting.level or "level unknown",
                 ", ".join(posting.languages) or "no language stated")
        time.sleep(delay)

    return report


# --------------------------------------------------------------------------
# the back catalogue
# --------------------------------------------------------------------------

@dataclass
class Rescore:
    verdicts: int = 0
    pings: int = 0
    kept_instant: int = 0
    kept_decided: int = 0

    def summary(self) -> str:
        parts = [f"{self.verdicts} verdict(s) dropped",
                 f"{self.pings} digest ping(s) forgotten"]
        if self.kept_instant:
            parts.append(f"{self.kept_instant} already pinged, left alone")
        if self.kept_decided:
            parts.append(f"{self.kept_decided} already decided, left alone")
        return ", ".join(parts)


def scored_on_a_teaser(conn: sqlite3.Connection, cutoff: str,
                       provider: str | None = None) -> list[sqlite3.Row]:
    """Postings holding a verdict older than the body it was meant to judge.

    There is no per-row record of when a body arrived, which is why the caller
    supplies the cutoff instead of this working it out: the sweep that filled
    these postings logged when it finished, and any verdict written before that
    was written against a teaser. Nothing was scored in the hours before it, so
    the line falls in a gap rather than through the middle of a batch.
    """
    providers = [provider] if provider else sorted(DETAIL_URL)
    marks = ",".join("?" * len(providers))
    return list(conn.execute(
        f"""SELECT p.id, p.company, p.title, p.provider, v.score, v.verdict,
                   (SELECT n.kind FROM notifications n
                     WHERE n.posting_id = p.id ORDER BY n.id LIMIT 1) AS notified,
                   EXISTS (SELECT 1 FROM decisions d
                            WHERE d.posting_id = p.id) AS decided
              FROM postings p JOIN verdicts v ON v.posting_id = p.id
             WHERE p.provider IN ({marks})
               AND LENGTH(p.description) >= ?
               AND v.created_at < ?
             ORDER BY v.score DESC""",
        (*providers, TEASER_CHARS, cutoff),
    ))


def rescore_before(cutoff: str, provider: str | None = None,
                   dry_run: bool = False) -> Rescore:
    """Re-queue the verdicts that a teaser produced, and unmute the quiet ones.

    Two separate things, because the postings fall into three groups and only
    one of them wants both:

    * Never messaged about — dropping the verdict is enough. If the real body
      scores high the next cycle pings, which is the ordinary path.
    * Digested — the score put them in the round-up rather than in front of the
      user, and that score is the one now known to be wrong. `unnotified_in_band`
      excludes anything with a notifications row, so re-scoring alone would
      correct the record and change nothing the user ever sees. These get the
      digest row dropped too.
    * Pinged, or already decided on — left exactly as they are. The user has
      seen the posting; a second ping for a score that moved is noise, and
      re-offering something they skipped is worse than noise.
    """
    store.init_db()
    with store.connect() as conn:
        rows = scored_on_a_teaser(conn, cutoff, provider=provider)
        report = Rescore()
        unmute: list[str] = []
        for row in rows:
            if row["decided"]:
                report.kept_decided += 1
            elif row["notified"] == "digest":
                unmute.append(row["id"])
            elif row["notified"]:
                report.kept_instant += 1

        log.info("%d posting(s) scored before %s now hold a full body",
                 len(rows), cutoff)
        for row in rows[:10]:
            log.info("  %s/%s %s — %s (%s)", row["score"], row["verdict"],
                     row["company"], row["title"], row["notified"] or "not sent")
        if len(rows) > 10:
            log.info("  ... and %d more", len(rows) - 10)

        if dry_run:
            report.verdicts = len(rows)
            report.pings = len(unmute)
            log.info("dry run — nothing was changed")
            return report

        report.verdicts = store.clear_verdicts(conn, [r["id"] for r in rows])
        report.pings = store.forget_notifications(conn, unmute, kind="digest")
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
    parser.add_argument("--rescore-before", metavar="TIMESTAMP",
                        help="re-queue postings whose verdict predates this "
                             "ISO timestamp and which now hold a full body; "
                             "fetches nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)

    if args.rescore_before:
        # Its own mode. Nothing here touches the network, so it does not want a
        # browser profile, a delay, or any of the sweep's other machinery.
        report = rescore_before(args.rescore_before, provider=args.provider,
                                dry_run=args.dry_run)
        log.info("rescore: %s", report.summary())
        return 0

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
