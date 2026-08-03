"""The part of the profile knowledge that grows from decisions.

Two halves, deliberately unequal in trust:

* **Immediate, no model.** A reply that carries a reason ("no, too much devops")
  is appended verbatim under `## Learned from decisions`, alongside the posting
  it was about. Cheap, exact, and auditable — the file only ever gains lines
  the user actually typed.
* **Periodic, model-assisted.** Condensing those lines into the Prefer/Avoid
  sections is a judgement call, so it is proposed to Telegram for approval
  rather than written silently — `propose()` builds it, `apply_proposal()`
  writes it, and nothing calls the second without a reply in between.

The consolidation only ever *adds* bullets to `## Prefer` and `## Avoid`, and
only those two. It cannot rewrite `## Hard filters` or `## How to weigh gaps`,
which are calibrated by hand against the applications already in `2026/`, and it
cannot delete the raw lines it generalised from — those stay under `## Learned
from decisions` as the audit trail for why a rule exists. An approval that turns
out to be wrong is undone by deleting one bullet, which is the property that
makes approving one at a glance reasonable.

Nothing here may be used to strengthen a claim in an application document. The
file header says so, and `rules/00-canonical-profile.md` remains the only fact
source for anything that reaches a CV.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from .config import DECISIONS_PATH, KB_PROPOSAL_PATH, PROFILE_KB_PATH, Config

log = logging.getLogger("watcher.kb")

_SECTION = "## Learned from decisions"

# The only two sections a proposal may touch.
EDITABLE = ("Prefer", "Avoid")


def log_decision(posting: dict[str, Any], action: str, note: str) -> None:
    """Append one line to decisions.jsonl — the raw, append-only record."""
    entry = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "posting_id": posting.get("id"),
        "company": posting.get("company"),
        "title": posting.get("title"),
        "score": posting.get("score"),
        "action": action,
        "note": note,
    }
    DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_note(company: str, title: str, action: str, note: str) -> bool:
    """Record a reasoned decision in profile_kb.md. Returns False if there was
    nothing to record — a bare yes/no teaches the matcher nothing."""
    note = (note or "").strip()
    if not note:
        return False
    if not PROFILE_KB_PATH.exists():
        log.warning("profile_kb.md missing — skipping note")
        return False

    today = dt.date.today().isoformat()
    line = f"- {today} · {action} · {company} — {title}: {note}\n"

    text = PROFILE_KB_PATH.read_text(encoding="utf-8")
    if _SECTION not in text:
        text = text.rstrip() + f"\n\n{_SECTION}\n\n"
    # Append at the very end: the section is last in the file by convention, and
    # this keeps the write a pure append rather than a splice that could damage
    # hand-written sections above it.
    PROFILE_KB_PATH.write_text(text.rstrip() + "\n" + line, encoding="utf-8")
    return True


def read_kb() -> str:
    return PROFILE_KB_PATH.read_text(encoding="utf-8") if PROFILE_KB_PATH.exists() else ""


# ---------------------------------------------------------------------------
# state: one file holding the pending proposal and the high-water mark
# ---------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    if not KB_PROPOSAL_PATH.exists():
        return {}
    try:
        data = json.loads(KB_PROPOSAL_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("kb_proposal.json is unreadable — starting from empty")
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    KB_PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    KB_PROPOSAL_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pending() -> dict[str, Any] | None:
    """The proposal awaiting a yes/no, if there is one."""
    value = _load_state().get("pending")
    return value if isinstance(value, dict) else None


def save_pending(proposal: dict[str, Any], message_id: int) -> None:
    state = _load_state()
    state["pending"] = {**proposal, "message_id": message_id}
    _save_state(state)


def clear_pending(consumed_through: str | None = None) -> None:
    """Drop the pending proposal and move the high-water mark.

    The mark moves on *decline* as well as approval: a rejected proposal must
    not be regenerated verbatim next week from the same decisions, or the answer
    is a standing no that has to be repeated forever.
    """
    state = _load_state()
    state.pop("pending", None)
    if consumed_through:
        state["consumed_through"] = consumed_through
    _save_state(state)


def recent_decisions(limit: int = 100) -> list[dict[str, Any]]:
    if not DECISIONS_PATH.exists():
        return []
    lines = DECISIONS_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def unconsumed(limit: int = 60) -> list[dict[str, Any]]:
    """Decisions newer than the last proposal, oldest first."""
    mark = str(_load_state().get("consumed_through") or "")
    return [d for d in recent_decisions(limit) if str(d.get("at") or "") > mark]


# ---------------------------------------------------------------------------
# the weekly proposal
# ---------------------------------------------------------------------------

_PROMPT = """\
You maintain the matching knowledge base of a job watcher. It decides which
postings are worth notifying about. It is NOT a CV fact source and nothing in it
may describe the candidate's experience — only what to prefer, avoid, and weigh.

Below is the current file, then the decisions taken since the last review: each
is a posting that was approved (a CV was built), skipped, or snoozed, with
whatever reason was typed at the time.

Propose bullets to ADD to the "Prefer" or "Avoid" sections. Rules:

