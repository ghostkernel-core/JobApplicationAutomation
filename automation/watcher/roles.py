"""Seniority level and years-of-experience, read out of a posting.

Both readings are advisory. `level_of` returns `unknown` rather than guessing,
and `years_required` returns `None` rather than zero, because in this system a
confident wrong answer is worse than an admitted absence: `prefilter.check`
passes anything it cannot classify through to the matcher, so an honest
`unknown` costs one scored posting while a wrong `intern` silently deletes a
real opportunity.

Years of experience is deliberately **not** a filter by default. Postings
overstate the bar routinely — `profile_kb.md` already says being one or two
years short is a minor gap — so the number is extracted, shown in the Telegram
ping, and handed to the matcher to weigh in context. `[filters.experience]
mode = "filter"` exists for when that stops being the preference.
"""

from __future__ import annotations

import re
from typing import Iterable

# Ordered most-specific first. The first pattern to hit the title wins, which
# is why `head of machine learning` resolves to executive rather than being
# caught later by a bare `machine learning` seniority guess.
LEVELS: tuple[str, ...] = (
    "intern", "junior", "mid", "senior", "lead", "staff", "principal",
    "executive",
)

_LEVEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("intern", re.compile(
        r"\b(intern|internship|praktikum|praktikant\w*|werkstudent\w*|"
        r"working\s+student|trainee|traineeship|ausbildung|auszubildende\w*|"
        r"apprentice\w*|dual(?:es)?\s+studium|thesis|abschlussarbeit)\b", re.I)),
    ("executive", re.compile(
        r"\b(head\s+of|vp|vice\s+president|chief\b|c[toei]o\b|"
        r"director|geschäftsführ\w+|geschaeftsfuehr\w+|bereichsleiter\w*|"
        r"managing\s+director|partner)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    # `staff` only as a standalone rank word: "staff engineer", never
    # "staffing" or "medical staff".
    ("staff", re.compile(r"\bstaff\b(?!\s*(?:ing|augmentation))", re.I)),
    ("lead", re.compile(
        # German compounds bolt the rank onto the noun, so `\bleiter` never
        # fires on `Teamleiter`. Prefixes are enumerated rather than allowing
        # any `\w*leiter` — that would also catch `Begleiter` (companion).
        r"\b(lead|(?:team|projekt|gruppen|entwicklungs|technischer?\s*)?leiter\w*|"
        r"teamlead\w*|team\s+lead|tech\s+lead|"
        r"technical\s+lead|führung\w*|fuehrung\w*)\b", re.I)),
    ("senior", re.compile(
        r"\b(senior|sr\.?|snr\.?|expert\w*|experienced|erfahren\w*|"
        r"iii|iv|level\s*3|l[456])\b", re.I)),
    ("junior", re.compile(
        r"\b(junior|jr\.?|entry[\s-]level|graduate|new\s+grad|absolvent\w*|"
        r"berufseinsteiger\w*|einsteiger\w*|associate|level\s*1|l[12])\b", re.I)),
    ("mid", re.compile(r"\b(mid[\s-]?level|intermediate|regular)\b", re.I)),
)


def level_of(title: str, description: str = "") -> str:
    """Seniority band of a role. One of `LEVELS`, or `unknown`.

    Only the title is read. `description` is accepted and ignored, so callers
    that have a body do not have to know that.

    The body looked like free evidence and is not. A job page carries the
    employer's *other* openings in its sidebar, its boilerplate advertises the
    graduate programme, and the responsibilities section names the seniority of
    everyone the hire reports to. A real archived posting here — "Solutions
    Architect – AI & Data Integration" — read as `intern` purely because an
    "Intern – Legal & Regulatory Affairs" tile sat 40 characters into its body.
    An honest `unknown` costs one scored posting; a confident wrong `intern`
    meets a deny list and deletes the job silently.
    """
    text = title or ""
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.search(text):
            return level
    return "unknown"


# ---------------------------------------------------------------------------
# Years of experience
# ---------------------------------------------------------------------------

# The number, in digits or spelled out. Ranges keep their lower bound: "3-5
# years" is a bar of 3, because 3 is what disqualifies you.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
    "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "fuenf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "zwölf": 12,
    "zwoelf": 12,
}
_NUMBER = r"(\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")"

# "5+ years", "3-5 years", "at least four years", "mindestens 5 Jahre".
_YEARS = re.compile(
    _NUMBER + r"\s*(?:\+|plus)?\s*(?:(?:-|–|to|bis)\s*\d{1,2}\s*)?"
    r"(?:\+\s*)?(?:years?|yrs?\.?|jahre?n?|j\.)\b",
    re.I,
)

# The number only counts when experience is what is being counted. Without
# this, "founded 10 years ago", "a 5 year roadmap", and "5 years of company
# growth" all read as a hiring bar.
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(experience|experienced|background|track\s+record|hands[\s-]on|"
    r"working|worked|practice|expertise|erfahrung|berufserfahrung|"
    r"praxiserfahrung|praxis|tätigkeit|taetigkeit)\b",
    re.I,
)

