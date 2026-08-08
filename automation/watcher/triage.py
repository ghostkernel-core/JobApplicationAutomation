"""Judge whether a posting is worth reading at all, before it costs anything.

This is the profile-derived discovery gate that replaced `title_allow`: a
hand-written substring list decided what was ever *seen*, ahead of the profile
that decides what is worth applying to, and it was never measured — see the
comment on `SourceDefaults.title_deny` in config.py. Triage asks the same
question the substring list was trying to answer, but against the actual
candidate profile, and only after the free deterministic rules in
`prefilter.py` have already run.

The prompt carries title, company, and location only — never a description.
Judging on those three costs about 15 tokens a line; reading the body would
cost hundreds. That is the whole point of putting this stage before hydration:
a wrong `unsure` is retried for free by the normal pipeline downstream, so the
only mistake that actually costs something is a wrong `drop`. The prompt says
so explicitly, and `_coerce` enforces it structurally — nothing but the exact
string "drop" ever becomes a drop.

Fail-open, in every sense matcher.py is, but landing somewhere different. A
degraded matcher result still gets persisted eventually (see matcher.py's
`_persist`), because a scored-but-wrong verdict is recoverable — it sits in the
digest at 45/maybe until someone looks. A degraded triage result must never be
persisted at all: writing a drop row this stage was not actually able to judge
would silently remove a posting from every future cycle on the strength of
nothing. So there is no `score_attempts`-style withhold-then-persist dance
here — "persist nothing" is automatically safe, because the posting simply
proceeds to hydrate, store, and score as if triage had not run this cycle.

Cache key: `sha1(profile_digest + "\\n" + kb)[:16]`, stored on every drop as
`digest_key`. `known_drops` only suppresses a repeat within the same key, so
editing the canonical profile or approving a `profile_kb.md` rule re-judges
the backlog exactly once rather than trusting a decision the profile has since
outgrown.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import profile, store
from .claude_cli import ClaudeError, run_json
from .config import Config, load_config, load_sources
from .logsetup import force_utf8, setup
from .normalize import Posting
from .poll import PollReport, _collect
from .prefilter import check as prefilter_check

log = logging.getLogger("watcher.triage")

DECISIONS = ("keep", "drop", "unsure")

# The paragraph beginning "The test is" is not padding. Without it the model
# reads the profile, sees it is dominated by AI/ML, and quietly swaps the drop
# test for a relevance test — "no ML signal in the title, therefore drop". The
# first honest `--replay 100` caught it doing exactly that to three postings the
# scorer had rated 68, 68 and 72: a "Software Engineer II", a "Software Engineer
# - Backend", and a "Data Analyst*in Claims", each dropped with a `why` that
# said, in so many words, that the title was not about machine learning. Every
# one of those is a technical role one specialisation away, which the paragraph
# above it already calls out as never a drop — so the rule was stated and lost
# anyway. Naming the wrong test is what makes it stick.
#
# The paragraph after it closes the other route to the same mistake. The kb's
# Avoid list is handed to this stage and to the matcher alike, but only the
# matcher can see the thing Avoid is actually about: "pure analytics, BI/
# reporting" turns on how much of the role is reporting, which is a fact about
# the description. From a title it is unknowable, and acting on it anyway costs
# real postings — of the 205 stored postings scoring >=65, nine carry an
# analytics-shaped title, and they include a Data Engineer at 82, a Data
# Scientist at 78, and an energy-sector analyst at 85 that sits squarely on the
# profile. "Data Analyst*in Claims" is indistinguishable from those at this
# stage, so the honest answer here is unsure and the matcher decides.
_SYSTEM = """\
You are the first, cheapest filter in a pipeline that later reads full job \
postings for one candidate. You see only a title, a company, and a location — \
never a description — because the question here is whether reading the \
description would even be worth it.

--- CANDIDATE PROFILE ---
{digest}

