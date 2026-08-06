"""Canonical posting shape, plus the normalisation that makes dedupe work.

The interesting problem here is that the same role reaches us under several
names. `Deutsche Börse Group`, `Deutsche Boerse Group` and `Deutsche Börse AG`
are all one employer and all already exist as separate folders under 2026/.
`AI Engineer (m/w/d)` and `AI Engineer` are one role. A Greenhouse URL and the
hiring.cafe mirror of it are one posting.

So normalisation happens in three places, at increasing looseness:

    fingerprint()        exact-ish: never notify about the same posting twice
    company_key()        employer identity across spellings
    title_similarity()   role identity across seniority prefixes and gender tags
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that carry tracking rather than identity. Stripping these
# stops the same posting arriving as "new" via a different referral link.
TRACKING_PARAMS = {
    "gh_src", "gh_jid", "lever-source", "lever-origin",
    "ref", "referrer", "source", "src", "origin",
    "trk", "trackingid", "tracking_id", "recommended",
    "sessionid", "session_id", "jsessionid",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}

# Corporate suffixes that differ between how a company writes itself on its own
# job board and how an aggregator writes it.
#
# No entry here may contain punctuation. `company_key` replaces every non
# alphanumeric character with a space before it tokenises, so a `"b.v."` entry
# could never match anything — the dotted form arrives as the two tokens `b` and
# `v`. Dotted abbreviations are folded back together instead (see DOTTED_ABBREV),
# which is what makes the `bv`/`nv`/`sa`/`as` entries below actually fire.
LEGAL_SUFFIXES = {
    "gmbh", "ag", "aktiengesellschaft", "se", "kg", "kgaa", "ohg", "mbh", "ug",
    "inc", "llc", "ltd", "limited", "plc", "corp", "corporation",
    "bv", "nv", "sa", "as", "ab", "oy", "aps",
    "co", "company", "group", "holding", "holdings", "international",
    "deutschland", "germany", "europe", "eu",
}

# A run of single letters each followed by a dot: `B.V.`, `N.V.`, `S.A.`, `A.S.`.
# Folded to `bv`, `nv`, … so they reach LEGAL_SUFFIXES as one token. Requiring at
# least two groups is what keeps an ordinary sentence-ending initial out of it.
DOTTED_ABBREV = re.compile(r"(?:\b[a-z]\.){2,}")

# German job ads bolt a gender tag onto nearly every title.
GENDER_TAG = re.compile(
    r"\(\s*[mwdfxa](?:\s*[/|–-]\s*[mwdfxa])+\s*\)|\(\s*all\s+genders?\s*\)|\bm/w/d\b|\bf/m/d\b|\bm/f/d\b|\bd/m/w\b",
    re.IGNORECASE,
)

# Noise that appears in titles but says nothing about the role.
TITLE_NOISE = re.compile(
    r"\b(full[- ]?time|part[- ]?time|permanent|unbefristet|vollzeit|teilzeit"
    r"|remote|hybrid|onsite|on[- ]site|festanstellung|w/m/d|job|stelle)\b",
    re.IGNORECASE,
)

UMLAUT_MAP = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "å": "a", "ø": "o", "æ": "ae",
})


def strip_accents(value: str) -> str:
    """Fold accents after expanding German umlauts to their two-letter forms.

    Order matters: `ü` must become `ue` (matching how Germans transliterate)
    rather than the bare `u` that NFKD would leave behind.
    """
    value = value.translate(UMLAUT_MAP)
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def company_key(name: str) -> str:
    """Employer identity, insensitive to spelling, legal form, and accents.

    The legal form has to go, and go in every spelling the same employer uses.
    A posting said `PFALZWERKE AKTIENGESELLSCHAFT` while the application folder
    said `PFALZWERKE`; the two keyed differently, so a finished application was
    reported as a failed build and would not have blocked a rebuild either.
    """
    text = strip_accents(name or "").casefold()
    text = DOTTED_ABBREV.sub(lambda m: m.group(0).replace(".", ""), text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t and t not in LEGAL_SUFFIXES]
    if not tokens:  # a name made entirely of suffixes — keep something
        tokens = text.split()
    return " ".join(tokens)


def clean_title(title: str) -> str:
    """Human-facing title with gender tags and boilerplate removed."""
    text = GENDER_TAG.sub(" ", title or "")
    text = TITLE_NOISE.sub(" ", text)
    text = re.sub(r"[–—]", "-", text)
    text = re.sub(r"\s*[-|/,]\s*$", "", text)
    text = re.sub(r"\(\s*\)", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -|,")


def title_key(title: str) -> str:
    """Role identity for comparison. Lossier than clean_title."""
    text = strip_accents(clean_title(title)).casefold()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


# Words that describe rank or job family rather than subject matter. Splitting
# these out is what lets `Data Scientist` / `Senior Data Scientist` count as the
# same role while `Data Scientist` / `Data Engineer` does not.
GENERIC_TITLE_TOKENS = {
    "engineer", "engineering", "scientist", "science", "developer", "analyst",
    "specialist", "manager", "architect", "consultant", "researcher", "expert",
    "senior", "junior", "lead", "principal", "staff", "associate", "mid",
    "level", "i", "ii", "iii", "sr", "jr", "and", "of", "for", "the",
}


def _containment(left: set[str], right: set[str]) -> float:
    """Overlap relative to the smaller set. Both empty is a match; one empty is not."""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def title_similarity(left: str, right: str) -> float:
    """0..1 similarity between two role titles.

    Plain string similarity is unusable here: `AI Engineer` and `Data Engineer`
    score 0.83 on SequenceMatcher purely because they share the word
    `Engineer`, which would suppress a legitimate application. Plain token
    Jaccard is too strict the other way — it scores `Data Scientist` against
    `Senior Data Scientist` at 0.67 and lets us apply to the same job twice.

    So tokens are split into subject words and rank/family words, and both
    halves must agree before extra words are forgiven. `Senior` being added is
    then free, while `Data` becoming `AI` is not.
    """
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    ta, tb = set(a.split()), set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0

    subject_a, subject_b = ta - GENERIC_TITLE_TOKENS, tb - GENERIC_TITLE_TOKENS
    family_a, family_b = ta & GENERIC_TITLE_TOKENS, tb & GENERIC_TITLE_TOKENS
    structural = min(_containment(subject_a, subject_b),
                     _containment(family_a, family_b))

    return max(jaccard, structural)


def canonical_url(url: str) -> str:
    """URL with tracking parameters and trailing slashes removed."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunsplit((parts.scheme.lower() or "https", netloc, path,
                       urlencode(sorted(query)), ""))