# Phrases that mean the sentence is about the company or product, not the hire.
_NOT_A_REQUIREMENT = re.compile(
    r"\b(founded|gegründet|gegruendet|since|seit|ago|vor\s+\w+\s+jahren|"
    r"anniversary|jubiläum|in\s+the\s+(?:last|past)|over\s+the\s+(?:last|past)|"
    r"next\s+\d|roadmap|history|geschichte)\b",
    re.I,
)

# Anything beyond this is a typo or a decade count, not a hiring bar.
_MAX_CREDIBLE_YEARS = 20

# How far either side of the number to look for the experience context word.
_WINDOW = 60


def _value(token: str) -> int | None:
    token = token.strip().casefold()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def years_required(*fragments: object) -> int | None:
    """Highest stated years-of-experience bar, or None when nothing credible.

    Highest, not first: a posting that asks for "2+ years Python and 5+ years
    machine learning" has a bar of 5, and reporting 2 would understate what the
    notification needs to say. Ranges contribute their lower bound.
    """
    text = "\n".join(str(f) for f in fragments if f)
    if not text:
        return None

    best: int | None = None
    for match in _YEARS.finditer(text):
        value = _value(match.group(1))
        if value is None or not 0 < value <= _MAX_CREDIBLE_YEARS:
            continue
        start = max(0, match.start() - _WINDOW)
        window = text[start:match.end() + _WINDOW]
        if not _EXPERIENCE_CONTEXT.search(window):
            continue
        if _NOT_A_REQUIREMENT.search(window):
            continue
        if best is None or value > best:
            best = value
    return best


# ---------------------------------------------------------------------------
# Filter decisions
# ---------------------------------------------------------------------------

def level_allowed(level: str, allow: Iterable[str], deny: Iterable[str]) -> bool:
    """Whether a level survives an allow/deny pair.

    `unknown` always survives. It is the value we assign when we could not
    read the posting, and refusing to notify about what we failed to parse is
    how a filter becomes invisible data loss.
    """
    if level == "unknown":
        return True
    deny_set = {str(d).strip().casefold() for d in deny or ()}
    if level in deny_set:
        return False
    allow_set = {str(a).strip().casefold() for a in allow or ()}
    if not allow_set:
        return True
    return level in allow_set


def describe(level: str, years: int | None) -> str:
    """Short label for a Telegram line: 'senior · asks 5+ yrs'."""
    parts = []
    if level and level != "unknown":
        parts.append(level)
    if years:
        parts.append(f"asks {years}+ yrs")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

_LEVEL_FIXTURES: tuple[tuple[str, str], ...] = (
    ("Senior Machine Learning Engineer", "senior"),
    ("Machine Learning Engineer (Senior)", "senior"),
    ("Junior Data Scientist", "junior"),
    ("Working Student – Data Science", "intern"),
    ("Werkstudent Machine Learning (m/w/d)", "intern"),
    ("Praktikum Computer Vision", "intern"),
    ("Head of Machine Learning", "executive"),
    ("VP of Engineering", "executive"),
    ("Staff AI Engineer", "staff"),
    ("Principal Data Scientist", "principal"),
    ("Tech Lead – Machine Learning", "lead"),
    ("Teamleiter Data Science", "lead"),
    ("Machine Learning Engineer", "unknown"),
    ("AI Engineer", "unknown"),
    ("Mid-Level Backend Engineer", "mid"),
    ("Data Scientist III", "senior"),
    ("Staffing Coordinator", "unknown"),      # 'staff' must not match 'staffing'
    ("Medical Staff Scheduler", "staff"),     # standalone 'Staff' does match
)

_YEARS_FIXTURES: tuple[tuple[str, int | None], ...] = (
    ("5+ years of experience in machine learning", 5),
    ("At least 3 years of hands-on experience", 3),
    ("3-5 years of professional experience", 3),
    ("Mindestens 4 Jahre Berufserfahrung", 4),
    ("Sie bringen 6+ Jahre Erfahrung mit", 6),
    ("two years of experience with PyTorch", 2),
    ("2+ years Python and 5+ years experience in ML", 5),
    ("We were founded 10 years ago", None),
    ("Our product has grown over the last 5 years", None),
    ("A great team and a 3 year roadmap", None),
    ("No experience requirement stated", None),
    ("100 years of experience", None),        # beyond credible
)

_GEO_NOTE = "geo resolution is covered by `python -m watcher.geo --self-test`"


def _self_test() -> int:
    failures = 0
    print("level_of")
    for title, expected in _LEVEL_FIXTURES:
        got = level_of(title)
        ok = got == expected
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {title!r:52} -> {got}" + ("" if ok else f"  (want {expected})"))

    print("\nyears_required")
    for text, expected in _YEARS_FIXTURES:
        got = years_required(text)
        ok = got == expected
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {text!r:52} -> {got}" + ("" if ok else f"  (want {expected})"))

    total = len(_LEVEL_FIXTURES) + len(_YEARS_FIXTURES)
    print(f"\n{total - failures}/{total} passed. {_GEO_NOTE}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true",
                        help="run the level and years-of-experience fixtures")
    parser.add_argument("--title", help="classify one title")
    parser.add_argument("--text", help="extract the years-of-experience bar from text")
    args = parser.parse_args(argv)

    if args.title:
        print(f"level: {level_of(args.title)}")
    if args.text:
        print(f"years: {years_required(args.text)}")
    if args.self_test or not (args.title or args.text):
        return _self_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