--- MATCHING PREFERENCES ---
These are where "stop showing me X" belongs. They narrow what to flag, never \
what the candidate is capable of.
{kb}

--- DECISION ---
For each posting decide keep, drop, or unsure.

drop means reading the full posting would be pure waste of money: a different \
profession entirely — nursing, accounting, skilled trades, legal, sales, and \
the like — never a technical role that is merely one seniority level or one \
specialisation away from the profile. If a technical role could plausibly be \
adjacent, that is not a drop.

The test is "is this a different profession?", never "does this title match \
the candidate's specialisation?". Those two come apart on exactly the titles \
that matter. A bare "Software Engineer", "Backend Engineer", or "Data Analyst" \
says nothing either way about specialisation, which makes it unsure. That a \
title does not mention AI, ML, or data science is not a reason to drop it — it \
is the ordinary case for a title this short, and the description settles it \
downstream for free.

A matching preference is not a shortcut past that test. Preferences that turn \
on how much of a role is something — "pure" analytics, work that is \
"primarily" X, a role whose "core" is Y — describe the body of a posting, and \
a title cannot settle them. "Data Analyst*in Claims" and "Data Engineer \
Campaign & Analytics" read almost identically from here, yet one may be the \
reporting job the preferences rule out and the other a data engineering job \
the scorer rates in the eighties. So a preference of that shape makes a \
posting unsure, never a drop. "Analyst", "Analytics", "BI" and "Reporting" \
are the wording of the preference, not the name of a profession: a title \
carrying one of them is never a drop here, whatever else it says. Only a \
title naming a genuinely different line of work — "Recruiter", "Sales \
Executive" — is a drop, and that is the different-profession test doing the \
work, not the preference.

unsure means there is not enough here to be sure either way: a generic or \
ambiguous title, an unfamiliar company, a role that could go either way. An \
unsure costs one description read later, downstream, for free. A wrong drop \
costs the job outright, silently, with nobody ever seeing the posting again. \
When genuinely uncertain, choose unsure — never drop.

keep means that on title, company, and location alone this looks like it \
could be worth the candidate's time.

"why" is at most 5 words on the reason for the decision.

--- OUTPUT ---
Return only this JSON object, no prose and no code fence:
{{"results":[{{"id":"<the id given>","decision":"keep","why":""}}]}}
Return exactly one result per posting, using the ids given below verbatim.