#: A start or end tag with a real element name. Deliberately stricter than
#: "contains a < and a >": `<5 years experience` and `a < b > c` are prose, and
#: handing either to an HTML parser deletes the middle of the sentence without
#: saying so.
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")

#: How many strip-then-unescape rounds to run before accepting the result.
#: Greenhouse serves HTML that has itself been entity-encoded, so a single
#: round unescapes `&lt;p&gt;` into a live `<p>` and stops there — every
#: Greenhouse body reached the scorer wrapped in its own markup, tag attributes
#: and all. Two rounds settle that; the third only confirms nothing changed.
#: Bounded rather than "until stable" because an input can be built to
#: alternate for ever.
_DECODE_PASSES = 3


def _strip_tags(text: str) -> str:
    if not _TAG.search(text):
        return text
    try:
        from bs4 import BeautifulSoup  # imported lazily; only ATS HTML needs it

        return BeautifulSoup(text, "html.parser").get_text("\n")
    except Exception:  # noqa: BLE001 - a missing parser must not lose the body
        return re.sub(r"<[^>]+>", " ", text)


def to_text(value: str | None) -> str:
    """HTML (or plain text) to readable plain text, whitespace collapsed."""
    if not value:
        return ""
    import html as _html

    text = value
    for _ in range(_DECODE_PASSES):
        decoded = _html.unescape(_strip_tags(text))
        if decoded == text:
            break
        text = decoded
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_date(value: Any) -> dt.date | None:
    """Best-effort date parsing across the formats the sources actually emit."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds; Workday sometimes seconds.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Workday-style relative strings: "Posted 3 Days Ago"
    rel = re.search(r"(\d+)\+?\s*(day|week|month)s?\s*ago", text, re.IGNORECASE)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2).lower()
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return dt.date.today() - dt.timedelta(days=days)
    if re.search(r"\b(today|heute|just posted)\b", text, re.IGNORECASE):
        return dt.date.today()
    return None


@dataclass
class Posting:
    """One job posting, normalised across every source.

    `description` may be empty at fetch time. Sources that need a second HTTP
    call for the body leave `detail_url` set; hydration happens only for
    postings that survive the free prefilter, so we never pay for descriptions
    we are going to throw away.
    """

    source: str                       # source_key(), e.g. "ats:Bayer"
    provider: str                     # greenhouse | lever | arbeitsagentur | ...
    source_job_id: str
    url: str
    company: str
    title: str
    location: str = ""
    country: str = ""
    remote: bool = False
    posted_at: dt.date | None = None
    description: str = ""
    detail_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.company = (self.company or "").strip()
        self.title = clean_title(self.title)
        self.location = (self.location or "").strip()
        self.url = (self.url or "").strip()

    @property
    def canonical_url(self) -> str:
        return canonical_url(self.url)

    @property
    def fingerprint(self) -> str:
        """Stable id. Two records with this id are the same posting.

        Built from employer + role + URL path rather than the source's own job
        id, so the same role listed on a company board and mirrored by an
        aggregator collapses to one entry.
        """
        path = urlsplit(self.canonical_url).path.rstrip("/")
        basis = "|".join((company_key(self.company), title_key(self.title), path))
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def loose_key(self) -> str:
        """Employer + role + country. Collapses the same job across sources.

        Country has to be in here. Large employers list one title in a dozen
        countries, and without it the Basel opening and the Hyderabad opening
        collapse into a single record — whichever arrived first wins, and if
        that was the one outside the target region, the good one is silently
        discarded by the country prefilter.
        """
        return f"{company_key(self.company)}|{title_key(self.title)}|{self.country.upper()}"

    @property
    def age_days(self) -> int | None:
        if not self.posted_at:
            return None
        return (dt.date.today() - self.posted_at).days

    # --- derived readings --------------------------------------------------
    # Properties rather than fields on purpose. `description` arrives empty from
    # sources that need a second HTTP call and is filled in later by hydration,
    # so a value computed once at construction would be stale for exactly the
    # postings that survive far enough to matter.

    @property
    def city(self) -> str:
        """Canonical city spelling, or "" when the location does not name one."""
        from . import geo

        return geo.city_of(self.location, self.raw.get("location", ""))

    @property
    def level(self) -> str:
        """Seniority band, or `unknown`. See roles.level_of."""
        from . import roles

        return roles.level_of(self.title, self.description)

    @property
    def years_required(self) -> int | None:
        """Highest stated years-of-experience bar, or None."""
        from . import roles

        return roles.years_required(self.description)

    @property
    def languages(self) -> list[str]:
        """Stated language bar, e.g. `["German C1", "English fluent"]`."""
        from . import terms

        return terms.languages(self.title, self.description)

    @property
    def contract(self) -> str:
        """Contract type and hours, e.g. `permanent · full-time`, or ""."""
        from . import terms

        return terms.contract(self.title, self.description)

    @property
    def arrangement(self) -> str:
        """`remote`, `hybrid`, `onsite`, or "" when the posting does not say."""
        from . import terms

        return terms.arrangement(self.title, self.description,
                                 remote_flag=self.remote)

    def summary(self) -> str:
        where = self.location or ("Remote" if self.remote else "?")
        age = f"{self.age_days}d" if self.age_days is not None else "?"
        return f"{self.company} — {self.title} · {where} · {age} · {self.provider}"
