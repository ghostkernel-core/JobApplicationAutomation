"""Score postings against the profile with a headless `claude -p` call.

Everything cheap has already happened by the time a posting arrives here: the
prefilter dropped roughly 90% of each poll for free, and descriptions were only
hydrated for what survived. What is left is the judgement call, and that costs
tokens, so postings are batched and the profile context is sent once per batch
rather than once per posting.

The output contract is strict JSON. A batch that comes back malformed twice is
not dropped — every posting in it is recorded as a low-confidence `maybe`, which
lands in the digest band. A parsing failure should cost a nudge in the evening
digest, never a silently missed role.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import profile, roles, store
from .claude_cli import ClaudeError, run_json
from .config import Config, REPO_ROOT, load_config
from .dedupe import FOLDER_PATTERN
from .logsetup import force_utf8, setup
from .normalize import to_text

log = logging.getLogger("watcher.match")

VERDICTS = ("strong", "maybe", "no")

_SYSTEM = """\
You are screening job postings for one candidate. Decide how well each posting \
fits, using only the profile below. Do not assume skills, seniority, or \
credentials that the profile does not state.

--- CANDIDATE PROFILE ---
{digest}

--- MATCHING PREFERENCES ---
These tune what is worth flagging. They never add to the candidate's facts.
{kb}

--- SCORING ---
score 0-100, where:
  80-100  strong overlap on the core requirements; worth applying today
  60-79   solid fit with one or two real gaps
  40-59   plausible but the core of the role sits outside the profile
  0-39    wrong field, wrong seniority, or a requirement the profile cannot meet

verdict: "strong" for 70+, "maybe" for 40-69, "no" below 40.

Set stop_and_ask to true when the posting requires something the profile does \
not contain and that cannot be honestly written around — a named certification, \
a security clearance, a completed doctorate, native-level German, or a seniority \
level well above the profile. Give stop_reason as one short phrase naming it. \
This is not the same as a low score: a strong posting can still need a decision.

"why" is up to 3 short phrases on what actually matches. "gaps" is up to 3 short \
phrases on what is missing. Phrases, not sentences — they go into a phone \
notification.

--- OUTPUT ---
Return only this JSON object, no prose and no code fence:
{{"results":[{{"id":"<the id given>","score":0,"verdict":"no","why":[],"gaps":[],\
"stop_and_ask":false,"stop_reason":null}}]}}
Return exactly one result per posting, using the ids given below verbatim.