--- POSTINGS ---
{postings}
"""


@dataclass
class TriageReport:
    #: Postings an actual triage call judged (excludes suppressed repeats).
    judged: int = 0
    #: Drop rows written this run.
    dropped: int = 0
    #: Judged postings whose batch failed and degraded to unsure — never a
    #: reason for a persisted drop.
    degraded: int = 0
    #: Postings already dropped under the current digest_key — no call made.
    suppressed: int = 0
    #: Postings this call declined to judge at all because there were more
    #: than triage_max_per_cycle of them. Left with no result and no drop row
    #: — not marked unsure just to clear the queue, and not dropped by
    #: exhaustion. A future call (the next cycle, or another --backfill
    #: chunk) is what judges them.
    deferred: int = 0
    by_decision: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_decision is None:
            self.by_decision = {"keep": 0, "drop": 0, "unsure": 0}


def cache_key(digest: str, kb: str) -> str:
    """The digest_key stored on every drop row.

    Scoped to the profile content that produced the decision, not to a
    version number, so any edit to either input invalidates every drop made
    under the old one — `known_drops` simply stops matching them.
    """
    basis = f"{digest}\n{kb}".encode("utf-8")
    return hashlib.sha1(basis).hexdigest()[:16]


def _render_posting(posting: Posting) -> str:
    location = posting.location or posting.country or "unknown"
    remote = " · remote" if posting.remote else ""
    return (
        f"### id: {posting.fingerprint}\n"
        f"{posting.title} — {posting.company}\n"
        f"{location}{remote}\n"
    )


def build_prompt(postings: Sequence[Posting], digest: str, kb: str) -> str:
    body = "\n".join(_render_posting(p) for p in postings)
    return _SYSTEM.format(digest=digest, kb=kb or "(none yet)", postings=body)


def _coerce(raw: Any) -> dict[str, Any]:
    """Normalise one model result. Only an exact "drop" string ever drops.

    null, 42, "maybe", "KEEP!" — anything that is not the literal string
    "drop" resolves to unsure, whatever it looks like it meant. The asymmetry
    is deliberate: unsure and keep have the identical downstream effect (the
    posting proceeds normally), so there is nothing to lose by collapsing
    every ambiguous or malformed response into the safe one.
    """
    decision = raw.get("decision")
    if decision == "drop":
        resolved = "drop"
    elif decision == "keep":
        resolved = "keep"
    else:
        resolved = "unsure"
    why = str(raw.get("why") or "").strip()
    return {"decision": resolved, "why": why[:80]}


_FALLBACK: dict[str, Any] = {"decision": "unsure", "why": ""}


def _degraded(reason: str) -> dict[str, Any]:
    """The fallback result, tagged with why it was needed.

    `degraded` is an in-memory marker only, exactly as in matcher.py — nothing
    stores it directly. Callers test this key, and it is what keeps a
    degraded result from ever being written as a drop.
    """
    return {**_FALLBACK, "degraded": reason}


# The two rules the prompt above states and cannot, on its own, keep.
#
# Neither is new policy — both are already written into `_SYSTEM` in as many
# words. What is new is that they no longer depend on the model agreeing with
# them on any given call, which two live `--replay 150` runs showed it does
# not. Each run dropped exactly one posting from the stored >=65 band, and a
# *different* one each time: "Data Analyst*in Claims" (68) reasoning "Data
# analyst, pure BI/analytics", then "DevOps Engineer (Product)" (75) reasoning
# "DevOps/infrastructure ops" — a posting the previous run had called unsure
# with the reason "DevOps, could be MLOps". The same title, judged twice,
# opposite answers. A rule that survives only when the sampling goes your way
# is not being enforced, and the postings it fails on are lost silently and
# permanently.
#
# Both guards are one-way, which is the whole of their safety: they turn a
# drop into an unsure and never the reverse, so they can only widen the
# funnel. This is not `title_allow` returning — that list decided what was
# ever *seen*, and nothing here drops anything. A posting either guard touches
# goes on to be hydrated and scored on its description like any other.
#
# The measured price is small and the measured cover is not: of run D's 57
# drops only 3 would flip, while 186 of the 206 stored postings scoring >=65
# match one guard or the other.
_TITLE_ONLY_DROP_RULES = (
    # profile_kb.md rules out "pure analytics, BI/reporting" — and "pure" is a
    # fact about the body of a posting, so from a title the rule is unknowable.
    # Nine of the 206 band postings carry a title like this, topping out at 85.
    #
    # No leading word boundary on the stems: German titles compound them, so
    # `\banalyst` would miss "Datenanalyst" and `\banalytik` would miss
    # "Datenanalytik" — the exact titles most at risk. Only "BI" needs
    # boundaries, or it matches inside every other word.
    ("a preference about proportion needs the description",
     re.compile(r"(analyst|analytic|analytik|analyse|reporting"
                r"|\bbi\b|business\s+intelligence)", re.IGNORECASE)),
    # The profile's own field and everything one specialisation from it. A
    # narrow list on purpose: "Process Engineer", "Quality Engineer" and
    # "Hardware Developer" match none of it and still drop, which is most of
    # what this stage is for.
    ("a technical role one specialisation away is never a drop",
     re.compile(r"(devops|mlops|\bsre\b|site\s+reliability|platform|cloud"
                r"|backend|back-end|software|machine\s+learning|deep\s+learning"
                r"|\bml\b|\bai\b|\bki\b|\bdata\b|\bllm\b|\bnlp\b"
                r"|computer\s+vision|\bpython\b)", re.IGNORECASE)),
)


def _overrule_title_only_drop(posting: Posting,
                              verdict: dict[str, Any]) -> dict[str, Any]:
    """Downgrade a drop the title alone cannot honestly support."""
    if verdict.get("decision") != "drop":
        return verdict
    for rule, pattern in _TITLE_ONLY_DROP_RULES:
        if not pattern.search(posting.title):
            continue
        log.info("overruling title-only drop (%s): %s (%s) — %s", rule,
                 posting.title, posting.company,
                 verdict.get("why") or "no reason given")
        return {"decision": "unsure",
                "why": f"overruled: {verdict.get('why') or 'title-only drop'}"[:80],
                "overruled": rule}
    return verdict


def score_batch(postings: Sequence[Posting], digest: str, kb: str,
                config: Config) -> dict[str, dict[str, Any]]:
    """Judge one batch. Never raises — a failed batch degrades to unsure."""
    prompt = build_prompt(postings, digest, kb)
    try:
        data = run_json(prompt, model=config.triage_model,
                        timeout=config.triage_timeout, retries=1)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ClaudeError(f"no results list in response: {str(data)[:200]}")
    except ClaudeError as exc:
        log.error("batch of %d failed to triage (%s) — degrading to unsure",
                  len(postings), exc)
        reason = str(exc)[:300]
        return {p.fingerprint: _degraded(reason) for p in postings}

    by_id: dict[str, dict[str, Any]] = {}
    for item in results:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = _coerce(item)

    out: dict[str, dict[str, Any]] = {}
    for posting in postings:
        if posting.fingerprint in by_id:
            out[posting.fingerprint] = _overrule_title_only_drop(
                posting, by_id[posting.fingerprint])
        else:
            log.warning("no result for %s (%s) — degrading to unsure",
                        posting.fingerprint, posting.title)
            out[posting.fingerprint] = _degraded("no result returned for this posting")
    return out


def _chunks(postings: Sequence[Posting], size: int) -> Iterable[Sequence[Posting]]:
    for start in range(0, len(postings), size):
        yield postings[start:start + size]


def triage_postings(postings: Sequence[Posting], config: Config | None = None,
                    persist: bool = True,
                    ) -> tuple[dict[str, dict[str, Any]], TriageReport]:
    """Judge a batch of never-stored postings. This is what poll_once calls.

    `persist=False` (the --replay / --calibrate path) makes no database calls
    at all — no suppression lookup, no writes — so it is safe to point at
    postings that were never fetched from a live source.

    `triage_max_per_cycle` is enforced here, once, as a hard per-call limit —
    the safety net a live poll cycle relies on so a widened prefilter can
    never hand it thousands of first-time candidates in one go. Anything past
    the cap gets no result and no drop row; it is deferred, not judged, so a
    wrong verdict is never manufactured just to clear a queue. `--backfill`
    drives this same limit across as many calls as it takes rather than
    bypassing it — see `_triage_all`.
    """
    config = config or load_config()
    report = TriageReport()
    results: dict[str, dict[str, Any]] = {}
    if not postings:
        return results, report

    cap = config.triage_max_per_cycle
    if cap and len(postings) > cap:
        report.deferred = len(postings) - cap
        postings = postings[:cap]

    digest = profile.get_digest()
    kb = profile.get_kb()
    digest_key = cache_key(digest, kb)

    known: set[str] = set()
    if persist:
        ids = [p.fingerprint for p in postings]
        with store.connect() as conn:
            known = store.known_drops(conn, ids, "triage", digest_key)
            if known:
                store.touch_drops(conn, [i for i in ids if i in known])

    for posting in postings:
        if posting.fingerprint in known:
            results[posting.fingerprint] = {
                "decision": "drop", "why": "suppressed (already dropped)",
                "suppressed": True,
            }
            report.suppressed += 1

    to_judge = [p for p in postings if p.fingerprint not in known]

    for batch in _chunks(to_judge, config.triage_batch_size):
        batch_results = score_batch(batch, digest, kb, config)
        results.update(batch_results)
        for posting in batch:
            verdict = batch_results[posting.fingerprint]
            report.judged += 1
            report.by_decision[verdict["decision"]] += 1
            if verdict.get("degraded"):
                report.degraded += 1

        if persist:
            with store.connect() as conn:
                for posting in batch:
                    verdict = batch_results[posting.fingerprint]
                    if verdict.get("degraded"):
                        # Fail-open point 4: a degraded result is never
                        # persisted. No row means the posting proceeds
                        # through the normal hydrate/store/score pipeline
                        # next time it is seen, exactly as if triage had
                        # not run at all this cycle.
                        continue
                    if verdict["decision"] == "drop":
                        store.record_drop(conn, posting, "triage",
                                          verdict["why"], digest_key)
                        report.dropped += 1

    return results, report


def _triage_all(postings: Sequence[Posting], config: Config,
                persist: bool) -> tuple[dict[str, dict[str, Any]], TriageReport]:
    """Work through a backlog of any size, one capped call at a time.

    `--backfill` is the one caller that can genuinely hand triage thousands
    of first-time candidates in a single run. `triage_postings` won't judge
    more than `triage_max_per_cycle` in one call, so this drives it across as
    many calls as it takes rather than silently truncating the backlog to
    one cycle's worth.
    """
    cap = max(config.triage_max_per_cycle, 1)
    all_results: dict[str, dict[str, Any]] = {}
    total = TriageReport()
    for start in range(0, len(postings), cap):
        results, report = triage_postings(postings[start:start + cap], config,
                                          persist=persist)
        all_results.update(results)
        total.judged += report.judged
        total.dropped += report.dropped
        total.degraded += report.degraded
        total.suppressed += report.suppressed
        for key, value in report.by_decision.items():
            total.by_decision[key] += value
    return all_results, total


# --------------------------------------------------------------------------
# --replay: judge already-stored postings, never write
# --------------------------------------------------------------------------

def _posting_from_row(row: sqlite3.Row) -> Posting:
    """Rebuild a Posting from a stored row, for --replay.

    Good enough for what triage reads — title, company, location — even
    though the recomputed fingerprint is not guaranteed byte-identical to the
    stored id. --replay always calls with persist=False, so that never matters.
    """
    return Posting(
        source=row["source"], provider=row["provider"],
        source_job_id=row["source_job_id"] or "",
        url=row["url"], company=row["company"], title=row["title"],
        location=row["location"] or "", country=row["country"] or "",
        remote=bool(row["remote"]),
    )


def _print_replay_table(rows: Sequence[sqlite3.Row], postings: Sequence[Posting],
                        results: dict[str, dict[str, Any]]) -> None:
    for row, posting in zip(rows, postings):
        verdict = results.get(posting.fingerprint, _FALLBACK)
        flag = ("!" if verdict.get("degraded")
                else "~" if verdict.get("overruled") else " ")
        print(f"{verdict['decision']:<6}{flag} {row['company'][:20]:<20} "
              f"{row['title'][:44]:<44} {row['country'] or '--'}")
        if verdict.get("why"):
            print(f"        {verdict['why']}")


def _acceptance_gate(rows: Sequence[sqlite3.Row], postings: Sequence[Posting],
                     results: dict[str, dict[str, Any]],
                     stored: dict[str, Any]) -> tuple[int, list[str]]:
    """Judge a replay against the stored >=65 band.

    Returns the exit code and the lines explaining it, rather than printing
    and returning, so the caller decides where they go — the JSON path needs
    them on stderr to keep stdout parseable, and both paths need the code.

    The band is the gate's whole population and its claim is that triage kept
    all of it. A degraded posting was never judged, so it is evidence of
    nothing — counting it as "did not come back drop" is how this gate once
    certified a run in which all 300 postings timed out and no decision was
    made at all. Absence of a drop and absence of a verdict read differently.
    """
    band = 0          # postings with a stored score >= 65
    at_risk = []      # ... that triage would drop
    uncovered = []    # ... that triage never actually judged
    for row, posting in zip(rows, postings):
        verdict_row = stored.get(row["id"])
        if not (verdict_row and verdict_row["score"] >= 65):
            continue
        band += 1
        verdict = results.get(posting.fingerprint)
        if not verdict or verdict.get("degraded"):
            uncovered.append((row, verdict_row["score"]))
        elif verdict["decision"] == "drop":
            at_risk.append((row, verdict_row["score"]))

    if at_risk:
        # With the denominator, because "3 dropped" reads as a rounding error
        # and "3 of 24" reads as the 12% of the band it actually is. The other
        # two verdicts already carry theirs.
        lines = [f"\nGATE FAILED: {len(at_risk)} of {band} posting(s) scoring "
                 f">=65 would be dropped by triage — the gate expects zero:"]
        lines += [f"  {score:>3}  {row['company']} — {row['title']}"
                  for row, score in at_risk]
        return 1, lines
    if not band:
        return 1, ["\nGATE INCONCLUSIVE: no replayed posting has a stored score "
                   ">=65, so there was nothing to certify."]
    if uncovered:
        return 1, [f"\nGATE INCONCLUSIVE: {len(uncovered)} of {band} posting(s) "
                   f"scoring >=65 were never judged (degraded), so this run says "
                   f"nothing about them. Fix the degradation and re-run — see the "
                   f"log for why the batch failed."]
    return 0, [f"\nacceptance gate passed: all {band} posting(s) scoring >=65 were "
               f"judged, none came back drop."]


# --------------------------------------------------------------------------
# --backfill: fetch live sources and pre-seed the drops table
# --------------------------------------------------------------------------

def _backfill_candidates(config: Config, only: str | None,
                         limit: int | None) -> tuple[list[Posting], PollReport]:
    """What triage would see on the next real poll, fetched right now.

    Reuses poll_once's own fetch-and-dedupe exactly — same source health
    tracking, same known-id/loose-key collapsing — so a candidate here is
    genuinely new, not something already sitting in `postings`. Deliberately
    stops short of hydration: triage never reads a description, so paying
    for one here would be pure waste on postings that may be dropped a
    moment later.
    """
    sources = load_sources()
    report = PollReport()
    store.init_db()
    with store.connect() as conn:
        raw = _collect(sources, config, conn, only, report, include_disabled=False)
        report.fetched = len(raw)
        known = store.known_ids(conn, (p.fingerprint for p in raw))
        recent_loose = store.recent_loose_keys(conn, days=30)

        batch_ids: set[str] = set()
        batch_loose: set[str] = set()
        candidates: list[Posting] = []
        for posting in raw:
            if posting.fingerprint in known or posting.fingerprint in batch_ids:
                continue
            if posting.loose_key in recent_loose or posting.loose_key in batch_loose:
                continue
            batch_ids.add(posting.fingerprint)
            batch_loose.add(posting.loose_key)
            verdict = prefilter_check(posting, sources.defaults, config.max_age_days,
                                      sources.filters)
            if not verdict.accepted:
                continue
            candidates.append(posting)
            if limit and len(candidates) >= limit:
                break
    return candidates, report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _sample_postings(limit: int = 3) -> list[Posting]:
    store.init_db()
    with store.connect() as conn:
        rows = store.recent_postings(conn, limit)
    if rows:
        return [_posting_from_row(row) for row in rows]
    return [Posting(
        source="example", provider="example", source_job_id="0",
        url="https://example.com/jobs/0", company="Example GmbH",
        title="Senior Data Scientist", location="Berlin", country="DE",
    )]


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(
        description="Judge postings against the profile before they are stored.")
    parser.add_argument("--replay", type=int, metavar="N",
                        help="re-judge the N most recently stored postings and "
                             "print a table, without saving")
    parser.add_argument("--backfill", action="store_true",
                        help="fetch live sources, run phase-1 filtering, and "
                             "triage what survives — pre-seeds the drops table "
                             "so the first real poll cycle is not swamped")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="with --backfill, stop after this many candidates")
    parser.add_argument("--source", help="with --backfill, limit to one source, "
                                          "e.g. ats:Bayer")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --backfill, judge but write nothing")
    parser.add_argument("--explain", action="store_true",
                        help="print the current config, cache key, and the "
                             "prompt that would be sent, without calling the model")
    parser.add_argument("--json", action="store_true", help="print raw results as JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    config = load_config()

    if args.explain:
        digest = profile.get_digest()
        kb = profile.get_kb()
        key = cache_key(digest, kb)
        print(f"triage: enabled={config.triage_enabled} model={config.triage_model} "
              f"batch_size={config.triage_batch_size} "
              f"timeout={config.triage_timeout}s "
              f"max_per_cycle={config.triage_max_per_cycle} "
              f"drop_retention_days={config.triage_drop_retention_days}")
        print(f"digest_key: {key}  "
              f"(profile digest {len(digest)} chars, kb {len(kb)} chars)")
        print("\n--- prompt that would be sent ---\n")
        print(build_prompt(_sample_postings(), digest, kb))
        return 0

    if args.replay:
        import json as _json

        store.init_db()
        with store.connect() as conn:
            rows = store.recent_postings(conn, args.replay)
            if not rows:
                print("No stored postings. Run `python -m watcher.poll` first.")
                return 1
            postings = [_posting_from_row(row) for row in rows]
            stored = {row["id"]: store.get_verdict(conn, row["id"]) for row in rows}

        results, report = _triage_all(postings, config, persist=False)

        # The gate runs whichever way the results are rendered. It used to sit
        # inside the else-branch, so `--replay --json` printed the verdicts,
        # never reached the band check, and returned 0 no matter how many high
        # scorers came back drop — the same "reports fine without having
        # looked" defect the gate itself exists to catch, one branch over.
        code, gate_lines = _acceptance_gate(rows, postings, results, stored)

        if args.json:
            print(_json.dumps(results, ensure_ascii=False, indent=2))
            # stdout stays byte-identical to what a consumer already parses;
            # the verdict goes to stderr and the exit code carries the result.
            for line in gate_lines:
                print(line, file=sys.stderr)
        else:
            _print_replay_table(rows, postings, results)
            print(f"\n{report.judged} judged — keep {report.by_decision['keep']}, "
                  f"drop {report.by_decision['drop']}, "
                  f"unsure {report.by_decision['unsure']}"
                  f"{f', {report.degraded} degraded' if report.degraded else ''}")
            for line in gate_lines:
                print(line)
        return code

    if args.backfill:
        candidates, poll_report = _backfill_candidates(config, args.source, args.limit)
        if not candidates:
            print(f"No backfill candidates (fetched {poll_report.fetched}) — "
                  f"nothing new for triage to judge.")
            return 0
        print(f"{len(candidates)} candidate(s) survived phase-1 filtering "
              f"(fetched {poll_report.fetched}) — triaging...")
        results, report = _triage_all(candidates, config, persist=not args.dry_run)
        for posting in candidates:
            verdict = results.get(posting.fingerprint)
            if verdict and verdict["decision"] == "drop":
                why = f"  [{verdict['why']}]" if verdict.get("why") else ""
                print(f"  DROP {posting.company} — {posting.title}{why}")
        print(f"\n{report.judged} judged, {report.suppressed} already known — "
              f"keep {report.by_decision['keep']}, drop {report.by_decision['drop']}, "
              f"unsure {report.by_decision['unsure']}"
              f"{f', {report.degraded} degraded (not persisted)' if report.degraded else ''}")
        if args.dry_run:
            print("(dry run — no drop rows written)")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
