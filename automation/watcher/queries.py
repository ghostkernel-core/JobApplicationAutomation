"""Portal search terms, written from the profile instead of by hand.

`sources.toml` ships four search terms per portal — "Machine Learning
Engineer", "Data Scientist", "AI Engineer", "Product Engineer" — and those
four *are* the entire aperture of the three aggregators. Everything downstream
can only narrow what they return, so a role the profile is qualified for that
nobody thought to type is not ranked badly: it is never fetched at all, and
nothing anywhere reports its absence. Widening the list by hand means guessing
at the vocabulary employers use for work the candidate has actually done,
which is the one thing the profile digest already spells out.

So one model call turns that digest into a query set per portal, cached
against the digest text. A poll cycle pays for it only when the profile
changes, which is the same bargain `profile.get_digest` itself strikes one
level up.

Three properties matter more than the query text does.

**A bad generation narrows nothing.** A generated set replaces a portal's
hand-written list only when enough of it validates; below `MIN_QUERIES` that
portal keeps `sources.toml` exactly as written. The fallback is per portal,
not global, because the three do not fail together — arbeitsagentur is asked
for German and the other two for English, and it is entirely possible for one
of those halves to come back unusable.

**It never touches the entry it is handed.** `_Reloader` caches the parsed
`Sources` and hands the same dict objects to every caller, so mutating one
would poison that cache until the next file edit — the poll would keep running
generated queries after the feature was switched back off. `for_entry` returns
a new dict and leaves the original alone.

**It validates hard, because both failure modes are silent.** A malformed
query can 4xx a fragile portal, and a structural failure *parks* the source
with no automatic retry, so one bad string can take StepStone off the board
until somebody reads a status page. A location term is worse than malformed:
every portal takes location as its own field, so a city inside the query text
double-filters and returns an empty result set that is indistinguishable from
a quiet day.

The matching preferences (`profile_kb.md`) are deliberately *not* an input
here. They exist to say "stop showing me X", and this stage's whole job is to
widen what is seen; narrowing is what triage and the matcher are for. Keeping
them out is also what makes the cache key honest — it is the digest text and
nothing else, so a whitespace-only profile edit that condenses to the same
digest does not spend a call regenerating identical queries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from typing import Any, Sequence

from . import profile, store
from .claude_cli import ClaudeError, run_json
from .config import SEARCH_QUERIES_PATH, Config, load_config, load_sources
from .logsetup import force_utf8, setup

log = logging.getLogger("watcher.queries")

#: Below this many valid queries, a portal keeps its hand-written list. Two is
#: the point at which a generated set is still a set rather than a single
#: lucky string standing in for four deliberate ones.
MIN_QUERIES = 2

#: One word matches half the board and buries everything in noise; seven is a
#: sentence, and every one of these portals treats a long query as an implicit
#: AND of all its words, which matches nothing.
MIN_WORDS, MAX_WORDS = 2, 6
MAX_CHARS = 60

#: Which portals want their queries in German. Not derived from `location` —
#: hiring.cafe and StepStone both index German employers and both search in
#: English, and StepStone in particular returns far less for a German query.
PORTAL_LANGUAGE = {"arbeitsagentur": "German"}
DEFAULT_LANGUAGE = "English"

# Boolean and wildcard syntax. None of the three portals document supporting
# any of it, and StepStone 4xxs on unbalanced quotes — which is a structural
# failure, which parks the source.
_OPERATORS = re.compile(r"""["'()\[\]{}|&+*~<>!?:;,\\]|(?:^|\s)-\S""")
_BOOLEAN_WORDS = {"and", "or", "not", "und", "oder", "nicht"}

