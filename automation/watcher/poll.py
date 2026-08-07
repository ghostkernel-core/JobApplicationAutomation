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

from . import queries, store
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
class SourceCounts:
    """One source's share of the funnel, for the per-source `/status` block.

    The totals alone cannot answer the question people actually ask, which is
    never "how many listings were dropped" but "why is nothing coming from
    *that* board". A single number covering eighteen sources hides a fetcher
    returning its whole catalogue and having all of it filtered, which looks
    identical to one returning nothing at all.
    """
    fetched: int = 0
    already_known: int = 0
    filtered: int = 0
    stored: int = 0
    #: Set when the fetch itself failed, so the row says so instead of
    #: reporting a truthful but misleading four zeros.
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "fetched": self.fetched,
            "already_known": self.already_known,
            "filtered": self.filtered,
            "stored": self.stored,
        }
        if self.error:
            stats["error"] = self.error
        return stats


@dataclass
class PollReport:
    fetched: int = 0
    already_known: int = 0
    filtered: int = 0
    stored: int = 0
    #: Postings triage actually judged this cycle (excludes suppressed
    #: repeats and postings deferred past triage_max_per_cycle). Zero
    #: whenever triage is disabled, skipped with --no-triage, or nothing
    #: survived the deterministic phase.
    triaged: int = 0
    triage_dropped: int = 0
    triage_degraded: int = 0
    triage_deferred: int = 0
    by_source: dict[str, SourceCounts] = field(default_factory=dict)
    new_postings: list[Posting] = field(default_factory=list)
    #: (posting, reason, stage) — stage is one of prefilter.STAGES, including
    #: "triage" for a drop made by the LLM gate rather than a deterministic rule.
    filtered_out: list[tuple[Posting, str, str]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    newly_disabled: list[str] = field(default_factory=list)
    # (source, error) — parked sources get the error in the alert, because the
    # whole point of the escalation is that someone has to read it and act.
    newly_parked: list[tuple[str, str]] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def source(self, key: str) -> SourceCounts:
        """The counters for `key`, created on first mention."""
        return self.by_source.setdefault(key, SourceCounts())

    def source_stats(self) -> dict[str, dict[str, Any]]:
        """The per-source funnel, JSON-safe for `store.save_cycle`."""
        return {key: counts.as_dict()
                for key, counts in sorted(self.by_source.items())}

    def summary(self) -> str:
        parts = [
            f"fetched {self.fetched}",
            f"known {self.already_known}",
            f"filtered {self.filtered}",
            f"new {self.stored}",
        ]
        if self.triaged:
            parts.append(f"triaged {self.triaged} (dropped {self.triage_dropped})")
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
        # Generated search terms, if there are usable ones for this portal.
        # This returns a *new* entry rather than editing the one it was given:
        # `sources.all_enabled()` hands out the objects the config cache holds,
        # so writing into one would leave generated queries in place after the
        # feature was switched back off.
        entry = queries.for_entry(entry, config)
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
            report.source(key).error = message
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
        # Keyed off the config entry rather than `posting.source` so a source
        # that legitimately returns nothing still gets a row. Attributing only
        # what came back would make an empty board vanish from the report,
        # which is the one case worth seeing.
        report.source(key).fetched = len(found)
        postings.extend(found)
    return postings


def _record_filtered(report: PollReport, posting: Posting, reason: str,
                     stage: str, conn, dry_run: bool) -> None:
    """Count a rejected posting and, unless dry_run, persist the drop.

    Every rejecting path funnels through here — deterministic or triage —
    so `filtered_out` and the `drops` table never fall out of step. `conn`
    is `None` for a triage drop: `triage_postings` already wrote its row
    itself, in its own short connection, before poll_once ever sees the
    result, so this call only updates the in-memory report.
    """
    report.filtered += 1
    report.source(posting.source).filtered += 1
    report.filtered_out.append((posting, reason, stage))
    if conn is not None and not dry_run:
        store.record_drop(conn, posting, stage, reason)


def poll_once(dry_run: bool = False, only: str | None = None,
              config: Config | None = None, sources: Sources | None = None,
              no_triage: bool = False, triage_only: bool = False,
              ) -> PollReport:
    """Fetch, filter, triage, and store — in three phases, deliberately.

    Phase 1 (deterministic rules) and phase 3 (hydrate/re-check/store) each
    hold their own short-lived connection. Phase 2 (triage) holds none at
    all: it is a batch of model calls that can run for minutes, and a write
    transaction left open across that is what produced "database is locked"
    against the matcher running the same minute. `triage_postings` opens and
    closes its own connections internally, once per batch, so the drop rows
    it writes are never blocked on — or blocking — anything else in poll_once.

    Re-checking after hydration never triages again: hydration changes the
    description, not title/company/location, the only fields triage reads.
    """
    config = config or load_config()
    sources = sources or load_sources()
    report = PollReport()
    store.init_db()

    # Phase 1 — deterministic rules, inside one short connection. Re-run
    # every cycle rather than suppressed, so a config edit changes what a
    # posting scores against immediately.
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
                report.source(posting.source).already_known += 1
                continue
            if posting.loose_key in recent_loose or posting.loose_key in batch_loose:
                report.already_known += 1
                report.source(posting.source).already_known += 1
                continue
            batch_ids.add(posting.fingerprint)
            batch_loose.add(posting.loose_key)
            candidates.append(posting)

        survivors: list[Posting] = []
        for posting in candidates:
            verdict = check(posting, sources.defaults, config.max_age_days,
                            sources.filters)
            if not verdict.accepted:
                _record_filtered(report, posting, verdict.reason, verdict.stage,
                                 conn, dry_run)
                continue
            survivors.append(posting)

    # Phase 2 — triage, outside any open connection. Judges title/company/
    # location only, before a posting is ever hydrated.
    if survivors and config.triage_enabled and not no_triage:
        from . import triage  # deferred: triage.py imports PollReport from here

        results, triage_report = triage.triage_postings(
            survivors, config, persist=not dry_run)
        report.triaged = triage_report.judged
        report.triage_dropped = triage_report.dropped
        report.triage_degraded = triage_report.degraded
        report.triage_deferred = triage_report.deferred

        judged, survivors = survivors, []
        for posting in judged:
            verdict = results.get(posting.fingerprint)
            if verdict is None:
                # Deferred past triage_max_per_cycle — left untouched, not
                # filtered and not stored, so the next cycle re-offers it.
                continue
            if verdict["decision"] == "drop":
                _record_filtered(report, posting, verdict["why"], "triage",
                                 None, dry_run)
                continue
            survivors.append(posting)

    if triage_only:
        return report

    # Phase 3 — hydrate, re-check, store. A fresh connection: phase 2 may
    # have spent minutes in model calls, and nothing here should hold a
    # lock across that.
    with store.connect() as conn:
        for posting in survivors:
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
                _record_filtered(report, posting, recheck.reason, recheck.stage,
                                 conn, dry_run)
                continue

            report.new_postings.append(posting)
            if not dry_run and store.insert_posting(conn, posting):
                report.stored += 1
                report.source(posting.source).stored += 1

        if dry_run:
            report.stored = len(report.new_postings)
            for posting in report.new_postings:
                report.source(posting.source).stored += 1
            conn.rollback()

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and filter, but write nothing to the database")
    parser.add_argument("--source", help="limit to one source, e.g. ats:Bayer")
    parser.add_argument("--show-filtered", action="store_true",
                        help="also list postings the prefilter/triage rejected, "
                             "with their stage and reason")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--no-triage", action="store_true",
                       help="skip the triage gate; every deterministic survivor "
                            "goes straight to hydrate/store")
    group.add_argument("--triage-only", action="store_true",
                       help="run phases 1-2 and stop — nothing is hydrated or "
                            "stored, only filtered/triaged and reported")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    report = poll_once(dry_run=args.dry_run, only=args.source,
                       no_triage=args.no_triage, triage_only=args.triage_only)

    for posting in report.new_postings:
        print(f"  NEW  {posting.summary()}")
        print(f"       {posting.canonical_url}")
    if args.show_filtered:
        for posting, reason, stage in report.filtered_out:
            print(f"  skip {posting.summary()}  [{stage}: {reason}]")
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
