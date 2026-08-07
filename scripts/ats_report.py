"""Keyword-coverage report for a finished application's CV.

**This is not a recruiter's ATS score, and it must never be presented as one.**
Workday, Greenhouse, SmartRecruiters and the rest each rank differently, most
weight fields the candidate cannot see, and none publish the formula. What this
module computes is a keyword-coverage proxy: of the terms this posting is built
around, how many does the rendered CV actually contain. That is a genuinely
useful thing to know before sending — it just isn't the number on the
recruiter's screen, and every caller renders it with that caveat attached.

Two numbers, reported separately, because they answer different questions:

* **Brief coverage** — against the `## Top ATS keywords` list in the Match
  Brief. Step 01A curated that list: it is integrity-filtered (nothing the
  profile cannot support) and vendor-neutral (rule 07 F2). High trust, and the
  one to act on. `null` when the run kept no brief — 4 of the 9 archived runs
  did not, and a missing brief must read as *unknown*, never as 0%.
* **Posting coverage** — against terms extracted from the raw job description
  by frequency. Nobody curated these, so the list contains noise by
  construction. Lower trust, shown beside the brief number rather than alone,
  and useful for exactly one thing: spotting a term the posting leans on that
  nothing in the pipeline noticed.

Scored against the **CV only**. An ATS parses the resume; the cover letter is
normally stored as an attachment and never keyword-scored.

Nothing here can fail a run. `qa_application.run()` embeds the result under its
`ats` key and leaves the verdict untouched, and every failure path in this
module returns a partial report rather than raising.

Usage:
    python scripts/ats_report.py "<folder>"
    python scripts/ats_report.py "<folder>" --posting-text description.txt
    python scripts/ats_report.py "<folder>" --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_deliverable import payload_dir  # noqa: E402
from cleanup_application import describe_folder  # noqa: E402
from latex_healthcheck import pdf_text_and_pages, read_text  # noqa: E402
from workspace_identity import load as load_identity  # noqa: E402


def _find_cv(folder: Path) -> Path | None:
    """The rendered CV, by name if the workspace knows it and by shape if not.

    `load_identity()` is called here rather than at import, because it raises on
    a workspace with no `identity.toml` — which is every fresh clone and every
    CI checkout, the file being deliberately untracked. Reading it at import
    time made this module unimportable there, so a keyword-coverage report that
    is optional by design took the whole test run down with it.

    The glob is not a fallback for that case alone: a German run renders a
    Lebenslauf, and the suffix identity supplies is the English one.
    """
    try:
        named = folder / load_identity().doc_name("CV", ".pdf")
    except (OSError, ValueError):
        named = None
    if named is not None and named.is_file():
        return named
    matches = sorted(folder.glob("*CV.pdf")) or sorted(folder.glob("*Lebenslauf.pdf"))
    return matches[0] if matches else None

#: Printed with every report. The caveat is not decoration — a coverage
#: percentage that looks like an ATS score is worse than no number at all,
#: because it invites the reader to trust a figure no vendor would recognise.
DISCLAIMER = (
    "Keyword coverage of the rendered CV, not a recruiter's ATS score — "
    "every ATS ranks differently and none publish their formula."
)

# ---------------------------------------------------------------------------
# Match Brief
# ---------------------------------------------------------------------------

#: Both spellings occur in the wild. Agents title the file the way CLAUDE.md
#: names the document ("Match Brief.md"); scripts write the slug form. Looking
#: for only one silently reports `null` on half the archive.
BRIEF_NAMES = ("match_brief.md", "Match Brief.md", "match brief.md",
               "Match_Brief.md", "MATCH_BRIEF.md")

#: Anchored on the stem only. The suffix drifts across runs — the archive holds
#: `(truthfully usable)`, `(truthful)` and `(truthful, from approved stack)` —
#: so matching the whole heading line finds nothing on two thirds of them.
_BRIEF_HEADING = re.compile(r"^##\s*Top ATS keywords.*$", re.IGNORECASE | re.MULTILINE)

#: A sentence the brief appends to the keyword list, reporting whether any
#: vendor name had to be substituted out under rule 07 F2. It is prose *about*
#: the list rather than a member of it, and it arrives both as its own paragraph
#: (Cint, Reply) and welded onto the end of the keyword paragraph after a full
#: stop (kausable) — which is why sentence-level filtering is needed and
#: paragraph-level is not enough.
_BRIEF_ASIDE = re.compile(
    r"\b(?:no\s+(?:ai[- ]vendor|substitution)|substitution\s+(?:flag|was|is)"
    r"|so\s+no\s+substitution|appears?\s+(?:anywhere\s+)?in\s+this\s+posting)\b",
    re.IGNORECASE,
)

#: A sentence end, which needs whitespace after the stop so `Node.js`, `CI/CD`
#: and `scikit-learn.` survive intact.
_SENTENCE = re.compile(r"(?<=\.)\s+")


def find_brief(folder: Path) -> Path | None:
    """The Match Brief for this application, wherever step 09 left it.

    The deliverable folder first (the run has not been cleaned yet), then
    `_tmp/payloads/<Company> <date> <Role>/` where `clean_deliverable` moves it.
    Both are legitimate: 06A runs before cleanup, a standalone debug run after.
    """
    candidates = [folder]
    moved = payload_dir(folder)
    if moved is not None:
        candidates.append(moved)
    for directory in candidates:
        for name in BRIEF_NAMES:
            path = directory / name
            if path.is_file():
                return path
    return None


def _split_list(text: str) -> list[str]:
    """Split a keyword list on its separators, ignoring bracketed ones.

    Commas separate the list in four archived briefs and semicolons in suena's,
    so both count. But a separator inside brackets belongs to the term: suena's
    `production ML delivery (Docker, CI/CD)` split naively into `production ML
    delivery (Docker` and `CI/CD)`, neither of which can ever match a CV.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char in ",;" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def brief_keywords(brief: Path) -> list[str]:
    """The `## Top ATS keywords` list, split on commas.

    The list is the **first** paragraph under the heading, in all five archived
    briefs that have one. Anything after it is commentary — Reply's brief spends
    a paragraph explaining why LangChain must not be used as a keyword, and
    comma-splitting that prose yields fragments like `use vendor-neutral
    phrasing — "LLM orchestration` as if they were skills. Taking only the first
    paragraph costs nothing real and removes that whole class of garbage.

    Within that paragraph the vendor-substitution note is dropped sentence by
    sentence, because kausable's brief ends the keyword list and starts the note
    inside the same paragraph.

    Everything surviving is taken at face value: step 01A already decided each
    entry is truthfully usable and vendor-neutral, and second-guessing it here
    would just be a worse copy of that judgement.
    """
    text = read_text(brief)
    heading = _BRIEF_HEADING.search(text)
    if heading is None:
        return []

    body = text[heading.end():]
    # Stop at the next heading, so a brief with an empty section cannot swallow
    # the requirement map that follows it.
    end = re.search(r"^#{1,6}\s", body, re.MULTILINE)
    if end is not None:
        body = body[:end.start()]

    paragraph = next((p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()), "")
    if not paragraph:
        return []

    keywords: list[str] = []
    for sentence in _SENTENCE.split(paragraph.replace("\n", " ")):
        if _BRIEF_ASIDE.search(sentence):
            continue
        for chunk in _split_list(sentence):
            term = chunk.strip().strip(".;").strip()
            # A real ATS term is a word or a short phrase. Anything longer is a
            # clause the comma split tore out of a sentence.
            if term and 1 <= len(term.split()) <= 6 and not _BRIEF_ASIDE.search(term):
                keywords.append(term)
    return _dedupe(keywords)


