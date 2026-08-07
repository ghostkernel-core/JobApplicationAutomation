"""Measure what discovery threw away, once a week, and write nothing.

Every other module here decides. This one only reports, and that asymmetry is
the whole design: `prefilter.py` and `triage.py` reject roughly nineteen
postings out of twenty before anything is stored, and until the `drops` table
existed there was no record at all of what those were. A filter nobody
measures is indistinguishable from a filter that works — the failure mode is
silence, and silence is exactly what a job watcher produces when it is
working correctly.

So: draw a sample of drops, hydrate them, score them through the *live*
matcher, and report how many would have been worth a notification. A miss rate
of 0% says the filters are calibrated. Anything else says which stage to go
and look at, which is why the report is broken out per stage rather than given
as one number — "all the misses came from triage" and "all of them came from
location" call for completely different fixes, and one aggregate cannot tell
them apart.

Shaped like `kb.py`, minus the approval loop. `kb.py` proposes edits to a
hand-written file and therefore has to ask; this writes to nothing a human
maintains, so there is nothing to approve. It writes no verdict rows and no
postings either — a drop that scores 80 here does *not* get quietly promoted
into the pipeline, because a posting that reappears in a feed will arrive
through the normal path anyway, and one that has left every feed cannot be
applied to. The only column it touches is `drops.audited_at`, which stops the
same row consuming next week's budget as well.

The denominator is kept honest on purpose. A drop whose description cannot be
fetched any more, and a batch the matcher failed to score, both count as
`no_body` rather than as "correctly dropped" — scoring a title with no body
would flatter every number in the report, and flattering this particular
number defeats the point of computing it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import matcher, store
from .config import Config, load_config
from .fetchers import hydrate
from .fetchers import browser
from .fetchers.base import get_text, session
from .logsetup import force_utf8, setup
from .normalize import Posting, to_text

log = logging.getLogger("watcher.recall")

#: Roughly how much of the sample goes to `triage`. It is the newest stage,
#: the only non-deterministic one, and by far the largest — but a purely
#: proportional draw would spend the entire budget on it and never test a
#: deterministic rule at all.
TRIAGE_SHARE = 0.6

#: Every deterministic stage that rejected anything gets at least this many
#: rows. The floor is the point of the exercise: "geography drops are
#: obviously safe" is precisely the assumption nobody has ever checked.
MIN_PER_STAGE = 2

#: How many misses the Telegram message names before it stops. The rest are
#: still counted; this is only about what fits on a phone.
TOP_MISSES = 5

#: A body shorter than this is a teaser or an error page, not a description,
#: and scoring against it produces a number about nothing. Same threshold as
#: `poll.TEASER_CHARS`, and for the same reason.
MIN_BODY_CHARS = 800


@dataclass
class RecallReport:
    """What one audit found. Every count here is per posting, not per row."""

    #: Drop rows in the lookback window — the population, not the sample.
    population: int = 0
    #: Rows actually drawn and looked at.
    sampled: int = 0
    #: Rows a score was obtained for. `sampled - scored == no_body`.
    scored: int = 0
    #: Rows that could not be hydrated, or whose batch degraded. Reported
    #: rather than dropped from the denominator: they are unknowns, and an
    #: audit that silently discarded its unknowns would report a cleaner
    #: result the worse the feeds got.
    no_body: int = 0
    #: Scored at or above `notify_threshold` — an instant ping that never
    #: happened. This is the number the whole module exists to produce.
    would_ping: int = 0
    #: Scored at or above `digest_threshold`. A softer miss: it would have
    #: appeared in the evening list rather than interrupted anyone.
    would_digest: int = 0
    #: stage -> {"sampled": n, "pinged": n, "population": n}
    by_stage: dict[str, dict[str, int]] = field(default_factory=dict)
    #: The would-have-pinged rows themselves, best first, for the message.
    misses: list[dict[str, Any]] = field(default_factory=list)

    def stage(self, name: str) -> dict[str, int]:
        return self.by_stage.setdefault(
            name, {"sampled": 0, "pinged": 0, "population": 0})

    @property
    def miss_rate(self) -> float:
        """Share of *scored* rows that would have pinged, 0.0 when none were.

        Against `scored`, not `sampled`: a posting whose body could not be
        fetched was not judged either way, and putting it in the denominator
        would report a lower miss rate the more feeds went stale.
        """
        return (self.would_ping / self.scored) if self.scored else 0.0


# --------------------------------------------------------------------------
# stratification
# --------------------------------------------------------------------------

def plan_sample(counts: Mapping[str, int], size: int) -> dict[str, int]:
    """How many rows to draw from each stage, given the per-stage volumes.

    Three passes, in this order and for these reasons:

    1. `triage` takes its share first, capped at `TRIAGE_SHARE`. Capped
       rather than merely targeted — it is 90% of the table and would
       otherwise be 90% of the sample.
    2. Every other stage that rejected anything gets `MIN_PER_STAGE`,
       ahead of any proportional arithmetic, because a stage holding 30 rows
       beside triage's 1,200 rounds to zero and is never looked at again.
    3. What is left goes to the deterministic stages in proportion to how
       much each of them actually rejected; only if they are all exhausted
       does the remainder go back to triage.

    Asking for more than exists is fine — every stage is capped at its own
    count, and the caller gets a smaller sample rather than an error.
    """
    available = {stage: int(n) for stage, n in counts.items() if int(n) > 0}
    if size <= 0 or not available:
        return {}

    triage_total = available.get("triage", 0)
    others = {stage: n for stage, n in available.items() if stage != "triage"}
    # Largest first everywhere below, so the arithmetic is deterministic and
    # any rounding slack lands on the stage with the most to say.
    order = sorted(others, key=lambda stage: (-others[stage], stage))

    take: dict[str, int] = {}
    if triage_total:
        take["triage"] = min(triage_total, int(round(size * TRIAGE_SHARE)))
    budget = size - sum(take.values())

    for stage in order:
        if budget <= 0:
            break
        take[stage] = min(others[stage], MIN_PER_STAGE, budget)
        budget -= take[stage]

    volume = sum(others.values())
    if budget > 0 and volume:
        share = budget
        for stage in order:
            if budget <= 0:
                break
            want = int(share * others[stage] / volume)
            grant = max(0, min(want, others[stage] - take.get(stage, 0), budget))
            take[stage] = take.get(stage, 0) + grant
            budget -= grant
        # Integer division leaves a few unspent. Hand them out largest first
        # rather than returning a sample two short of what was asked for.
        for stage in order:
            if budget <= 0:
                break
            grant = min(others[stage] - take.get(stage, 0), budget)
            take[stage] = take.get(stage, 0) + grant
            budget -= grant

    if budget > 0 and triage_total:
        take["triage"] = take.get("triage", 0) + min(
            triage_total - take.get("triage", 0), budget)

    return {stage: n for stage, n in take.items() if n > 0}


# --------------------------------------------------------------------------
# scoring a drop through the live path
# --------------------------------------------------------------------------

def _posting_from_drop(row: sqlite3.Row) -> Posting:
    """Enough of a Posting for `fetchers.hydrate` to fetch the body.

    Only `provider`, `detail_url` and `description` are actually read by the
    hydrators; the rest is filled in so the object is coherent if something
    downstream logs it.
    """
    return Posting(
        source=row["source"], provider=row["provider"],
        source_job_id="", url=row["url"], company=row["company"],
        title=row["title"], location=row["location"] or "",
        country=row["country"] or "", detail_url=row["detail_url"] or "",
    )


def _from_public_page(url: str, timeout: int) -> str:
    """The posting's own public page as text — a plain GET, then a render.

    Most boards serve the description in the HTML (Greenhouse, Personio), so
    the cheap request answers first. Ashby and friends render theirs in the
    browser, and for those the GET comes back as an empty shell that is *not*
    an error — it is simply short, which is why the fallback triggers on
    length rather than on an exception.
    """
    if not url:
        return ""
    try:
        text = to_text(get_text(session(), url, timeout)).strip()
    except Exception as exc:  # noqa: BLE001 — a dead link is one row, not a failure
        log.info("plain fetch of %s failed — %s: %s", url,
                 type(exc).__name__, exc)
        text = ""
    if len(text) >= MIN_BODY_CHARS:
        return text
    try:
        return browser.page_text(url, timeout=timeout).strip()
    except Exception as exc:  # noqa: BLE001
        log.info("could not render %s — %s: %s", url, type(exc).__name__, exc)
        return text


def _body_for(row: sqlite3.Row, timeout: int) -> str:
    """Re-fetch one dropped posting's description, or "" if it has gone.

    Two routes, because a drop row deliberately keeps no description. The
    providers that need a second request per posting stored a `detail_url` and
    are re-fetched through the same hydrator the poller uses. The ones that
    inline the body in their list response — Greenhouse, Ashby and Personio,
    which between them are over a third of this table — stored no `detail_url`
    at all, so for those the only surviving pointer at the text is the public
    URL. Without this second route the audit could not hydrate any of them and
    reported 39 of its first 40 rows as `no_body`: technically honest, and
    useless.

    Never raises. A 404 here is the *expected* case for anything older than a
    few weeks — the posting was filled or withdrawn — and it must cost this
    one row rather than the audit.
    """
    posting = _posting_from_drop(row)
    if posting.detail_url:
        try:
            body = (hydrate(posting, timeout) or "").strip()
        except Exception as exc:  # noqa: BLE001 — one dead link is not a failure
            log.info("could not re-fetch %s — %s: %s", row["title"],
                     type(exc).__name__, exc)
            body = ""
        if len(body) >= MIN_BODY_CHARS:
            return body
    return _from_public_page(row["canonical_url"] or row["url"], timeout)


def _row_for(row: sqlite3.Row, body: str) -> matcher._RowLike:
    """A drop row in the shape `matcher.score_postings` reads.

    `_RowLike` exists for exactly this — it is what `--calibrate` already uses
    to push archived applications through the live scorer. Reusing it means
    the audit cannot drift away from what the matcher actually does, which
    would make every number it reports a measurement of the audit instead.
    """
    return matcher._RowLike(
        id=row["id"], company=row["company"], title=row["title"],
        location=row["location"], country=row["country"],
        remote=0, description=body,
    )


def _sample(conn: sqlite3.Connection, plan: Mapping[str, int]
            ) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for stage, want in plan.items():
        rows.extend(store.sample_drops(conn, stage, want))
    return rows


def audit(config: Config | None = None) -> RecallReport | None:
    """Re-score a sample of this week's drops. Returns None when there is
    nothing worth measuring.

    None rather than an empty report, on the same reasoning as
    `kb.propose`: "we dropped eleven postings all week" is not a miss rate,
    and sending a message that says 0% from a sample of eleven would be worse
    than sending nothing.
    """
    config = config or load_config()
    store.init_db()

    since = (dt.date.today()
             - dt.timedelta(days=config.recall_lookback_days)).isoformat()
    with store.connect() as conn:
        # Sized against every drop in the window, including ones a previous
        # audit already looked at: the proportions being measured are about
        # how much each stage *rejects*, not about how much of it is left
        # unaudited. `sample_drops` still only ever returns fresh rows.
        counts = store.drop_counts(conn, since)
        population = sum(counts.values())
        if population < config.recall_min_drops:
            log.info("recall: %d drop(s) in the last %d day(s) — below the "
                     "minimum of %d, skipping", population,
                     config.recall_lookback_days, config.recall_min_drops)
            return None
        plan = plan_sample(counts, config.recall_sample_size)
        rows = _sample(conn, plan)

    if not rows:
        log.info("recall: %d drop(s) in the window but none unaudited — "
                 "skipping", population)
        return None

    report = RecallReport(population=population, sampled=len(rows))
    for stage, total in counts.items():
        report.stage(stage)["population"] = total

    by_id: dict[str, sqlite3.Row] = {}
    scorable: list[matcher._RowLike] = []
    missing_body: list[sqlite3.Row] = []
    for row in rows:
        report.stage(row["stage"])["sampled"] += 1
        by_id[row["id"]] = row
        body = _body_for(row, config.http_timeout)
        if len(body.strip()) < MIN_BODY_CHARS:
            missing_body.append(row)
            continue
        scorable.append(_row_for(row, body))

    verdicts: dict[str, dict[str, Any]] = {}
    if scorable:
        # persist=False is load-bearing, not a convenience: `save_verdict`
        # would create verdict rows for postings that have no `postings` row
        # at all, and the digest reads verdicts.
        verdicts, _ = matcher.score_postings(scorable, config, persist=False)

    notify = config.notify_threshold
    digest = config.digest_threshold
    audited: list[tuple[str, int | None]] = []

    for row in missing_body:
        report.no_body += 1
        audited.append((row["id"], None))

    for posting_id, verdict in verdicts.items():
        row = by_id[posting_id]
        if verdict.get("degraded"):
            # Not a judgement about the posting — see the module docstring.
            report.no_body += 1
            audited.append((posting_id, None))
            continue
        score = int(verdict.get("score") or 0)
        report.scored += 1
        audited.append((posting_id, score))
        if score >= digest:
            report.would_digest += 1
        if score >= notify:
            report.would_ping += 1
            report.stage(row["stage"])["pinged"] += 1
            report.misses.append({
                "score": score, "company": row["company"],
                "title": row["title"], "stage": row["stage"],
                "reason": row["reason"] or "", "url": row["url"],
            })

    report.misses.sort(key=lambda m: -m["score"])

    with store.connect() as conn:
        for drop_id, score in audited:
            store.mark_drop_audited(conn, drop_id, score)

    log.info("recall: %d sampled, %d scored, %d would have pinged (%d%%), "
             "%d no body", report.sampled, report.scored, report.would_ping,
             round(report.miss_rate * 100), report.no_body)
    return report


# --------------------------------------------------------------------------
# CLI — run an audit without a bot token
# --------------------------------------------------------------------------

def _print(report: RecallReport) -> None:
    print(f"{report.sampled} of {report.population} drop(s) re-scored")
    print(f"  would have pinged:  {report.would_ping} "
          f"({round(report.miss_rate * 100)}%)")
    print(f"  would have made the digest: {report.would_digest}")
    if report.no_body:
        print(f"  no description available: {report.no_body}")
    print("\nby stage (sampled -> would have pinged):")
    for stage, counts in sorted(report.by_stage.items(),
                                key=lambda kv: -kv[1]["sampled"]):
        if not counts["sampled"]:
            continue
        print(f"  {stage:<12}{counts['sampled']:>4} -> {counts['pinged']}")
    if report.misses:
        print("\nmisses:")
        for miss in report.misses:
            print(f"  {miss['score']:>3}  {miss['company']} — {miss['title']}"
                  f"   [{miss['stage']}]")
            if miss["reason"]:
                print(f"       dropped for: {miss['reason']}")
            print(f"       {miss['url']}")
    print("\nNothing was written except drops.audited_at.")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(
        description="Re-score a sample of dropped postings and report the "
                    "miss rate. Writes no verdicts and no postings.")
    parser.add_argument("--plan", action="store_true",
                        help="print the stratified sample this would draw and "
                             "stop — no fetching, no scoring, no writes")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    config = load_config()

    if args.plan:
        store.init_db()
        since = (dt.date.today()
                 - dt.timedelta(days=config.recall_lookback_days)).isoformat()
        with store.connect() as conn:
            counts = store.drop_counts(conn, since)
        plan = plan_sample(counts, config.recall_sample_size)
        print(f"drops in the last {config.recall_lookback_days} day(s): "
              f"{sum(counts.values())}")
        for stage, total in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {stage:<12}{total:>6} in table   "
                  f"{plan.get(stage, 0):>3} would be sampled")
        return 0

    report = audit(config)
    if report is None:
        print("nothing to audit")
        return 0

    if args.json:
        import json as _json

        print(_json.dumps({
            "population": report.population, "sampled": report.sampled,
            "scored": report.scored, "no_body": report.no_body,
            "would_ping": report.would_ping,
            "would_digest": report.would_digest,
            "by_stage": report.by_stage, "misses": report.misses,
        }, ensure_ascii=False, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