- Only generalise from a pattern you can see in at least two decisions, or from
  one decision whose stated reason is explicit and unambiguous.
- Do not restate something the file already says in other words.
- Do not propose anything about the candidate's skills, seniority, or history.
- A bare "yes" or "no" with no reason is not evidence of anything. Ignore it.
- Prefer no proposal over a weak one. An empty list is the correct answer most
  weeks, and saying so costs nothing.

Return JSON only:

{{"proposals": [{{"section": "Prefer"|"Avoid",
                 "text": "one bullet, imperative or descriptive, under 25 words",
                 "because": "which decisions this comes from, under 20 words"}}],
  "summary": "one sentence, or why there is nothing to propose"}}

=== current profile_kb.md ===
{kb}

=== decisions since the last review ({count}) ===
{decisions}
"""


def _decision_lines(decisions: list[dict[str, Any]]) -> str:
    out = []
    for d in decisions:
        note = (d.get("note") or "").strip() or "(no reason given)"
        out.append(
            f"- {d.get('at', '?')} · {d.get('action', '?')} · "
            f"{d.get('company', '?')} — {d.get('title', '?')} "
            f"(score {d.get('score')}): {note}"
        )
    return "\n".join(out)


def propose(config: Config) -> dict[str, Any] | None:
    """Ask for consolidated rules. Returns None when there is nothing to review.

    Raises whatever `claude_cli` raises — the caller is a scheduled job that
    logs and moves on, and a silent empty proposal would be indistinguishable
    from a quiet week.
    """
    from . import claude_cli

    decisions = unconsumed(config.kb_lookback)
    reasoned = [d for d in decisions if (d.get("note") or "").strip()]
    if len(decisions) < config.kb_min_decisions or not reasoned:
        log.info("kb: %d decision(s) since the last review, %d with a reason — "
                 "skipping", len(decisions), len(reasoned))
        return None

    prompt = _PROMPT.format(kb=read_kb(), count=len(decisions),
                            decisions=_decision_lines(decisions))
    data = claude_cli.run_json(prompt, model=config.kb_model,
                               timeout=config.kb_timeout)

    items = [
        {"section": str(p.get("section", "")).strip().title(),
         "text": str(p.get("text", "")).strip(),
         "because": str(p.get("because", "")).strip()}
        for p in (data.get("proposals") or [])
        if isinstance(p, dict)
    ]
    # A section name outside Prefer/Avoid is dropped rather than corrected: the
    # model wanting to edit "Hard filters" is exactly the case this must not
    # quietly satisfy.
    items = [p for p in items if p["section"] in EDITABLE and p["text"]]

    newest = max((str(d.get("at") or "") for d in decisions), default="")
    return {
        "proposals": items,
        "summary": str(data.get("summary", "")).strip(),
        "consumed_through": newest,
        "reviewed": len(decisions),
    }


# ---------------------------------------------------------------------------
# applying it
# ---------------------------------------------------------------------------

def _insert(text: str, section: str, bullets: list[str]) -> str:
    """Append bullets to the end of one `## <section>` block.

    A splice rather than an append, because Prefer and Avoid sit in the middle
    of the file. The block ends at the next `## ` heading or at end of file, and
    the insert goes after the last non-blank line inside it so hand-written
    ordering above is untouched.
    """
    lines = text.splitlines()
    heading = f"## {section}"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        raise ValueError(f"profile_kb.md has no '{heading}' section")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1

    return "\n".join(lines[:end] + bullets + lines[end:]) + "\n"


def apply_proposal(proposal: dict[str, Any]) -> int:
    """Write the approved bullets into profile_kb.md. Returns how many landed.

    Each bullet is dated and marked `proposed`, so a rule that came out of this
    loop is distinguishable at a glance from one written by hand.
    """
    items = proposal.get("proposals") or []
    if not items or not PROFILE_KB_PATH.exists():
        return 0

    today = dt.date.today().isoformat()
    text = read_kb()
    written = 0
    for section in EDITABLE:
        bullets = [f"- {p['text']}  <!-- proposed {today} -->"
                   for p in items if p.get("section") == section and p.get("text")]
        if not bullets:
            continue
        text = _insert(text, section, bullets)
        written += len(bullets)

    if written:
        PROFILE_KB_PATH.write_text(text, encoding="utf-8")
    return written


# ---------------------------------------------------------------------------
# CLI — dry-run the proposal without a bot token
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import load_config

    parser = argparse.ArgumentParser(description="propose profile_kb.md edits")
    parser.add_argument("--propose", action="store_true",
                        help="build a proposal and print it; write nothing")
    parser.add_argument("--pending", action="store_true",
                        help="show the proposal currently awaiting a reply")
    args = parser.parse_args(argv)

    if args.pending:
        print(json.dumps(pending(), ensure_ascii=False, indent=2))
        return 0

    proposal = propose(load_config())
    if proposal is None:
        print("nothing to review")
        return 0
    print(json.dumps(proposal, ensure_ascii=False, indent=2))
    print("\n(dry run — nothing written. Approval happens in Telegram.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