# ---------------------------------------------------------------------------
# Posting terms
# ---------------------------------------------------------------------------

#: Function words, posting boilerplate, and the rank/family words that describe
#: seniority rather than subject matter. The last group is seeded from
#: `automation/watcher/normalize.GENERIC_TITLE_TOKENS`, copied rather than
#: imported: `scripts/` must not depend on `automation/`, and the watcher's set
#: is tuned for title comparison, so the two are free to drift.
STOPWORDS = {
    # English function words
    "a", "about", "above", "across", "after", "all", "also", "an", "and", "any",
    "are", "as", "at", "be", "been", "being", "both", "but", "by", "can", "do",
    "does", "each", "for", "from", "had", "has", "have", "how", "if", "in",
    "into", "is", "it", "its", "may", "more", "most", "must", "no", "not", "of",
    "on", "one", "or", "other", "our", "out", "over", "own", "per", "should",
    "so", "some", "such", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "through", "to", "up", "us",
    "very", "was", "we", "well", "were", "what", "when", "where", "which",
    "while", "who", "will", "with", "within", "would", "you", "your", "yours",
    # German function words — many postings in this catchment are German
    "aber", "als", "am", "auch", "auf", "aus", "bei", "dass", "dem", "den",
    "der", "des", "die", "das", "du", "ein", "eine", "einem", "einen", "einer",
    "eines", "für", "fuer", "hat", "ihr", "im", "ist", "im", "mit", "nicht",
    "oder", "sich", "sie", "sind", "über", "ueber", "um", "und", "uns",
    "unsere", "unseren", "unserem", "von", "vor", "was", "werden", "wie",
    "wir", "zu", "zum", "zur",
    # Dutch function words. Not hypothetical: DPG Media's posting is written in
    # English but sits inside a Dutch site, and 190 KB of Dutch navigation drowned
    # the 3 KB ad — `je`, `en`, `de`, `van` and `het` took the whole top 25.
    "aan", "als", "bij", "dan", "dat", "deze", "door", "een", "en", "er", "het",
    "hun", "ik", "je", "jij", "jouw", "kan", "maar", "meer", "met", "naar",
    "niet", "of", "ons", "onze", "ook", "op", "te", "uit", "van", "voor",
    "wat", "worden", "zijn",
    # Posting boilerplate. Present in nearly every ad, informative in none.
    "ability", "applicants", "application", "apply", "benefits", "candidate",
    "candidates", "career", "company", "culture", "deep", "diverse",
    "diversity", "employee", "employees", "employer", "environment",
    "excellent", "experience", "field", "good", "great", "help", "high",
    "including", "join", "know", "knowledge", "like", "look", "looking",
    "make", "new", "offer", "opportunity", "part", "please", "plus",
    "position", "role", "salary", "skills", "strong", "team", "teams",
    "understanding", "want", "work", "working", "world", "year", "years",
    "erfahrung", "kenntnisse", "team", "unternehmen", "stelle", "bewerbung",
    # Rank and job family
    "engineer", "engineering", "scientist", "science", "developer", "analyst",
    "specialist", "manager", "architect", "consultant", "researcher", "expert",
    "senior", "junior", "lead", "principal", "staff", "associate", "mid",
    "level", "sr", "jr",
    # Page furniture. An archived posting is a whole rendered web page, so the
    # share buttons, cookie banner and nav bar arrive with it — Cint's capture
    # put `wechat` and `share` in the top 25 before this list existed.
    "accept", "consent", "cookie", "cookies", "facebook", "follow",
    "instagram", "linkedin", "login", "menu", "navigation", "newsletter",
    "policy", "privacy", "search", "share", "sign", "subscribe", "twitter",
    "wechat", "whatsapp", "xing", "youtube", "datenschutz", "impressum",
}