#: Geography, in any of the languages these three portals are read in. Every
#: portal takes location as a separate field, so any of these inside the query
#: text is a double filter — and an empty result set looks exactly like a
#: quiet day, which is why this is a hard reject rather than a warning.
_GEO_WORDS = {
    # arrangement, which portals also model as their own field
    "remote", "onsite", "on-site", "hybrid", "vor", "ort", "homeoffice",
    "worldwide", "international", "nationwide", "relocation", "umkreis",
    # regions and countries
    "europe", "european", "eu", "emea", "dach", "germany", "deutschland",
    "german", "deutsch", "austria", "österreich", "switzerland", "schweiz",
    "netherlands", "niederlande", "belgium", "belgien", "france", "frankreich",
    "spain", "spanien", "portugal", "italy", "italien", "poland", "polen",
    "ireland", "irland", "denmark", "dänemark", "sweden", "schweden",
    "norway", "norwegen", "finland", "finnland", "czechia", "tschechien",
    # the cities these boards actually carry
    "berlin", "munich", "münchen", "muenchen", "hamburg", "cologne", "köln",
    "koeln", "frankfurt", "stuttgart", "düsseldorf", "duesseldorf",
    "dusseldorf", "dortmund", "essen", "leipzig", "dresden", "hannover",
    "hanover", "nuremberg", "nürnberg", "nuernberg", "bonn", "mannheim",
    "karlsruhe", "bremen", "münster", "muenster", "bochum", "wuppertal",
    "bielefeld", "aachen", "augsburg", "wien", "vienna", "graz", "linz",
    "zurich", "zürich", "zuerich", "basel", "geneva", "genf", "bern",
    "amsterdam", "rotterdam", "eindhoven", "utrecht", "brussels", "brüssel",
    "antwerp", "paris", "lyon", "toulouse", "madrid", "barcelona", "valencia",
    "lisbon", "lissabon", "porto", "dublin", "copenhagen", "kopenhagen",
    "stockholm", "oslo", "helsinki", "warsaw", "warschau", "krakow", "prague",
    "prag", "budapest", "bucharest", "milan", "mailand", "rome", "rom",
    "turin", "london", "manchester", "edinburgh",
}

_SYSTEM = """\
You are choosing the search terms a job-board crawler will type into three job \
portals on behalf of one candidate. These terms decide what is ever fetched. \
Anything they miss is never seen by any later stage, so the goal is coverage \
of everything this candidate could plausibly be hired for — not precision. \
Relevance is judged later, by a separate stage that reads the full posting.

--- CANDIDATE PROFILE ---
{digest}

--- WHAT MAKES A GOOD QUERY ---
A job title as an employer would advertise it, and nothing else. Include the \
obvious central titles, the adjacent ones a level up and a level down, and the \
specialisations this profile supports that a different employer would name \
differently.

Hard rules, all of them enforced after you answer:
- {min_words} to {max_words} words, at most {max_chars} characters.
- No location, city, country, region, or work-arrangement word of any kind \
(no "remote", no "Berlin", no "Germany", no "Europe"). Location is a separate \
field the crawler fills in itself; a place name inside the query silently \
returns nothing.
- No boolean operators, quotes, brackets, wildcards, or punctuation. Plain \
words separated by spaces.
- No seniority-only terms on their own ("Senior", "Lead") — they must be part \
of a real title.
- No duplicates within a portal, and no two queries where one is a substring \
of the other.

--- PORTALS ---
{portals}

--- OUTPUT ---
Return only this JSON object, no prose and no code fence:
{{"portals":{{"<portal name>":["<query>","<query>"]}}}}
Use the portal names given above verbatim, and give exactly {per_portal} \
queries for each one.
"""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _words(query: str) -> list[str]:
    return [word for word in re.split(r"[\s/–—-]+", query) if word]


def rejection(query: Any) -> str:
    """Why this query may not be used, or "" if it may.

    Returns the reason rather than a bool so `--explain` and the log can say
    which rule a generated set fell over, which is the difference between
    "the model is bad at this" and "one word needs adding to `_GEO_WORDS`".
    """
    if not isinstance(query, str):
        return f"not a string ({type(query).__name__})"
    text = query.strip()
    if not text:
        return "empty"
    if len(text) > MAX_CHARS:
        return f"longer than {MAX_CHARS} characters"
    if _OPERATORS.search(text):
        return "contains operator or punctuation"
    words = _words(text)
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        return f"{len(words)} words, wanted {MIN_WORDS}-{MAX_WORDS}"
    lowered = [word.lower() for word in words]
    for word in lowered:
        if word in _BOOLEAN_WORDS:
            return f"boolean operator {word!r}"
        if word in _GEO_WORDS:
            return f"location term {word!r}"
    return ""


def _accept(raw: Any, portal: str) -> list[str]:
    """The usable queries out of one portal's generated list.

    Drops rather than raises, and de-duplicates case-insensitively — a model
    that returns "Data Scientist" twice has given one query, and the caller's
    `MIN_QUERIES` check must see it as one.
    """
    if not isinstance(raw, list):
        log.warning("%s: expected a list of queries, got %s",
                    portal, type(raw).__name__)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        reason = rejection(item)
        if reason:
            log.info("%s: rejected %r — %s", portal, item, reason)
            continue
        text = str(item).strip()
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


