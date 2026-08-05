"""Poll every configured source and record postings not seen before.

    python -m watcher.poll --dry-run          # show what would be stored
    python -m watcher.poll --source ats:Bayer # one source only
    python -m watcher.poll                    # real run, writes to the database

A poll never raises because of one bad source. Failures are recorded against
`source_health`, and what happens next depends on how the source failed:

  transient (5xx, 429, timeout)   retried in-cycle by the HTTP layer, then
                                  counted. Enough of them in a row disables the
                                  source; it is probed again after a cooldown
                                  that doubles each time it fails, and a probe
                                  that succeeds re-enables it and says so.

  structural (4xx, non-JSON)      the endpoint moved or the key was withdrawn.
                                  Probing reproduces it exactly, so the source
                                  is *parked*: no auto-retry at all, and one
                                  escalated message naming the error. It comes
                                  back with `watcher.health --reset` once
                                  someone has fixed the fetcher.

How much failure a source is allowed before either of those depends on its
tier — see `failures_allowed` and `fragile` in sources.toml.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from typing import Any

from . import store
from .config import Config, Sources, load_config, load_sources, source_key
from .fetchers import SourceNotImplemented, fetch_source, hydrate
from .fetchers.base import StructuralError
from .logsetup import setup
from .normalize import Posting
from .prefilter import check

log = logging.getLogger("watcher.poll")

# A description shorter than this is treated as a teaser and re-fetched from the
# detail page, provided the source offered one.
#
# The guard here used to be `not posting.description`, which is only correct for
# a source that sends nothing at all. stepstone and hiringcafe both ship the
# search tile's one-line snippet — non-empty, around 300 characters, and enough
# to satisfy an emptiness test — so their `hydrate()` functions never ran once,
# and the two largest sources in the rotation (198 of the first 225 postings)
# were prefiltered, scored, and reported on the opening sentence of the ad.
# Everything downstream that reads the body was reading that sentence: the
# seniority and years-of-experience bar, the language requirement, the hard
# blockers the re-check exists to catch, and the matcher's own 4000-char window.
#
# 800 is comfortably above the longest observed teaser (476) and far below the
# shortest real ad body (~2900).
TEASER_CHARS = 800


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
    # (source, error) — parked sources get the error in the alert, because the
    # whole point of the escalation is that someone has to read it and act.
    newly_parked: list[tuple[str, str]] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
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
        # A disabled source is not off for good: once its cooldown has elapsed
        # it gets exactly one probe, and the next one only after another,
        # longer cooldown, whether that probe worked or not. A *parked* source
        # is the exception — `retry_is_due` never says yes for one, because it
        # failed in a way no probe can fix.
        retrying = False
        if store.is_source_disabled(conn, key):
            if include_disabled:
                pass
            elif store.retry_is_due(conn, key, config.retry_after_minutes,
                                    config.retry_backoff_factor,
                                    config.retry_backoff_max_minutes):
                retrying = True
                log.info("%s is disabled — retrying after cooldown", key)
            else:
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
            structural = isinstance(exc, StructuralError)
            report.errors[key] = message
            log.warning("%s failed: %s", key, message)
            allowance = config.failures_allowed(entry)
            tripped = store.mark_source_failed(conn, key, message, allowance,
                                               structural=structural)
            if tripped == "parked":
                report.newly_parked.append((key, message))
                log.error("%s parked after %d structural failures — needs a "
                          "fetcher change, not a retry", key, allowance)
            elif tripped == "disabled":
                report.newly_disabled.append(key)
                log.error("%s disabled after %d consecutive failures",
                          key, allowance)
            elif structural and store.park_source(conn, key, message):
                # Already disabled when the endpoint went structural. Stop
                # probing it and escalate now rather than at the next trip,
                # which would never come.
                report.newly_parked.append((key, message))
                log.error("%s parked — %s", key, message)
            if retrying:
                # Still broken. Widen the cooldown and stay quiet — the one
                # notification was sent when it first switched off.
                store.bump_source_cooldown(conn, key)
                log.info("%s still failing — next retry in %d min", key,
                         store.backoff_minutes(
                             store.retry_attempts(conn, key),
                             config.retry_after_minutes,
                             config.retry_backoff_factor,
                             config.retry_backoff_max_minutes))
            conn.commit()
            continue
        if retrying:
            report.recovered.append(key)
            log.info("%s recovered and is enabled again", key)
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
            if posting.detail_url and len(posting.description) < TEASER_CHARS:
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
    for key, message in report.newly_parked:
        print(f"  PARK {key}: {message}")
        print("       needs a fetcher change — no auto-retry until --reset")
    for key in report.recovered:
        print(f"  BACK {key} recovered and is enabled again")
    for note in report.pending:
        print(f"  todo {note}")

    print(("(dry run) " if args.dry_run else "") + report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