#: A word as it survives into a term. Keeps the punctuation that is part of a
#: technology's name — `C++`, `C#`, `Node.js`, `CI/CD`, `scikit-learn` — and
#: nothing else.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./\-]*")

#: Where one thought ends. n-grams never cross these, or "Python. Experience"
#: becomes a term.
_BREAK = re.compile(r"[.;:!?\n\r|•‣]|(?<=\s)[-–—](?=\s)")

#: A list item. The requirements of a posting live in bullets far more often
#: than in prose, so a term that appears in one is worth more than its raw
#: frequency suggests.
_BULLET = re.compile(r"^\s*(?:[-*•‣–—]|\d+[.)])\s+")

#: Phrases that demote a requirement to a bonus, in both languages. Copied in
#: spirit from `automation/watcher/terms._OPTIONAL` — same job, same wording,
#: independently maintained for the same import-direction reason as STOPWORDS.
_OPTIONAL = re.compile(
    r"\b(?:a\s+plus|of\s+advantage|von\s+vorteil|nice\s+to\s+have|"
    r"nice-to-have|beneficial|bonus|wünschenswert|wuenschenswert|"
    r"would\s+be\s+(?:a\s+)?(?:plus|great)|optional|not\s+required|"
    r"desirable|preferred\s+but\s+not)\b",
    re.IGNORECASE,
)

