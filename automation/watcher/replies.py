"""Parsing what the user types back at a notification.

Kept as a pure function so the grammar can be checked without a bot token, a
network, or a running event loop.

The design constraint from the original request: approval is by *replying* to a
specific message, because several notifications can be in flight at once. So
this module only decides what a message means — resolving which posting it
refers to is the caller's job, via the reply-to message id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

APPROVE = "approve"
SKIP = "skip"
SNOOZE = "snooze"
UNKNOWN = "unknown"

_APPROVE = {"yes", "y", "yeah", "yep", "ok", "okay", "go", "do it", "build",
            "apply", "ja", "los"}
_SKIP = {"no", "n", "nope", "skip", "nein", "pass", "drop"}
_SNOOZE = {"later", "snooze", "wait", "hold", "not now", "später", "spaeter"}

# Words that carry no meaning of their own and are dropped from the note.
# "apply"/"build"/"go" are absent because they govern what follows — "apply as
# Data Scientist" has to reach the pipeline whole, not as "as Data Scientist".
_DROPPABLE = {"yes", "y", "yeah", "yep", "ok", "okay", "ja"} | _SKIP | _SNOOZE

# "build 3" / "3" / "#3" — promoting one line out of a digest.
_INDEX = re.compile(r"^(?:build\s*|#)?(\d{1,2})\b[\s,.:;-]*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Reply:
    action: str
    note: str = ""
    index: int | None = None  # 1-based digest line, when one was named

    @property
    def is_actionable(self) -> bool:
        return self.action in (APPROVE, SKIP, SNOOZE)


def _classify(word: str) -> str | None:
    if word in _APPROVE:
        return APPROVE
    if word in _SKIP:
        return SKIP
    if word in _SNOOZE:
        return SNOOZE
    return None


def parse(text: str) -> Reply:
    """Turn a reply into an action plus a verbatim note.

    The note is passed through to the build prompt untouched, so
    "yes, add German" and "yes, apply as Data Scientist" reach the pipeline in
    exactly the phrasing its Trigger section already understands.
    """
    raw = (text or "").strip()
    if not raw:
        return Reply(UNKNOWN)

    body = raw
    index: int | None = None
    match = _INDEX.match(raw)
    if match:
        index = int(match.group(1))
        body = match.group(2).strip()
        # A bare "3" is a request to build line 3.
        if not body:
            return Reply(APPROVE, "", index)

    lowered = body.lower()

    # Longest multi-word keywords first, so "not now" beats "no".
    for phrase in sorted(_APPROVE | _SKIP | _SNOOZE, key=len, reverse=True):
        if " " not in phrase:
            continue
        if lowered == phrase or lowered.startswith(phrase + " ") or \
                lowered.startswith(phrase + ","):
            return Reply(_classify(phrase) or UNKNOWN,
                         body[len(phrase):].lstrip(" ,.:;-"), index)

    head, _, tail = body.partition(" ")
    head_clean = head.strip(" ,.:;!-").lower()
    action = _classify(head_clean)
    if action:
        note = tail.strip(" ,.:;-") if head_clean in _DROPPABLE else body
        return Reply(action, note, index)

    # "3 add German" — the index is the approval, the rest is the instruction.
    if index is not None:
        return Reply(APPROVE, body, index)

    # No keyword and no index. Deliberately not treated as a yes: a stray
    # message in the chat must never be able to start a build. The handler asks.
    return Reply(UNKNOWN, body)
