"""Poll every configured source and record postings not seen before.

    python -m watcher.poll --dry-run          # show what would be stored
    python -m watcher.poll --source ats:Bayer # one source only
    python -m watcher.poll                    # real run, writes to the database

A poll never raises because of one bad source. Failures are recorded against
`source_health`; a fragile source that fails `failures_before_disable` times in
a row switches itself off and reports once, rather than retrying forever.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import Any

from . import store
from .config import Config, Sources, load_config, load_sources, source_key
from .fetchers import SourceNotImplemented, fetch_source, hydrate
from .logsetup import setup
from .normalize import Posting
from .prefilter import check

log = logging.getLogger("watcher.poll")


@dataclass
class PollReport:
    fetched: int = 0
    already_known: int = 0
    filtered: int = 0
    stored: int = 0
    new_postings: list[Posting] = field(default_factory=list)
    filtered_out: list[tuple[Posting, str]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    newly_disabled: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"fetched {self.fetched}",
            f"known {self.already_known}",
            f"filtered {self.filtered}",
            f"new {self.stored}",
        ]
        if self.errors:
            parts.append(f"errors {len(self.errors)}")
        if self.pending:
            parts.append(f"pending {len(self.pending)}")
        return ", ".join(parts)


def _collect(sources: Sources, config: Config, conn, only: str | None,
             report: PollReport, include_disabled: bool) -> list[Posting]:
    postings: list[Posting] = []
    for entry in sources.all_enabled():
        key = source_key(entry)
        if only and key != only:
            continue
        if not include_disabled and store.is_source_disabled(conn, key):
            log.info("%s is disabled after repeated failures — skipping", key)
            continue
        try:
            found = fetch_source(entry, config.http_timeout)
        except SourceNotImplemented as exc:
            report.pending.append(f"{key}: {exc}")
            log.info("%s: %s", key, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — one source must not end the poll
            message = f"{type(exc).__name__}: {exc}"
            report.errors[key] = message
            log.warning("%s failed: %s", key, message)
            if store.mark_source_failed(conn, key, message,
                                        config.failures_before_disable):
                report.newly_disabled.append(key)
                log.error("%s disabled after %d consecutive failures",
                          key, config.failures_before_disable)
            conn.commit()
            continue
        store.mark_source_ok(conn, key)
        # Commit each health update immediately. Without this the first source
        # to report opens a write transaction that is not committed until the
        # whole poll ends — and a poll spends minutes in network and browser I/O
        # between sources, so any other process trying to write in that window
        # (the matcher, mid-cycle) waits it out and eventually fails. Observed
        # exactly that: a scoring write lost to "database is locked".
        conn.commit()
        log.info("%s returned %d postings", key, len(found))
        postings.extend(found)
    return postings


def poll_once(dry_run: bool = False, only: str | None = None,
              config: Config | None = None, sources: Sources | None = None,
              ) -> PollReport:
    config = config or load_config()
    sources = sources or load_sources()
    report = PollReport()
    store.init_db()

    with store.connect() as conn:
        raw = _collect(sources, config, conn, only, report,
                       include_disabled=dry_run)
        report.fetched = len(raw)

        known = store.known_ids(conn, (p.fingerprint for p in raw))
        recent_loose = store.recent_loose_keys(conn, days=30)

        # Dedupe within this batch too: two sources can surface the same role in
        # a single poll, and neither is in the database yet.
        batch_ids: set[str] = set()
        batch_loose: set[str] = set()
        candidates: list[Posting] = []
        for posting in raw:
            if posting.fingerprint in known or posting.fingerprint in batch_ids:
                report.already_known += 1
                continue
            if posting.loose_key in recent_loose or posting.loose_key in batch_loose:
                report.already_known += 1
                continue
            batch_ids.add(posting.fingerprint)
            batch_loose.add(posting.loose_key)
            candidates.append(posting)

        for posting in candidates:
            verdict = check(posting, sources.defaults, config.max_age_days,
                            sources.filters)
            if not verdict.accepted:
                report.filtered += 1
                report.filtered_out.append((posting, verdict.reason))
                continue

            # Only now is it worth paying for a description.
            if posting.detail_url and not posting.description:
                try:
                    posting.description = hydrate(posting, config.http_timeout)
                except Exception as exc:  # noqa: BLE001
                    log.warning("hydrate failed for %s: %s", posting.summary(), exc)

            # Re-check: hard blockers usually live in the body, not the title.
            recheck = check(posting, sources.defaults, config.max_age_days,
                            sources.filters)
            if not recheck.accepted:
                report.filtered += 1
                report.filtered_out.append((posting, recheck.reason))
                continue

            report.new_postings.append(posting)
            if not dry_run and store.insert_posting(conn, posting):
                report.stored += 1

        if dry_run:
            report.stored = len(report.new_postings)
            conn.rollback()

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and filter, but write nothing to the database")
    parser.add_argument("--source", help="limit to one source, e.g. ats:Bayer")
    parser.add_argument("--show-filtered", action="store_true",
                        help="also list postings the prefilter rejected, with reasons")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    report = poll_once(dry_run=args.dry_run, only=args.source)

    for posting in report.new_postings:
        print(f"  NEW  {posting.summary()}")
        print(f"       {posting.canonical_url}")
    if args.show_filtered:
        for posting, reason in report.filtered_out:
            print(f"  skip {posting.summary()}  [{reason}]")
    for key, message in report.errors.items():
        print(f"  FAIL {key}: {message}")
    for note in report.pending:
        print(f"  todo {note}")

    print(("(dry run) " if args.dry_run else "") + report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