#: …except when the marker introduces a list, in which case it governs every
#: bullet beneath it until the next heading. The colon is what tells the two
#: apart, exactly as `terms._HEADING` does for language requirements.
_OPTIONAL_HEADING = re.compile(r":\s*$")

MAX_POSTING_TERMS = 25


#: A contraction's tail. Apostrophes are not word characters here, so without
#: this "you're" tokenises to "you" + "re" and `re` climbs into the top 25.
_CONTRACTION = re.compile(r"['‘’ʼ](?:s|re|ve|ll|d|t|m)\b", re.IGNORECASE)


def _ngrams(line: str, size: int) -> list[tuple[str, ...]]:
    """Every `size`-word window in one line, not crossing a sentence break."""
    grams: list[tuple[str, ...]] = []
    for segment in _BREAK.split(_CONTRACTION.sub("", line)):
        words = [w.strip("-./").lower() for w in _WORD.findall(segment)]
        words = [w for w in words if w]
        for start in range(len(words) - size + 1):
            grams.append(tuple(words[start:start + size]))
    return grams


def _informative(gram: tuple[str, ...], exclude: frozenset[str] = frozenset()) -> bool:
    """Whether an n-gram is worth counting.

    A term may not start or end on a stopword — "of machine" and "learning and"
    are artifacts of the window, not phrases — and a term made entirely of
    stopwords carries nothing at all.
    """
    if any(len(word) < 2 for word in gram):
        return False
    if any(word in exclude for word in gram):
        return False
    if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
        return False
    return not all(word in STOPWORDS for word in gram)


def posting_terms(text: str, limit: int = MAX_POSTING_TERMS,
                  exclude: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    """Required and optional terms extracted from a job description.

    Deterministic and frequency-based: 1-3 word n-grams, kept when they recur or
    sit in a requirements bullet, ranked by weighted frequency, longest phrase
    winning over the words inside it.

    `exclude` drops terms containing a given word — the caller passes the
    company's own name, which saturates its own ad ("Cint Exchange" ranked 10th
    in Cint's) while being the one term a CV has no reason to carry.

    This extractor is noisy and is meant to be — it is why the posting number is
    labelled a proxy and never shown without the brief number beside it.
    """
    required: Counter[tuple[str, ...]] = Counter()
    optional: Counter[tuple[str, ...]] = Counter()
    heading_optional = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        bullet = _BULLET.search(line) is not None
        marked = _OPTIONAL.search(line) is not None
        if marked and _OPTIONAL_HEADING.search(line):
            # "Nice to have:" — everything under it is a bonus until the list ends.
            heading_optional = True
            continue
        if not bullet and not marked:
            # Plain prose ends the run of bullets a heading was governing.
            heading_optional = False

        target = optional if (marked or heading_optional) else required
        # A bullet is a claim about the job; prose around it is often company
        # blurb. Weighting the bullet is what stops "our people" outranking
        # "feature engineering" in a posting with a long culture section.
        weight = 2 if bullet else 1
        for size in (1, 2, 3):
            for gram in _ngrams(line, size):
                if _informative(gram, exclude):
                    target[gram] += weight

    # A term that appears once in prose is as likely to be an accident as a
    # requirement; one inside a bullet was written deliberately.
    kept = {gram: count for gram, count in required.items() if count >= 2}
    ranked = sorted(kept.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))

    chosen: list[tuple[str, ...]] = []
    for gram, _count in ranked:
        # Prefer the phrase over its parts: with "machine learning" already
        # kept, "machine" and "learning" add nothing but two more misses.
        if any(_contains(longer, gram) for longer in chosen):
            continue
        chosen.append(gram)
        if len(chosen) >= limit:
            break

    optional_only = [
        gram for gram, count in optional.most_common()
        if count >= 2 and gram not in kept and not any(_contains(c, gram) for c in chosen)
    ][:limit]

    return ([" ".join(g) for g in chosen], [" ".join(g) for g in optional_only])


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    """Whether `shorter` appears as a contiguous run inside `longer`."""
    if len(shorter) >= len(longer):
        return False
    return any(longer[i:i + len(shorter)] == shorter
               for i in range(len(longer) - len(shorter) + 1))


