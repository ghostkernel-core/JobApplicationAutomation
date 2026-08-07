"""Cheap, deterministic filters that run before any LLM call.

Every rule here is **one-sided**: it can reject only what it positively
resolved. A location that parses to no country, a title with no stated rank, a
posting with no experience bar — all pass through to the matcher, which scores
them and surfaces them. That is the governing design decision of this module,
and it is not a convenience: a filter that drops what it *failed to read* turns
every new board format, every unfamiliar city spelling, and every rewritten
title convention into silent, unauditable data loss. Uncertainty is meant to
cost one scored posting, never a missed job.

`FilterResult.reason` is written for a human reading `--show-filtered`, so it
names the value that caused the rejection rather than the rule that fired.
`stage` is the machine-readable counterpart: reasons interpolate values and so
cannot be grouped, which is how "these are all title misses" survived as a
belief for months while half of them were postings in India.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import geo, roles
from .config import Filters, SourceDefaults
from .normalize import Posting

HARD_BLOCKERS = (
    "security clearance", "sicherheitsüberprüfung",
    "native german", "muttersprachliches deutsch",
    "unpaid internship",
)

#: Every value `FilterResult.stage` may take, in the order the rules run.
#: `triage` is not produced here — it is the LLM gate `poll_once` applies after
#: these, and it shares this vocabulary because both write to the same `drops`
#: table and the weekly audit samples across all of them.
STAGES = ("title", "age", "location", "seniority", "blocker", "experience",
          "triage")


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str = ""
    #: Which rule rejected it; empty on acceptance. One of `STAGES`.
    stage: str = ""


def _location_ok(posting: Posting, f: Filters) -> FilterResult:
    """Geography, in order of how specific the evidence is.

    City is checked before country because it is the stronger signal: a posting
    that names a city we excluded is excluded even if its country is allowed.
    """
    loc = f.location
    city_key = geo.city_key_of(posting.location)
    country = (posting.country or geo.country_of(posting.location) or "").upper()

    if city_key and city_key in loc.excluded_cities:
        return FilterResult(
            False, f"city {geo.CITY_DISPLAY.get(city_key, city_key)} excluded",
            "location")

    if country and country in loc.excluded:
        return FilterResult(False, f"country {geo.describe(country)} excluded",
                            "location")

    allowed = loc.allowed
    if allowed and country and country not in allowed:
        # Remote is the escape hatch, but only as far as the config allows.
        # `remote_anywhere = false` means a remote posting from an out-of-region
        # employer is still out of region: the contract, the timezone, and the
        # work-authorisation question all follow the employer, not the desk.
        if posting.remote and loc.remote_ok and loc.remote_anywhere:
            return FilterResult(True)
        return FilterResult(
            False, f"country {geo.describe(country)} outside target region",
            "location")

    # A city allow-list is an explicit narrowing, so it is enforced whenever a
    # city was resolved — but a posting whose location names no city we know
    # still passes, per the one-sided rule.
    allowed_cities = loc.allowed_cities
    if allowed_cities and city_key and city_key not in allowed_cities:
        if posting.remote and loc.remote_ok:
            return FilterResult(True)
        display = geo.CITY_DISPLAY.get(city_key, city_key)
        return FilterResult(False, f"city {display} not in the city list",
                            "location")

    return FilterResult(True)


def check(
    posting: Posting,
    defaults: SourceDefaults,
    max_age_days: int,
    filters: Filters | None = None,
) -> FilterResult:
    """Whether a posting is worth spending a matcher call on.

    Rules run cheapest and most certain first, and the two that need a
    description run last. That order is not a micro-optimisation — it decides
    what a drop is *reported as*, and the report is the only record there is.
    Title used to run first, so roughly fifty postings a cycle from India,
    China, Mexico and Brazil were filed as "title outside target roles"; the
    geography rule that would have answered them exactly, and for free, sat
    three rules further down. Every one of those looked like evidence the title
    list needed widening.

    `filters` is optional so existing callers keep working; omitted, only the
    title, age and hard-blocker rules apply. That guard is per-rule rather than
    one early return, because the rules that need `filters` no longer sit in a
    contiguous block.
    """
    if not posting.title:
        return FilterResult(False, "missing title", "title")

    title = posting.title.casefold()
    if any(t in title for t in defaults.title_deny):
        return FilterResult(False, "title hard-blocked", "title")

    if posting.age_days is not None and posting.age_days > max_age_days:
        return FilterResult(False, f"posted {posting.age_days} days ago", "age")

    if filters is not None:
        located = _location_ok(posting, filters)
        if not located.accepted:
            return located

        # Safe this early only because `roles.level_of` reads the title and
        # nothing else (roles.py) — a real posting once resolved to `intern`
        # off unrelated body text, which is why it is title-only by design.
        # A later "improvement" that consults the description would silently
        # move this rule's meaning, so a test pins it.
        level = posting.level
        if not roles.level_allowed(level, filters.seniority.allow,
                                   filters.seniority.deny):
            return FilterResult(False, f"seniority {level} filtered out",
                                "seniority")

    text = f"{posting.title}\n{posting.description}".casefold()
    if any(term in text for term in HARD_BLOCKERS):
        return FilterResult(False, "explicit hard blocker", "blocker")

    # Years of experience is annotate-only by default: extracted, shown in the
    # ping, weighed by the matcher, never acted on here. Postings overstate the
    # bar routinely and profile_kb.md treats being one or two years short as a
    # minor gap, so a hard cut here would cost real matches for a number the
    # employer half-invented.
    if filters is not None and filters.experience.filtering:
        years = posting.years_required
        if years is not None and years > filters.experience.max_years:
            return FilterResult(
                False,
                f"asks {years}+ yrs (cap {filters.experience.max_years})",
                "experience")

    return FilterResult(True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _explain() -> int:
    """Print the active filter configuration, resolved.

    Prints what the config *expands to*, not what it says — `regions = ["EU"]`
    is only useful if you can see the 27 codes it became.
    """
    from .config import load_sources

    sources = load_sources()
    f = sources.filters
    loc = f.location

    allowed = sorted(loc.allowed)
    print("location")
    print(f"  continents        {list(loc.continents) or '-'}")
    print(f"  regions           {list(loc.regions) or '-'}")
    print(f"  countries         {list(loc.countries) or '-'}")
    print(f"  -> allowed ({len(allowed)})  {', '.join(allowed) or 'anywhere'}")
    excluded = sorted(loc.excluded)
    print(f"  -> excluded       {', '.join(excluded) or '-'}")
    cities = sorted(geo.CITY_DISPLAY.get(c, c) for c in loc.allowed_cities)
    print(f"  -> cities         {', '.join(cities) or 'any city in allowed countries'}")
    ex_cities = sorted(geo.CITY_DISPLAY.get(c, c) for c in loc.excluded_cities)
    print(f"  -> excluded cities {', '.join(ex_cities) or '-'}")
    print(f"  remote_ok         {loc.remote_ok}")
    print(f"  remote_anywhere   {loc.remote_anywhere}")

    print("\nseniority")
    print(f"  allow             {list(f.seniority.allow) or 'every band'}")
    print(f"  deny              {list(f.seniority.deny) or '-'}")
    denied_unknown = "unknown" in {d.casefold() for d in f.seniority.deny}
    if denied_unknown:
        print("  note              'unknown' cannot be denied; it always passes")

    print("\nexperience")
    print(f"  mode              {f.experience.mode}")
    if f.experience.filtering:
        print(f"  max_years         {f.experience.max_years}")
    else:
        print("  max_years         not enforced (mode is not 'filter')")

    print("\ntitle")
    print(f"  deny  ({len(sources.defaults.title_deny)})        "
          f"{', '.join(sources.defaults.title_deny) or '-'}")
    print("  note              no allow-list — a title is judged by triage "
          "against the profile, not by substring")

    print(f"\nrule order        {' -> '.join(s for s in STAGES if s != 'triage')}"
          " -> triage")
    print("  note              the last two read the description; everything "
          "before them is free")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--explain", action="store_true",
                        help="show the active filters, fully expanded — the "
                             "default, and the only thing this CLI does")
    parser.parse_args(argv)
    return _explain()


if __name__ == "__main__":
    raise SystemExit(main())