--- POSTINGS ---
{postings}
"""


@dataclass
class MatchReport:
    scored: int = 0
    failed: int = 0
    by_band: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_band is None:
            self.by_band = {"strong": 0, "maybe": 0, "no": 0}


def _render_posting(row: sqlite3.Row, description_chars: int) -> str:
    body = to_text(row["description"] or "").strip()
    if len(body) > description_chars:
        body = body[:description_chars] + " […truncated]"
    location = row["location"] or row["country"] or "unknown"
    remote = " · remote" if row["remote"] else ""
    # Recomputed here rather than read from the postings row: the stored values
    # are whatever was known at insert time, this batch may be re-scoring an
    # older row whose description arrived later, and archived calibration
    # postings have no such columns at all.
    rank = roles.describe(
        roles.level_of(row["title"] or "", body),
        roles.years_required(body),
    )
    return (
        f"### id: {row['id']}\n"
        f"{row['title']} — {row['company']}\n"
        f"{location}{remote}" + (f" · {rank}" if rank else "") + "\n"
        f"{body or '(no description available)'}\n"
    )


def build_prompt(rows: Sequence[sqlite3.Row], digest: str, kb: str,
                 description_chars: int) -> str:
    postings = "\n".join(_render_posting(row, description_chars) for row in rows)
    return _SYSTEM.format(digest=digest, kb=kb or "(none yet)", postings=postings)


def _coerce(raw: Any) -> dict[str, Any]:
    """Normalise one model result into the shape store.save_verdict expects."""
    score = raw.get("score", 0)
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = 0

    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        # Trust the number over the label; they disagree occasionally.
        verdict = "strong" if score >= 70 else "maybe" if score >= 40 else "no"

    def phrases(key: str) -> list[str]:
        value = raw.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip() for v in value if str(v).strip()][:3]

    return {
        "score": score,
        "verdict": verdict,
        "why": phrases("why"),
        "gaps": phrases("gaps"),
        "stop_and_ask": bool(raw.get("stop_and_ask")),
        "stop_reason": (str(raw["stop_reason"]).strip()
                        if raw.get("stop_reason") else None),
    }


_FALLBACK = {
    "score": 45,
    "verdict": "maybe",
    "why": [],
    "gaps": ["scoring failed — judge from the posting"],
    "stop_and_ask": False,
    "stop_reason": None,
}


def score_batch(rows: Sequence[sqlite3.Row], digest: str, kb: str,
                config: Config) -> dict[str, dict[str, Any]]:
    """Score one batch. Never raises — a failed batch degrades to `maybe`."""
    prompt = build_prompt(rows, digest, kb, config.description_chars)
    try:
        data = run_json(prompt, model=config.match_model,
                        timeout=config.match_timeout, retries=1)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ClaudeError(f"no results list in response: {str(data)[:200]}")
    except ClaudeError as exc:
        log.error("batch of %d failed to score (%s) — degrading to maybe",
                  len(rows), exc)
        return {row["id"]: dict(_FALLBACK) for row in rows}

    by_id: dict[str, dict[str, Any]] = {}
    for item in results:
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = _coerce(item)

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["id"] in by_id:
            out[row["id"]] = by_id[row["id"]]
        else:
            # The model returned fewer results than postings, or renamed an id.
            log.warning("no result for %s (%s) — degrading to maybe",
                        row["id"], row["title"])
            out[row["id"]] = dict(_FALLBACK)
    return out


def _chunks(rows: Sequence[sqlite3.Row], size: int) -> Iterable[Sequence[sqlite3.Row]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def score_postings(rows: Sequence[sqlite3.Row], config: Config | None = None,
                   persist: bool = True) -> tuple[dict[str, dict[str, Any]], MatchReport]:
    config = config or load_config()
    report = MatchReport()
    verdicts: dict[str, dict[str, Any]] = {}
    if not rows:
        return verdicts, report

    digest = profile.get_digest()
    kb = profile.get_kb()

    for batch in _chunks(rows, config.batch_size):
        results = score_batch(batch, digest, kb, config)
        verdicts.update(results)
        for verdict in results.values():
            report.scored += 1
            report.by_band[verdict["verdict"]] += 1
            if verdict["gaps"] == _FALLBACK["gaps"]:
                report.failed += 1
        if persist:
            with store.connect() as conn:
                for posting_id, verdict in results.items():
                    store.save_verdict(conn, posting_id, verdict, config.match_model)

    return verdicts, report


def match_pending(config: Config | None = None) -> MatchReport:
    """Score everything stored but not yet judged. This is the scheduled path."""
    store.init_db()
    with store.connect() as conn:
        rows = store.unscored(conn)
    if not rows:
        log.info("nothing to score")
        return MatchReport()
    log.info("scoring %d posting(s)", len(rows))
    _, report = score_postings(rows, config)
    log.info("scored %d — strong %d, maybe %d, no %d (%d degraded)",
             report.scored, report.by_band["strong"], report.by_band["maybe"],
             report.by_band["no"], report.failed)
    return report


# --------------------------------------------------------------------------
# Calibration set — postings that were actually applied to
# --------------------------------------------------------------------------

class _RowLike(dict):
    """Duck-types sqlite3.Row so archived postings reuse the scoring path."""

    def __getitem__(self, key: str) -> Any:  # pragma: no cover - trivial
        return self.get(key)


def archived_postings(limit: int = 20, root: Path | None = None) -> list[_RowLike]:
    """Every archived posting under `<YYYY>/<Company>/<date> - <Role>/`.

    These are ground truth: each one was judged worth a full application. If the
    matcher scores them below the notify threshold it would have stayed silent
    on exactly the roles that mattered, which is the failure mode `--calibrate`
    exists to catch.
    """
    root = root or REPO_ROOT
    found: list[tuple[str, _RowLike]] = []
    for year_dir in sorted(root.glob("20[0-9][0-9]"), reverse=True):
        for company_dir in year_dir.iterdir():
            if not company_dir.is_dir():
                continue
            for app_dir in company_dir.iterdir():
                match = FOLDER_PATTERN.match(app_dir.name) if app_dir.is_dir() else None
                if not match:
                    continue
                html = next(iter(sorted(app_dir.glob("*.html"))), None)
                if not html:
                    continue
                # SingleFile captures inline every image and stylesheet, so the
                # posting text can sit past the first megabyte of base64. Truncating
                # the HTML before stripping tags yields an empty body — read it all
                # and let the description cap apply to the extracted text instead.
                raw = to_text(html.read_text(encoding="utf-8", errors="replace"))
                found.append((match.group(1), _RowLike(
                    id=f"applied:{company_dir.name}:{match.group(2)}",
                    company=company_dir.name,
                    title=match.group(2),
                    location=None, country=None, remote=0,
                    description=raw,
                )))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in found[:limit]]


# --------------------------------------------------------------------------
# CLI — calibration
# --------------------------------------------------------------------------

def _print_table(rows: Sequence[sqlite3.Row],
                 verdicts: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(rows, key=lambda r: -verdicts[r["id"]]["score"])
    for row in ordered:
        v = verdicts[row["id"]]
        flag = "!" if v["stop_and_ask"] else " "
        print(f"{v['score']:>3} {v['verdict']:<6}{flag} {row['company'][:20]:<20} "
              f"{row['title'][:44]:<44} {row['country'] or '--'}")
        if v["why"]:
            print(f"        + {'; '.join(v['why'])}")
        if v["gaps"]:
            print(f"        - {'; '.join(v['gaps'])}")
        if v["stop_reason"]:
            print(f"        ! {v['stop_reason']}")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="Score stored postings against the profile.")
    parser.add_argument("--replay", type=int, metavar="N",
                        help="re-score the N most recently seen postings and print "
                             "a table, without saving (the calibration loop)")
    parser.add_argument("--calibrate", type=int, nargs="?", const=20, metavar="N",
                        help="score the N most recent postings you actually applied "
                             "to (read from the year folders). They should land at "
                             "or above the notify threshold")
    parser.add_argument("--pending", action="store_true",
                        help="score everything not yet judged, and save")
    parser.add_argument("--refresh-digest", action="store_true",
                        help="regenerate state/profile_digest.md and exit")
    parser.add_argument("--json", action="store_true", help="print raw verdicts as JSON")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    config = load_config()

    if args.refresh_digest:
        digest = profile.get_digest(force=True)
        print(digest)
        return 0

    if args.replay:
        store.init_db()
        with store.connect() as conn:
            rows = store.recent_postings(conn, args.replay)
        if not rows:
            print("No stored postings. Run `python -m watcher.poll` first.")
            return 1
        verdicts, report = score_postings(rows, config, persist=False)
        if args.json:
            print(json.dumps(verdicts, ensure_ascii=False, indent=2))
        else:
            _print_table(rows, verdicts)
            print(f"\n{report.scored} scored — strong {report.by_band['strong']}, "
                  f"maybe {report.by_band['maybe']}, no {report.by_band['no']}"
                  f"{f', {report.failed} degraded' if report.failed else ''}")
            print(f"thresholds: notify >= {config.notify_threshold}, "
                  f"digest >= {config.digest_threshold}")
        return 0

    if args.calibrate:
        rows = archived_postings(args.calibrate)
        if not rows:
            print("No archived postings found under the year folders.")
            return 1
        verdicts, report = score_postings(rows, config, persist=False)
        _print_table(rows, verdicts)
        missed = [r for r in rows if verdicts[r["id"]]["score"] < config.notify_threshold]
        print(f"\n{len(rows)} applied-to postings scored — "
              f"{len(rows) - len(missed)} would have been notified, "
              f"{len(missed)} would have been missed at >= {config.notify_threshold}.")
        if missed:
            print("Missed — tune profile_kb.md until these clear the threshold:")
            for row in missed:
                print(f"  {verdicts[row['id']]['score']:>3}  {row['company']} — {row['title']}")
        return 0

    if args.pending:
        match_pending(config)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