# ---------------------------------------------------------------------------
# Posting text
# ---------------------------------------------------------------------------

#: A SingleFile capture inlines every image, stylesheet and script, so these run
#: to megabytes — the Roche archive is 9.7 MB. Reading more than this buys
#: nothing: the posting itself is a few thousand characters and sits well inside
#: it, and the rest is base64.
MAX_HTML_BYTES = 12_000_000
MAX_POSTING_CHARS = 200_000

_HTML_DROP = re.compile(
    r"<(script|style|svg|noscript|head|template)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: Unbounded on purpose. A SingleFile capture inlines images as `data:` URIs
#: inside the attribute, so a single `<img>` tag can run to hundreds of
#: kilobytes; a length-capped tag pattern simply fails to match it and the whole
#: base64 blob arrives as if it were posting text. `[^>]*` is linear, so there
#: is no backtracking cost to removing the cap.
_HTML_TAG = re.compile(r"<[^>]*>", re.DOTALL)
#: Truncating a multi-megabyte archive can cut a tag in half, leaving an opening
#: `<script src="data:…` with no closing `>` for anything to match against.
_HTML_TRAILING = re.compile(r"<[^>]*$", re.DOTALL)
_HTML_ENTITY = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&ndash;": "-", "&mdash;": "-",
}


def html_to_text(html: str) -> str:
    """Plain text from an archived posting, by regex rather than a parser.

    Deliberately not bs4: this module is stdlib + pypdf so `qa_application` can
    call it in any environment, and a keyword-frequency proxy does not need a
    correct DOM. Block tags become newlines so the bullet detection above still
    has lines to work with.
    """
    text = _HTML_COMMENT.sub(" ", html)
    text = _HTML_DROP.sub(" ", text)
    text = _HTML_TRAILING.sub(" ", text)
    text = re.sub(r"</(p|div|li|tr|h[1-6]|section|article)\s*>", "\n",
                  text, flags=re.IGNORECASE)
    text = re.sub(r"<(br|li)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG.sub(" ", text)
    for entity, char in _HTML_ENTITY.items():
        text = text.replace(entity, char)
    # Numeric entities, decimal and hex. The hex form matters: `&#xa0;` is how
    # several boards write a non-breaking space, and leaving it produced a term
    # literally called "xa0" in Cint's report.
    text = re.sub(r"&#[xX][0-9a-fA-F]{1,6};|&#\d{1,6};|&[a-zA-Z]{2,10};", " ", text)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines
                     if line and not _is_residue(line))[:MAX_POSTING_CHARS]


#: A base64 payload, a minified script or a hashed id — never a sentence. Real
#: prose puts a space in long before this.
_LONG_TOKEN = re.compile(r"\S{45,}")

#: An attribute name that survived as text, e.g. `data-ph-at-id=jobs-list-item`.
_ATTRIBUTE = re.compile(r"\b[a-z][a-z-]{2,}=[\"']?[\w-]")


def _is_residue(line: str) -> bool:
    """Whether a stripped line is markup debris rather than posting text.

    Tag removal alone is not enough. A truncated archive leaves half-written
    tags behind, JSON-LD blocks leave `null`/`true` soup, and Phenom People's
    career pages leave `data-ph-at-id` attribute names as bare text — which is
    how HelloFresh's first report came back reporting the CV was missing
    `au-target-id`.
    """
    if _LONG_TOKEN.search(line) or "base64," in line:
        return True
    if "{" in line or "};" in line or "</" in line or "/>" in line:
        return True
    return _ATTRIBUTE.search(line) is not None


