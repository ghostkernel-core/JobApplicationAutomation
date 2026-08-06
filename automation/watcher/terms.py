"""Language bar, contract type, and work arrangement, read out of a posting.

The same one-sided rule that governs `roles` and `prefilter` governs this
module: every reading returns `""` or an empty list rather than guessing. These
values are shown in the Telegram ping so a posting can be judged without opening
it — nothing here filters anything, so a wrong confident answer costs a bad
decision on a real job, while an honest blank costs one glance at the URL.

Language is the reading that earns this module. A posting that wants
`verhandlungssicheres Deutsch` is a different proposition from one that says
"English is our working language", and that distinction is usually buried in the
last third of a German job ad — exactly where it is not read before replying
`yes`.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------

#: Language name -> display label. German and English first because they are the
#: two that decide anything here; the rest are reported when named but are not
#: hunted for aggressively.
_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("German", r"deutsch\w*|german"),
    ("English", r"englisch\w*|english"),
    ("French", r"französisch\w*|franzoesisch\w*|french"),
    ("Spanish", r"spanisch\w*|spanish"),
    ("Dutch", r"niederländisch\w*|niederlaendisch\w*|dutch"),
    ("Italian", r"italienisch\w*|italian"),
    ("Polish", r"polnisch\w*|polish"),
)

#: CEFR levels are the only unambiguous statement of a language bar, so they win
#: over every prose qualifier when both appear.
_CEFR = re.compile(r"\b([ABC][12])\b")

#: Every language name in one alternation, used to locate mentions before any
#: qualifier is assigned to one.
_ANY_LANGUAGE = "|".join(p for _, p in _LANGUAGES)

#: Prose qualifiers, strongest first. When several attach to the same language
#: the strongest wins, which is what makes "sehr gute" read as `fluent` even
#: though the weaker `gut\w*` pattern matches the same words.
_QUALIFIERS: tuple[tuple[str, str], ...] = (
    ("native", r"native|muttersprach\w*|first\s+language"),
    ("fluent", r"fluent|verhandlungssicher\w*|business[\s-]fluent|"
               r"fließend|fliessend|proficient|excellent|sehr\s+gut\w*"),
    # The German half is deliberately anchored to a language noun. A bare
    # `gut\w*` would read "gutes Gehalt" two lines above a language bullet as a
    # statement about the language.
    ("good", r"\bgood\b|\bsolid\b|strong\s+command|"
             r"gut\w*\s+(?:\w*kenntnisse|" + _ANY_LANGUAGE + r")"),
    ("basic", r"basic|grundkenntnisse|conversational|working\s+knowledge|"
              r"grundlegende"),
)

#: Phrases that demote a requirement to a bonus. Checked in the same window as
#: the qualifier, because "German is a plus" and "German is required" differ by
#: exactly these words and nothing else.
_OPTIONAL = re.compile(
    r"\b(a\s+plus|of\s+advantage|von\s+vorteil|nice\s+to\s+have|"
    r"beneficial|bonus|wünschenswert|wuenschenswert|ideal(?:ly)?|"
    r"would\s+be\s+(?:a\s+)?(?:plus|great)|optional|not\s+required|"
    r"no\s+german\s+required)\b",
    re.I,
)

#: How far from a language word a level or qualifier may sit and still be about
#: that language. Wide enough for "German language skills at least at level C1".
_WINDOW = 70

#: A language only counts when the surrounding text is about language ability.
#: Without this, "German market", "German engineering", and a Berlin address all
#: read as a language requirement — and "German" appears in the boilerplate of
#: nearly every posting in this pipeline's catchment.
#:
#: The German half cannot use word boundaries: the words that prove the context
#: arrive welded to the language itself, as `Deutschkenntnisse` and
#: `Sprachniveau`, so `\bkenntnisse\b` matches neither. It also needs the verb in
#: every stem it appears in — `sprach`/`sprich` alone miss the infinitive, and
#: "Sie sprechen fließend Deutsch" is one of the commonest ways a German posting
#: states its language bar, so missing it reported no requirement at all.
_LANGUAGE_CONTEXT = re.compile(
    r"\b(?:language|languages|speak\w*|spoken|written|fluen\w*|proficien\w*|"
    r"communicat\w*|level|native|[ABC][12])\b"
    r"|kenntnisse|sprach|sprech|sprich|niveau|muttersprach|verhandlungssicher"
    r"|fließend|fliessend",
    re.I,
)


#: A line break or a bullet marker ends the thought. Requirement lists put one
#: claim per bullet, so a qualifier on the far side of a break belongs to its own
#: bullet — "…Projektkoordination wünschenswert / * Sehr gute Deutschkenntnisse"
#: is a firm German requirement following an optional one, not an optional
#: German requirement.
#:
#: Sentence enders are deliberately *not* boundaries: "(mind. C1)" and similar
#: abbreviations put a full stop in the middle of the very phrase being read.
_BOUNDARY = re.compile(r"[\n\r]|\s[*•‣–-]\s")

#: …except when the optional marker is a heading introducing the list, in which
#: case it governs every bullet under it. The colon is what distinguishes
#: "Nice to have:" from a bullet that merely ends in "wünschenswert".
_HEADING = re.compile(r"\s*:")


def _gap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """Characters between two spans; 0 when they touch or overlap."""
    if a_end <= b_start:
        return b_start - a_end
    if b_end <= a_start:
        return a_start - b_end
    return 0


def _reaches(text: str, kind: str, m_start: int, m_end: int,
             l_start: int, l_end: int) -> bool:
    """Whether a marker may speak for a language mention it sits near."""
    between = text[m_end:l_start] if m_end <= l_start else text[l_end:m_start]
    if not _BOUNDARY.search(between):
        return True
    # A heading reaches across the bullets it introduces, but only forwards.
    return (kind == "optional" and m_end <= l_start
            and _HEADING.match(text[m_end:m_end + 3]) is not None)


def languages(*fragments: object) -> list[str]:
    """Stated language requirements, strongest statement per language.

    Returns labels like `German C1`, `English fluent`, `German basic (a plus)` —
    or an empty list when the posting never talks about language ability.

    Each level and qualifier is assigned to the language mention it sits closest
    to, rather than to every mention within reach of it. Postings state both
    languages in one breath — "verhandlungssicheres Deutsch und gutes Englisch"
    — so overlapping windows otherwise hand C1 to both and report a German bar
    on a role that only wants conversational English.
    """
    text = "\n".join(str(f) for f in fragments if f)
    if not text:
        return []

    mentions: list[tuple[str, int, int]] = []
    for label, pattern in _LANGUAGES:
        for match in re.finditer(pattern, text, re.I):
            start = max(0, match.start() - _WINDOW)
            if not _LANGUAGE_CONTEXT.search(text[start:match.end() + _WINDOW]):
                continue
            mentions.append((label, match.start(), match.end()))
    if not mentions:
        return []

    # kind -> rank, where a lower rank is a stronger claim. Levels outrank prose
    # because "C1" is a standard and "sehr gut" is an opinion.
    strength = {name: i for i, (name, _) in enumerate(_QUALIFIERS)}

    markers: list[tuple[str, str, int, int]] = []
    for match in _CEFR.finditer(text):
        markers.append(("level", match.group(1).upper(),
                        match.start(), match.end()))
    for name, words in _QUALIFIERS:
        for match in re.finditer(words, text, re.I):
            markers.append(("qualifier", name, match.start(), match.end()))
    for match in _OPTIONAL.finditer(text):
        markers.append(("optional", "", match.start(), match.end()))

    levels: dict[str, str] = {}
    quals: dict[str, str] = {}
    optional: dict[str, bool] = {}

    for kind, value, m_start, m_end in markers:
        nearest, best_gap = None, _WINDOW + 1
        for label, l_start, l_end in mentions:
            gap = _gap(m_start, m_end, l_start, l_end)
            if gap < best_gap and _reaches(text, kind, m_start, m_end,
                                           l_start, l_end):
                nearest, best_gap = label, gap
        if nearest is None:
            continue
        if kind == "level":
            levels.setdefault(nearest, value)
        elif kind == "optional":
            optional[nearest] = True
        else:
            current = quals.get(nearest)
            if current is None or strength[value] < strength[current]:
                quals[nearest] = value

    found: list[str] = []
    for label, _ in _LANGUAGES:
        if not any(m[0] == label for m in mentions):
            continue
        bar = levels.get(label) or quals.get(label, "")
        text_label = f"{label} {bar}".strip()
        if optional.get(label):
            text_label += " (a plus)"
        found.append(text_label)
    return found


# ---------------------------------------------------------------------------
# Contract type
# ---------------------------------------------------------------------------

#: Ordered most-specific first: a posting that says both "befristet" and
#: "Festanstellung" is a fixed-term employment contract, not a permanent one.
_CONTRACTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("working student", re.compile(
        r"\b(werkstudent\w*|working\s+student)\b", re.I)),
    ("internship", re.compile(
        r"\b(internship|praktikum|praktikant\w*)\b", re.I)),
    ("freelance", re.compile(
        r"\b(freelance\w*|freiberuflich|contractor|self[\s-]employed|"
        r"interim|b2b\s+contract|honorarbasis)\b", re.I)),
    ("fixed-term", re.compile(
        r"\b(fixed[\s-]term|befristet\w*|temporary\s+contract|"
        r"limited\s+to\s+\d+\s+(?:months|years)|zunächst\s+befristet)\b", re.I)),
    ("permanent", re.compile(
        r"\b(permanent\s+(?:contract|position|role|employment)|unbefristet\w*|"
        r"festanstellung|permanent\s+full[\s-]time)\b", re.I)),
)

_PART_TIME = re.compile(r"\b(part[\s-]time|teilzeit)\b", re.I)
_FULL_TIME = re.compile(r"\b(full[\s-]time|vollzeit)\b", re.I)


def contract(*fragments: object) -> str:
    """Contract type as a short label, or "" when the posting does not say.

    Hours are appended when stated, so "permanent · full-time" is one string —
    the ping has one line for this and two facts to fit into it.
    """
    text = "\n".join(str(f) for f in fragments if f)
    if not text:
        return ""

    kind = ""
    for label, pattern in _CONTRACTS:
        if pattern.search(text):
            kind = label
            break

    hours = ""
    # Part-time first: a posting offering both says so as "full- or part-time",
    # and the part-time option is the one that changes the decision.
    if _PART_TIME.search(text):
        hours = "part-time"
    elif _FULL_TIME.search(text):
        hours = "full-time"

    return " · ".join(p for p in (kind, hours) if p)


# ---------------------------------------------------------------------------
# Work arrangement
# ---------------------------------------------------------------------------

# Hybrid is tested before remote because it is the more specific claim: a
# posting describing "hybrid — 2 days remote" mentions remote but is not one.
_HYBRID = re.compile(
    r"\b(hybrid\w*|hybrides?\s+arbeiten|\d\s*(?:days?|tage?)\s*"
    r"(?:per|pro|a)\s*(?:week|woche)\s*(?:in\s+(?:the\s+)?office|"
    r"im\s+büro|vor\s+ort)|mobiles?\s+arbeiten|flexible\s+office)\b", re.I)

_REMOTE = re.compile(
    r"\b(fully\s+remote|100\s*%\s*remote|remote[\s-]first|remote[\s-]only|"
    r"work\s+from\s+home|homeoffice|home[\s-]office|ortsunabhängig|"
    r"ortsunabhaengig|vollständig\s+remote|remote\s+(?:position|role|work))\b",
    re.I)

_ONSITE = re.compile(
    r"\b(on[\s-]?site|vor\s+ort|präsenz\w*|praesenz\w*|in[\s-]office|"
    r"no\s+remote|kein\s+homeoffice|office[\s-]based)\b", re.I)


def arrangement(*fragments: object, remote_flag: bool = False) -> str:
    """`remote`, `hybrid`, `onsite`, or "" when the posting does not say.

    `remote_flag` is the source's own structured answer where it has one. It
    seeds the result but does not override the text: boards mark a hybrid role
    as remote routinely, and the body is the more honest of the two.
    """
    text = "\n".join(str(f) for f in fragments if f)

    if text:
        if _HYBRID.search(text):
            return "hybrid"
        if _REMOTE.search(text):
            return "remote"
        if _ONSITE.search(text):
            return "onsite"
    return "remote" if remote_flag else ""


def describe(langs: list[str] | None, contract_label: str,
             arrangement_label: str) -> str:
    """One compact line for the ping, or "" when nothing was readable."""
    parts: list[str] = []
    if arrangement_label:
        parts.append(arrangement_label)
    if contract_label:
        parts.append(contract_label)
    if langs:
        parts.append(", ".join(langs))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

_LANGUAGE_FIXTURES: tuple[tuple[str, list[str]], ...] = (
    ("Verhandlungssichere Deutschkenntnisse (mind. C1) und gutes Englisch",
     ["German C1", "English good"]),
    ("Fluent English required; German is a plus",
     ["German (a plus)", "English fluent"]),
    ("You speak German at C2 level", ["German C2"]),
    ("Sehr gute Deutschkenntnisse in Wort und Schrift", ["German fluent"]),
    ("English is our working language", ["English"]),
    ("Grundkenntnisse in Deutsch von Vorteil", ["German basic (a plus)"]),
    ("Deutsch C1 zwingend erforderlich, Englisch fließend",
     ["German C1", "English fluent"]),
    ("Sie sprechen fließend Deutsch", ["German fluent"]),
    ("Wir bieten ein gutes Gehalt. Deutschkenntnisse erforderlich.",
     ["German"]),
    # The optional marker belongs to the bullet above; German is not optional.
    ("* Projektkoordination wünschenswert\n* Sehr gute Deutschkenntnisse",
     ["German fluent"]),
    # ...but a heading with a colon does govern the bullets under it.
    ("Nice to have:\n - German language proficiency (native level)",
     ["German native (a plus)"]),
    ("We are a German company building German engineering software", []),
    ("Our office is in the German capital", []),
    ("No language requirement is stated in this posting", []),
)

_CONTRACT_FIXTURES: tuple[tuple[str, str], ...] = (
    ("Unbefristete Festanstellung in Vollzeit", "permanent · full-time"),
    ("This is a permanent position, full-time", "permanent · full-time"),
    ("Zunächst befristet auf 2 Jahre", "fixed-term"),
    ("Fixed-term contract, part-time possible", "fixed-term · part-time"),
    ("Freelance / contractor engagement", "freelance"),
    ("Teilzeit möglich", "part-time"),
    ("Werkstudent (m/w/d)", "working student"),
    ("A great role on a great team", ""),
)

_ARRANGEMENT_FIXTURES: tuple[tuple[str, bool, str], ...] = (
    ("Hybrid — 2 days per week in the office", False, "hybrid"),
    ("This is a fully remote role", False, "remote"),
    ("100% remote within the EU", False, "remote"),
    ("Homeoffice möglich", False, "remote"),
    ("Arbeit vor Ort in München", False, "onsite"),
    ("Hybrides Arbeiten mit Homeoffice-Anteil", False, "hybrid"),
    ("Nothing said about where you sit", True, "remote"),
    ("Nothing said about where you sit", False, ""),
)


def _self_test() -> int:
    failures = 0

    print("languages")
    for text, expected in _LANGUAGE_FIXTURES:
        got = languages(text)
        ok = got == expected
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {text[:54]!r:56} -> {got}"
              + ("" if ok else f"  (want {expected})"))

    print("\ncontract")
    for text, expected in _CONTRACT_FIXTURES:
        got = contract(text)
        ok = got == expected
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {text[:54]!r:56} -> {got!r}"
              + ("" if ok else f"  (want {expected!r})"))

    print("\narrangement")
    for text, flag, expected in _ARRANGEMENT_FIXTURES:
        got = arrangement(text, remote_flag=flag)
        ok = got == expected
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {text[:48]!r:50} flag={flag!s:5} -> {got!r}"
              + ("" if ok else f"  (want {expected!r})"))

    total = (len(_LANGUAGE_FIXTURES) + len(_CONTRACT_FIXTURES)
             + len(_ARRANGEMENT_FIXTURES))
    print(f"\n{total - failures}/{total} passed.")
    return 1 if failures else 0


def backfill(dry_run: bool = False) -> int:
    """Fill the three columns for postings stored before they existed.

    Reads only what is already in the database — no network, no re-fetch. A
    posting whose stored description is just the search-tile teaser yields
    little, which is a limit of that row, not of this pass.
    """
    from . import store

    filled = 0
    with store.connect() as conn:
        rows = list(conn.execute(
            """SELECT id, title, description, remote FROM postings
               WHERE COALESCE(languages, '') = ''
                 AND COALESCE(contract, '') = ''
                 AND COALESCE(arrangement, '') = ''"""
        ))
        for row in rows:
            title, body = row["title"] or "", row["description"] or ""
            if not body:
                continue
            langs = ", ".join(languages(title, body))
            kind = contract(title, body)
            where = arrangement(title, body, remote_flag=bool(row["remote"]))
            if not (langs or kind or where):
                continue
            filled += 1
            if not dry_run:
                conn.execute(
                    """UPDATE postings SET languages = ?, contract = ?,
                       arrangement = ? WHERE id = ?""",
                    (langs, kind, where, row["id"]),
                )
        if dry_run:
            conn.rollback()
    print(f"{filled} of {len(rows)} postings gained a reading"
          + (" (dry run, nothing written)" if dry_run else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--text", help="read one posting body")
    parser.add_argument("--backfill", action="store_true",
                        help="fill the columns for already-stored postings")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.backfill:
        return backfill(dry_run=args.dry_run)
    if args.text:
        print(f"languages:   {languages(args.text)}")
        print(f"contract:    {contract(args.text)!r}")
        print(f"arrangement: {arrangement(args.text)!r}")
        return 0
    return _self_test()


if __name__ == "__main__":
    raise SystemExit(main())