# --------------------------------------------------------------------------
# generation and cache
# --------------------------------------------------------------------------

def cache_key(digest: str) -> str:
    """Keyed on the digest text, not on the canonical profile's mtime.

    One level down from `profile.get_digest`, and deliberately looser: a
    profile edit that condenses to the same digest changes nothing this stage
    would produce, so it must not spend a call proving that.
    """
    return hashlib.sha1(digest.encode("utf-8")).hexdigest()[:16]


def _portal_block(entries: Sequence[dict[str, Any]]) -> str:
    lines = []
    for entry in entries:
        name = entry.get("name", "")
        language = PORTAL_LANGUAGE.get(name, DEFAULT_LANGUAGE)
        lines.append(f"- {name}: write these in {language}.")
    return "\n".join(lines)


def build_prompt(entries: Sequence[dict[str, Any]], digest: str,
                 per_portal: int) -> str:
    return _SYSTEM.format(digest=digest, portals=_portal_block(entries),
                          per_portal=per_portal, min_words=MIN_WORDS,
                          max_words=MAX_WORDS, max_chars=MAX_CHARS)


def _read_cache() -> dict[str, list[str]]:
    if not SEARCH_QUERIES_PATH.exists():
        return {}
    try:
        data = json.loads(SEARCH_QUERIES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        log.warning("could not read %s (%s) — falling back to sources.toml",
                    SEARCH_QUERIES_PATH.name, exc)
        return {}
    portals = data.get("portals") if isinstance(data, dict) else None
    if not isinstance(portals, dict):
        return {}
    return {str(name): list(queries) for name, queries in portals.items()
            if isinstance(queries, list)}


def _write_cache(portals: dict[str, list[str]], key: str) -> None:
    SEARCH_QUERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEARCH_QUERIES_PATH.write_text(
        json.dumps({"key": key, "portals": portals}, ensure_ascii=False,
                   indent=2), encoding="utf-8")
    with store.connect() as conn:
        store.set_meta(conn, "search_queries_key", key)


def generate(config: Config, entries: Sequence[dict[str, Any]],
             digest: str) -> dict[str, list[str]]:
    """One call for all portals. Returns {} on any failure — never raises.

    {} is not "no queries": every caller reads it as "this portal keeps its
    hand-written list", which is the same state as the feature being switched
    off. That is the only safe way for this to fail, since the alternative is
    a portal polled on nothing.
    """
    prompt = build_prompt(entries, digest, config.queries_per_portal)
    try:
        data = run_json(prompt, model=config.queries_model,
                        timeout=config.queries_timeout, retries=1)
    except ClaudeError as exc:
        log.error("could not generate search queries (%s) — every portal "
                  "keeps its sources.toml list", exc)
        return {}
    raw = data.get("portals") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        log.error("no portals object in the response (%s) — keeping the "
                  "sources.toml lists", str(data)[:200])
        return {}

    out: dict[str, list[str]] = {}
    for entry in entries:
        name = entry.get("name", "")
        accepted = _accept(raw.get(name), name)
        if len(accepted) < MIN_QUERIES:
            log.warning("%s: only %d valid quer%s generated — keeping its "
                        "sources.toml list", name, len(accepted),
                        "y" if len(accepted) == 1 else "ies")
            continue
        out[name] = accepted
    return out


#: Digest keys this process has already failed to generate for. Without it a
#: failing model would be retried once per portal per cycle, three times an
#: hour, for as long as it stayed broken.
_FAILED: set[str] = set()


def get_queries(config: Config, allow_generate: bool = True,
                force: bool = False) -> dict[str, list[str]]:
    """The current generated query sets, regenerating only when stale.

    `allow_generate=False` is the read-only path `status.py` uses: printing a
    status page must never spend a model call, so it shows what is cached and
    accepts that a profile edited since the last poll will show the previous
    set for one cycle.
    """
    if not config.queries_enabled:
        return {}
    cached = _read_cache()
    if not allow_generate:
        # No digest call on this path, deliberately. `profile.get_digest`
        # regenerates through the model when the canonical profile has changed,
        # so reading the key to check freshness here would cost exactly the
        # call this branch exists to avoid — and then show the cached set
        # anyway, because it has nothing else to show.
        return cached
    try:
        digest = profile.get_digest()
    except Exception as exc:  # noqa: BLE001 — no digest is not a poll failure
        log.warning("no profile digest (%s) — keeping the sources.toml lists",
                    exc)
        return {}
    key = cache_key(digest)

    store.init_db()
    with store.connect() as conn:
        cached_key = store.get_meta(conn, "search_queries_key")
    if not force and cached and cached_key == key:
        return cached

    if key in _FAILED:
        return cached

    entries = [entry for entry in load_sources().enabled_portals()
               if entry.get("name")]
    if not entries:
        return {}

    log.info("regenerating portal search queries for %d portal(s)", len(entries))
    generated = generate(config, entries, digest)
    if not generated:
        # Remember the failure against this digest, not for ever: a later
        # profile edit produces a different key and gets a fresh attempt.
        _FAILED.add(key)
        return cached
    _write_cache(generated, key)
    return generated


# --------------------------------------------------------------------------
# what the poll and the status page both call
# --------------------------------------------------------------------------

def for_entry(entry: dict[str, Any], config: Config,
              allow_generate: bool = True) -> dict[str, Any]:
    """`entry` with generated queries substituted in, or `entry` untouched.

    Returns a *new* dict whenever it substitutes anything. `_Reloader` caches
    the parsed `Sources` and hands the same dict objects to every caller, so
    writing into one here would leave generated queries in place after the
    feature was switched off — a config change that silently did not take.
    """
    if entry.get("kind") == "ats" or "provider" in entry:
        return entry
    name = entry.get("name", "")
    if not name:
        return entry
    generated = get_queries(config, allow_generate=allow_generate).get(name)
    if not generated:
        return entry
    return {**entry, "queries": list(generated)}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_current(config: Config) -> None:
    sources = load_sources()
    generated = get_queries(config, allow_generate=False)
    for entry in sources.portals:
        name = entry.get("name", "?")
        chosen = generated.get(name)
        origin = "generated" if chosen else "sources.toml"
        queries = chosen or list(entry.get("queries") or [])
        print(f"\n{name}  ({origin}, {len(queries)})")
        for query in queries:
            print(f"  {query}")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(
        description="Generate the portal search terms from the profile digest.")
    parser.add_argument("--regenerate", action="store_true",
                        help="call the model now and rewrite the cache, even "
                             "if the digest has not changed")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --regenerate, print what would be written "
                             "without touching the cache")
    parser.add_argument("--explain", action="store_true",
                        help="print the config, the cache key, and the prompt "
                             "that would be sent, without calling the model")
    parser.add_argument("--check", metavar="QUERY",
                        help="run one query string through the validator")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    setup(logging.DEBUG if args.verbose else logging.INFO)
    config = load_config()

    if args.check:
        reason = rejection(args.check)
        print(f"{args.check!r}: {reason or 'ok'}")
        return 1 if reason else 0

    if args.explain:
        digest = profile.get_digest()
        entries = [e for e in load_sources().enabled_portals() if e.get("name")]
        with store.connect() as conn:
            cached_key = store.get_meta(conn, "search_queries_key")
        key = cache_key(digest)
        print(f"queries: enabled={config.queries_enabled} "
              f"model={config.queries_model} "
              f"per_portal={config.queries_per_portal} "
              f"timeout={config.queries_timeout}s")
        print(f"cache key: {key}   on disk: {cached_key or '(none)'}"
              f"   {'fresh' if cached_key == key else 'STALE'}")
        print(f"cache file: {SEARCH_QUERIES_PATH}")
        print("\n--- prompt that would be sent ---\n")
        print(build_prompt(entries, digest, config.queries_per_portal))
        return 0

    if args.regenerate:
        digest = profile.get_digest()
        entries = [e for e in load_sources().enabled_portals() if e.get("name")]
        generated = generate(config, entries, digest)
        if not generated:
            print("nothing usable was generated — every portal keeps its "
                  "sources.toml list")
            return 1
        for name, queries in generated.items():
            print(f"\n{name}  ({len(queries)})")
            for query in queries:
                print(f"  {query}")
        if args.dry_run:
            print("\n(dry run — the cache was not written)")
        else:
            _write_cache(generated, cache_key(digest))
            print(f"\nwritten to {SEARCH_QUERIES_PATH}")
        return 0

    _print_current(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