#: Phrases that only appear in an actual job ad, in both languages. Two distinct
#: hits is the bar for believing an extraction worked.
_POSTING_ANCHOR = re.compile(
    r"\b(?:responsibilit\w+|qualification\w*|requirement\w*|what\s+you'?ll|"
    r"what\s+you\s+(?:will\s+)?(?:do|bring)|your\s+(?:profile|role|tasks)|"
    r"who\s+you\s+are|we\s+offer|about\s+the\s+role|years?\s+of\s+experience|"
    r"experience\s+(?:with|in)|you\s+will\s+be|nice\s+to\s+have|"
    r"aufgaben|profil|anforderung\w*|qualifikation\w*|wir\s+bieten|"
    r"das\s+bringst\s+du|deine\s+aufgaben|ihre\s+aufgaben|berufserfahrung)\b",
    re.IGNORECASE,
)

#: Below this a "posting" is a nav bar and a cookie banner.
MIN_POSTING_CHARS = 600

#: How much text one job ad occupies. The watcher's stored descriptions run to
#: about 8,000 characters, so this leaves generous headroom while still being a
#: small fraction of a captured page.
POSTING_WINDOW = 12_000
_WINDOW_STEP = 2_000


def focus_window(text: str) -> str:
    """The stretch of a captured page that actually holds the job ad.

    A SingleFile archive is the whole rendered site: navigation, cookie banner,
    every other vacancy on the careers page, and the footer. DPG Media's capture
    is 190 KB of Dutch site chrome around a 3 KB English posting, and ranking
    terms across all of it measured the website rather than the job — the top
    twenty-five came back as `je`, `en`, `de`, `van`.

    Chasing that with per-language stopword lists is endless. The ad is instead
    a *contiguous region*, so this slides a posting-sized window across the text
    and keeps the one densest in job-ad phrasing and requirement bullets.
    """
    if len(text) <= POSTING_WINDOW:
        return text

    best, best_score = 0, -1
    for start in range(0, len(text) - POSTING_WINDOW + _WINDOW_STEP, _WINDOW_STEP):
        window = text[start:start + POSTING_WINDOW]
        # Distinct phrases, not raw hits: a careers page repeating "we offer"
        # under twenty job cards should not outscore one real requirements list.
        anchors = len({m.group(0).casefold() for m in _POSTING_ANCHOR.finditer(window)})
        bullets = sum(1 for line in window.splitlines() if _BULLET.search(line))
        score = anchors * 3 + bullets
        if score > best_score:
            best, best_score = start, score
    return text[best:best + POSTING_WINDOW]


def looks_like_posting(text: str) -> bool:
    """Whether extracted text is plausibly a job ad.

    Four of the eight archived runs are Stepstone captures whose posting body is
    rendered client-side and simply is not in the saved HTML — all that survives
    stripping is "Job finden Job posten Login". Scoring a CV against that
    returned 0-33% and named `job` and `finden` as missing keywords, which is
    not a low score but a broken measurement, and reporting it as a number would
    be worse than reporting nothing.

    So: enough text, and at least two distinct job-ad phrases in it. Anything
    that fails becomes `null` with a stated reason, and the headless path — which
    passes the watcher's already-clean `postings.description` via
    `--posting-text` — sails through it.
    """
    if len(text) < MIN_POSTING_CHARS:
        return False
    hits = {m.group(0).casefold() for m in _POSTING_ANCHOR.finditer(text)}
    return len(hits) >= 2


def find_posting_text(folder: Path, override: Path | None = None) -> tuple[str, str]:
    """The job description, and where it came from.

    Headless runs pass `--posting-text` with `postings.description`, which is
    already clean text and is far better input than re-parsing the archive.
    Interactively there is only the `.html`, so that is what gets stripped.
    """
    if override is not None:
        if override.is_file():
            return read_text(override)[:MAX_POSTING_CHARS], override.name
        return "", ""
    for archive in sorted(folder.glob("*.html")):
        try:
            raw = archive.read_bytes()[:MAX_HTML_BYTES].decode("utf-8", "replace")
        except OSError:
            continue
        text = html_to_text(raw)
        if text:
            return text, archive.name
    return "", ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

#: Below this length a term is too short to rescue from a kerning split without
#: inviting nonsense: `A\s?I` would match the "A I" of an initialism, and a term
#: wrongly counted as present is the one failure this report cannot afford.
LOOSE_MIN_CHARS = 5


def _alternatives(keyword: str) -> list[str]:
    """A keyword's interchangeable forms.

    Briefs write alternatives with slashes and ampersands — `LLM & NLP`,
    `KPI/performance monitoring`, `anomaly detection / decision-support
    systems`. Any one of them being present means the CV carries the idea, so
    reporting the whole compound as missing because one third of it is absent
    would be wrong.
    """
    parts = [p.strip() for p in re.split(r"\s*[/&]\s*", keyword) if p.strip()]
    return parts or [keyword]


def _term_pattern(term: str, loose: bool = False) -> str:
    """One term as a regex over PDF-extracted text.

    Words are joined by "any whitespace, hyphen or slash" so a phrase survives
    the line wrap a PDF puts in the middle of it, and the final word tolerates a
    plural either way round — a brief asking for `data pipelines` is satisfied
    by a CV saying `data pipeline`.

    `loose` additionally allows a space *inside* a word, which is how pypdf
    renders a kerning pair: the CV's `PyTorch` comes back as `PyT orch` on every
    document ever built. Only used as a second attempt, and only on terms long
    enough that the extra tolerance cannot manufacture a match.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9+#]+", term) if w]
    if not words or sum(len(w) for w in words) < 2:
        return re.escape(term)

    def one(word: str) -> str:
        return r"\s?".join(re.escape(c) for c in word) if loose else re.escape(word)

    last = words[-1]
    if len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    body = [one(w) for w in words[:-1]] + [one(last) + r"(?:e?s)?"]
    return r"\b" + r"[\s\-/]+".join(body) + r"\b"


def match_terms(terms: list[str], text: str) -> tuple[list[str], list[str], list[str]]:
    """Split `terms` into (matched, missing, rescued-by-loose-matching).

    The third list becomes `parse_warnings`. A term only ever lands there when
    the strict pattern failed and the loose one succeeded, which means the CV
    does contain the word and the extractor broke it — reporting `PyTorch`
    missing when most parsers read it fine is precisely the false alarm that
    makes a coverage number worthless.
    """
    matched: list[str] = []
    missing: list[str] = []
    rescued: list[str] = []

    for term in terms:
        alternatives = _alternatives(term)
        if any(re.search(_term_pattern(alt), text, re.IGNORECASE) for alt in alternatives):
            matched.append(term)
            continue
        long_enough = [a for a in alternatives if len(a.replace(" ", "")) >= LOOSE_MIN_CHARS]
        if any(re.search(_term_pattern(alt, loose=True), text, re.IGNORECASE)
               for alt in long_enough):
            matched.append(term)
            rescued.append(term)
            continue
        missing.append(term)

    return matched, missing, rescued


def _dedupe(terms: list[str]) -> list[str]:
    """Order-preserving, case-insensitive."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _coverage(matched: list[str], missing: list[str]) -> float | None:
    total = len(matched) + len(missing)
    return round(len(matched) / total, 4) if total else None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

#: Legal-form suffixes, which say nothing about who the company is and would
#: otherwise blacklist a word as ordinary as "group".
_COMPANY_NOISE = {"gmbh", "ag", "se", "kg", "mbh", "co", "inc", "ltd", "llc",
                  "plc", "bv", "nv", "sa", "srl", "group", "holding", "the"}


def _company_words(folder: Path) -> frozenset[str]:
    """The company's own name, from the folder it was scaffolded into.

    Read from the path rather than the posting because that is where it is
    already authoritative — `<YYYY>/<Company>/<date> - <Role>` is the naming
    contract every other script in this pipeline relies on.
    """
    try:
        described = describe_folder(folder)
    except Exception:  # noqa: BLE001
        return frozenset()
    if described is None:
        return frozenset()
    words = re.findall(r"[a-z0-9]+", described[0].casefold())
    return frozenset(w for w in words if len(w) > 1 and w not in _COMPANY_NOISE)


def report(folder: Path, posting_text: Path | None = None) -> dict:
    """The full report for one application folder.

    Never raises on missing input. A folder with no CV, no brief and no archive
    returns a report whose three sections are all `null` — which is the honest
    answer, and one `qa_application` can embed without changing its verdict.
    """
    result: dict = {
        "cv": None,
        "brief": None,
        "posting": None,
        "skipped": {},
        "parse_warnings": [],
        "note": DISCLAIMER,
    }

    cv = _find_cv(folder)
    if cv is None:
        result["skipped"] = {"cv": f"no CV PDF in {folder.name}"}
        return result
    result["cv"] = cv.name

    _pages, cv_text, _errors = pdf_text_and_pages(cv)
    if not cv_text.strip():
        result["skipped"] = {"cv": f"{cv.name} has no extractable text layer"}
        return result

    warnings: list[str] = []
    skipped: dict[str, str] = {}

    brief = find_brief(folder)
    keywords = brief_keywords(brief) if brief is not None else []
    if keywords:
        matched, missing, rescued = match_terms(keywords, cv_text)
        warnings.extend(rescued)
        result["brief"] = {
            "coverage": _coverage(matched, missing),
            "matched": matched,
            "missing": missing,
            "total": len(keywords),
            "source": brief.name,
        }
    elif brief is None:
        skipped["brief"] = "no Match Brief archived for this application"
    else:
        skipped["brief"] = f"{brief.name} has no '## Top ATS keywords' list"

    text, origin = find_posting_text(folder, posting_text)
    required: list[str] = []
    if text and looks_like_posting(text):
        required, optional = posting_terms(focus_window(text),
                                           exclude=_company_words(folder))
        if required:
            matched, missing, rescued = match_terms(required, cv_text)
            warnings.extend(rescued)
            _opt_matched, opt_missing, opt_rescued = match_terms(optional, cv_text)
            warnings.extend(opt_rescued)
            result["posting"] = {
                "coverage": _coverage(matched, missing),
                "matched": matched,
                "missing": missing,
                "optional_missing": opt_missing,
                "total": len(required),
                "source": origin,
            }
    if result["posting"] is None:
        skipped["posting"] = (
            "no posting text available" if not text
            else f"{origin} holds no readable job description "
                 f"(client-rendered page); pass --posting-text"
        )

    result["skipped"] = skipped
    result["parse_warnings"] = _dedupe(warnings)
    return result


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def summary_line(data: dict, max_missing: int = 3) -> str:
    """One line for a notification, or "" when there is nothing to say.

    Deliberately short and deliberately optional: this goes into the Telegram
    message announcing that an application is ready, and that message must
    survive this module returning nothing useful.
    """
    brief = (data or {}).get("brief") or {}
    posting = (data or {}).get("posting") or {}
    if brief.get("coverage") is None and posting.get("coverage") is None:
        return ""

    parts = []
    if brief.get("coverage") is not None:
        parts.append(f"{_pct(brief['coverage'])} brief")
    if posting.get("coverage") is not None:
        parts.append(f"{_pct(posting['coverage'])} posting")
    line = "ATS " + " · ".join(parts)

    missing = brief.get("missing") or posting.get("missing") or []
    if missing:
        shown = ", ".join(missing[:max_missing])
        if len(missing) > max_missing:
            shown += f" +{len(missing) - max_missing} more"
        line += f" — missing: {shown}"
    return line


def _print_human(data: dict) -> None:
    print(f"CV: {data.get('cv') or '(none found)'}")
    for label, key in (("Brief", "brief"), ("Posting", "posting")):
        section = data.get(key)
        if section is None:
            reason = (data.get("skipped") or {}).get(key, "not measured")
            print(f"{label} coverage: n/a ({reason})")
            continue
        print(f"{label} coverage: {_pct(section['coverage'])} "
              f"({len(section['matched'])}/{section['total']}) — {section['source']}")
        if section["missing"]:
            print(f"  missing: {', '.join(section['missing'])}")
        if section.get("optional_missing"):
            print(f"  missing (nice-to-have): {', '.join(section['optional_missing'])}")
    if data.get("parse_warnings"):
        print(f"Parse warnings (present in the CV, split by the PDF text "
              f"extractor): {', '.join(data['parse_warnings'])}")
    print(data.get("note", DISCLAIMER))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", help="Application folder holding the rendered CV PDF")
    parser.add_argument("--posting-text", type=Path, default=None,
                        help="File holding the job description as plain text. "
                             "Preferred over re-parsing the archived .html.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)

    data = report(Path(args.folder), args.posting_text)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _print_human(data)
    # Always 0. This report measures; it never judges.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
