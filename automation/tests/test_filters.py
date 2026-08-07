"""The free filters: geography, seniority, and the prefilter that uses them.

Everything here runs before a single token is spent on scoring, which is what
makes it worth pinning down. A bug in a fetcher shows up as an error; a bug in
this layer shows up as nothing at all — the posting is simply never mentioned,
and there is no signal anywhere that it existed.

So the assertions come in pairs. For every rule that can reject, there is a test
that it rejects what it should, and a test that it lets through what it could
not read. `prefilter`'s docstring calls that the one-sided rule, and it is the
single property in this module that a refactor must not quietly lose.
"""

from __future__ import annotations

import datetime as dt

import pytest

from watcher import geo, prefilter, roles
from watcher.config import (
    ExperienceFilter,
    Filters,
    LocationFilter,
    SeniorityFilter,
    SourceDefaults,
    Sources,
)
from watcher.normalize import Posting


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def posting(**over) -> Posting:
    """A posting that passes every rule, so each test can break exactly one."""
    fields = {
        "source": "ats:Example",
        "provider": "greenhouse",
        "source_job_id": "1",
        "url": "https://example.test/jobs/1",
        "company": "Example GmbH",
        "title": "Machine Learning Engineer",
        "location": "Berlin, Germany",
        "country": "DE",
    }
    fields.update(over)
    return Posting(**fields)


def defaults(**over) -> SourceDefaults:
    fields = {"countries": (), "title_deny": ()}
    fields.update(over)
    return SourceDefaults(**fields)


def check(post: Posting, *, filters: Filters | None = None,
          max_age_days: int = 30, **default_over) -> prefilter.FilterResult:
    return prefilter.check(post, defaults(**default_over), max_age_days, filters)


# ===========================================================================
# geo
# ===========================================================================

@pytest.mark.parametrize("text,country,city", geo._FIXTURES)
def test_the_shipped_geo_fixtures_all_resolve(text, country, city) -> None:
    assert geo.country_of(text) == country
    assert geo.city_of(text) == city


@pytest.mark.parametrize("names,codes", geo._EXPAND_FIXTURES)
def test_the_shipped_expand_fixtures_all_resolve(names, codes) -> None:
    assert geo.expand(names) == codes


def test_a_continent_expands_to_its_members() -> None:
    europe = geo.expand(["EUROPE"])
    assert {"DE", "FR", "ES"} <= europe
    assert "US" not in europe


def test_worldwide_and_any_mean_every_country_we_know() -> None:
    assert geo.expand(["WORLDWIDE"]) == geo.ALL_COUNTRIES
    assert geo.expand(["ANY"]) == geo.ALL_COUNTRIES


def test_a_bare_two_letter_token_is_taken_as_a_code() -> None:
    assert geo.expand(["pt", "  ", ""]) == {"PT"}


def test_separators_in_a_region_name_do_not_matter() -> None:
    assert geo.expand(["north america"]) == geo.expand(["NORTH_AMERICA"])
    assert geo.expand(["north-america"]) == geo.expand(["NORTH_AMERICA"])


def test_an_unknown_token_costs_one_region_not_the_boot() -> None:
    # A typo in sources.toml must narrow the search, never raise on import.
    assert geo.expand(["DACH", "TYPOLAND"]) == {"DE", "AT", "CH"}
    assert geo.expand([]) == set()
    assert geo.expand(None) == set()


def test_a_country_name_beats_a_city_of_the_same_name() -> None:
    # Frankfort, Kentucky is the reason `country_of` reads names before cities.
    assert geo.country_of("Frankfurt, Kentucky, United States") == "US"
    assert geo.country_of("Frankfurt am Main") == "DE"


def test_a_bare_code_is_only_trusted_as_its_own_field() -> None:
    assert geo.country_of("Somewhere, DE") == "DE"
    # `IN` is an English preposition and `DE` opens `DevOps`; neither may
    # resolve mid-sentence.
    assert geo.country_of("Engineers IN our platform team") == ""


def test_a_word_boundary_keeps_bern_out_of_bernburg() -> None:
    assert geo.city_of("Bern") == "Bern"
    assert geo.city_of("Bernburg") == ""


def test_nothing_resolvable_returns_empty_rather_than_a_guess() -> None:
    for value in ("", "EMEA", "Remote (Europe)"):
        assert geo.country_of(value) == ""
        assert geo.city_of(value) == ""
        assert geo.city_key_of(value) == ""


def test_continent_of_and_describe_admit_what_they_do_not_know() -> None:
    assert geo.continent_of("DE") == "EUROPE"
    assert geo.continent_of("de") == "EUROPE"
    assert geo.continent_of("ZZ") == ""
    assert geo.continent_of("") == ""
    assert geo.describe("de") == "Germany"
    assert geo.describe("ZZ") == "ZZ"
    assert geo.describe("") == ""


@pytest.mark.parametrize("spelling", ["Munich", "München", "Muenchen", "MUNICH"])
def test_every_spelling_of_a_city_shares_one_key(spelling) -> None:
    """The bug this pins: `exclude_cities = ["Munich"]` ignored `München`.

    `city_key_of` used to return the alias the board happened to print, while
    `expand_cities` keyed whatever the user typed. The two only met when both
    chose the same spelling, so a city allow-list rejected postings in exactly
    the city it was written to keep.
    """
    assert geo.city_key_of(f"{spelling}, Germany") == geo.city_key_of("Munich")
    assert geo.city_key_of(spelling) in geo.expand_cities([spelling])
    assert geo.city_key_of(spelling) in geo.expand_cities(["München"])


def test_expand_cities_drops_blanks_and_unreadable_names() -> None:
    assert geo.expand_cities(["Berlin", "  ", ""]) == {"berlin"}
    assert geo.expand_cities([]) == set()
    assert geo.expand_cities(None) == set()
    # A name we do not know keeps its own key rather than matching anything.
    assert geo.expand_cities(["Nowhereville"]) == {"nowhereville"}


def test_a_non_latin_country_name_never_became_a_wildcard() -> None:
    # An empty alternative in the country regex matches at every position, so a
    # name that folds away has to be dropped from the table, not kept as "".
    assert "" not in geo.COUNTRY_NAME_KEYS
    assert geo.country_of("A posting with no location at all") == ""


def test_geo_self_test_passes_and_main_defaults_to_it(capsys) -> None:
    assert geo._self_test() == 0
    capsys.readouterr()
    assert geo.main([]) == 0
    assert "passed." in capsys.readouterr().out


def test_geo_main_can_resolve_and_expand(capsys) -> None:
    assert geo.main(["--resolve", "Düsseldorf, Germany"]) == 0
    out = capsys.readouterr().out
    assert "DE" in out and "Düsseldorf" in out and "EUROPE" in out

    assert geo.main(["--expand", "DACH"]) == 0
    assert "AT, CH, DE" in capsys.readouterr().out


def test_geo_main_says_unknown_rather_than_printing_a_blank(capsys) -> None:
    assert geo.main(["--resolve", "EMEA"]) == 0
    assert capsys.readouterr().out.count("(unknown)") == 3


# ===========================================================================
# roles — seniority
# ===========================================================================

@pytest.mark.parametrize("title,level", roles._LEVEL_FIXTURES)
def test_the_shipped_level_fixtures_all_resolve(title, level) -> None:
    assert roles.level_of(title) == level


def test_every_level_pattern_can_actually_be_reached() -> None:
    """A pattern no fixture reaches is a rule nobody has ever tested."""
    reached = {roles.level_of(title) for title, _ in roles._LEVEL_FIXTURES}
    assert set(roles.LEVELS) <= reached


def test_the_body_is_accepted_and_ignored() -> None:
    """The Solutions Architect that read as `intern` because of a sidebar tile.

    A job page carries the employer's other openings, its graduate-programme
    boilerplate, and the rank of everyone the hire reports to. None of that is
    evidence about this role, so the body is taken and thrown away.
    """
    body = "Intern - Legal & Regulatory Affairs\nAlso hiring: Head of Data"
    assert roles.level_of("Solutions Architect - AI & Data Integration", body) == "unknown"
    assert roles.level_of("Senior Data Scientist", body) == "senior"


def test_the_first_matching_pattern_wins() -> None:
    # Ordered most-specific first: a title with two rank words resolves to the
    # more senior reading rather than to whichever regex is cheapest.
    assert roles.level_of("Head of Machine Learning") == "executive"
    assert roles.level_of("Senior Principal Engineer") == "principal"
    assert roles.level_of("Working Student - Senior Team") == "intern"


def test_a_title_that_states_no_rank_is_unknown() -> None:
    assert roles.level_of("") == "unknown"
    assert roles.level_of(None) == "unknown"


# ===========================================================================
# roles — years of experience
# ===========================================================================

@pytest.mark.parametrize("text,years", roles._YEARS_FIXTURES)
def test_the_shipped_years_fixtures_all_resolve(text, years) -> None:
    assert roles.years_required(text) == years


def test_the_highest_bar_wins_across_fragments() -> None:
    assert roles.years_required(
        "2+ years of Python experience", "5+ years of experience in ML") == 5


def test_a_range_contributes_its_lower_bound() -> None:
    # 3 is the number that disqualifies you; 5 is the number that flatters them.
    assert roles.years_required("3-5 years of professional experience") == 3
    assert roles.years_required("3 to 5 years of hands-on experience") == 3
    assert roles.years_required("3 bis 5 Jahre Berufserfahrung") == 3


def test_a_spelled_out_number_counts() -> None:
    assert roles.years_required("two years of experience with PyTorch") == 2
    assert roles.years_required("Mindestens drei Jahre Berufserfahrung") == 3


def test_a_number_with_no_experience_word_near_it_is_not_a_bar() -> None:
    assert roles.years_required("We ship a release every 3 years") is None


def test_company_history_is_not_a_hiring_bar() -> None:
    for text in (
        "Founded 10 years ago, with deep experience in ML",
        "Over the last 8 years our experienced team has grown",
        "A 3 year roadmap built on years of experience",
    ):
        assert roles.years_required(text) is None


def test_an_incredible_number_is_refused_in_both_directions() -> None:
    assert roles.years_required("100 years of experience") is None
    assert roles.years_required("0 years of experience required") is None
    assert roles.years_required(f"{roles._MAX_CREDIBLE_YEARS} years of experience"
                                ) == roles._MAX_CREDIBLE_YEARS
    assert roles.years_required(
        f"{roles._MAX_CREDIBLE_YEARS + 1} years of experience") is None


def test_nothing_to_read_returns_none_not_zero() -> None:
    # None and 0 are different claims: one says "not stated", the other says
    # "stated as none", and `describe` and the matcher both act on that.
    assert roles.years_required("") is None
    assert roles.years_required(None) is None
    assert roles.years_required("No experience requirement stated") is None


def test_value_rejects_a_token_that_is_neither_digits_nor_a_word() -> None:
    assert roles._value("7") == 7
    assert roles._value(" Three ") == 3
    assert roles._value("many") is None


# ===========================================================================
# roles — the filter decision
# ===========================================================================

def test_unknown_always_survives_an_allow_and_a_deny_list() -> None:
    """The rule that keeps the seniority filter from being invisible data loss.

    Most good ML titles state no rank at all, so a deny list that could catch
    `unknown` would silently discard the bulk of the feed.
    """
    assert roles.level_allowed("unknown", allow=("senior",), deny=("unknown",))
    assert roles.level_allowed("unknown", allow=(), deny=())


def test_deny_beats_allow() -> None:
    assert not roles.level_allowed("intern", allow=("intern",), deny=("intern",))


def test_an_empty_allow_list_means_every_band() -> None:
    for level in roles.LEVELS:
        assert roles.level_allowed(level, allow=(), deny=())


def test_an_allow_list_excludes_what_it_does_not_name() -> None:
    assert roles.level_allowed("senior", allow=("senior", "lead"), deny=())
    assert not roles.level_allowed("intern", allow=("senior", "lead"), deny=())


def test_the_lists_are_read_case_and_space_insensitively() -> None:
    assert not roles.level_allowed("intern", allow=(), deny=("  INTERN ",))
    assert roles.level_allowed("senior", allow=("Senior",), deny=())


def test_none_lists_are_treated_as_empty() -> None:
    assert roles.level_allowed("senior", allow=None, deny=None)


@pytest.mark.parametrize("level,years,expected", [
    ("senior", 5, "senior · asks 5+ yrs"),
    ("senior", None, "senior"),
    ("unknown", 5, "asks 5+ yrs"),
    ("unknown", None, ""),
    ("", None, ""),
    ("senior", 0, "senior"),
])
def test_describe_omits_what_was_not_read(level, years, expected) -> None:
    assert roles.describe(level, years) == expected


def test_roles_self_test_passes_and_main_defaults_to_it(capsys) -> None:
    assert roles._self_test() == 0
    capsys.readouterr()
    assert roles.main([]) == 0
    assert "passed." in capsys.readouterr().out


def test_roles_main_can_classify_one_title_or_body(capsys) -> None:
    assert roles.main(["--title", "Staff AI Engineer"]) == 0
    assert "level: staff" in capsys.readouterr().out

    assert roles.main(["--text", "5+ years of experience"]) == 0
    assert "years: 5" in capsys.readouterr().out

    assert roles.main(["--title", "AI Engineer", "--self-test"]) == 0
    assert "passed." in capsys.readouterr().out


# ===========================================================================
# prefilter — title, age, hard blockers
# ===========================================================================

def test_a_posting_with_no_title_is_refused() -> None:
    result = check(posting(title=""))
    assert not result.accepted
    assert result.reason == "missing title"


def test_there_is_no_allow_list_every_title_gets_through_by_default() -> None:
    assert check(posting(title="Marketing Manager")).accepted


def test_the_deny_list_hard_blocks_a_title() -> None:
    result = check(posting(title="Machine Learning Sales Engineer"),
                   title_deny=("sales",))
    assert not result.accepted
    assert result.reason == "title hard-blocked"
    assert result.stage == "title"


def test_title_deny_ignores_case() -> None:
    result = check(posting(title="MACHINE LEARNING SALES ENGINEER"),
                   title_deny=("sales",))
    assert not result.accepted


def test_a_posting_older_than_the_cap_is_refused() -> None:
    old = dt.date.today() - dt.timedelta(days=45)
    result = check(posting(posted_at=old), max_age_days=30)
    assert not result.accepted
    assert result.reason == "posted 45 days ago"


def test_a_posting_with_no_date_is_not_aged_out() -> None:
    # One-sided: an unreadable date means the age rule abstains.
    assert check(posting(posted_at=None), max_age_days=1).accepted


def test_a_posting_exactly_at_the_cap_survives() -> None:
    edge = dt.date.today() - dt.timedelta(days=30)
    assert check(posting(posted_at=edge), max_age_days=30).accepted


@pytest.mark.parametrize("blocker", prefilter.HARD_BLOCKERS)
def test_every_hard_blocker_is_reachable_from_the_body(blocker) -> None:
    result = check(posting(description=f"Requirements: {blocker.upper()} needed"))
    assert not result.accepted
    assert result.reason == "explicit hard blocker"


def test_a_hard_blocker_in_the_title_counts_too() -> None:
    assert not check(posting(title="Unpaid Internship - Data")).accepted


def test_without_a_filters_object_only_the_free_rules_apply() -> None:
    # The signature keeps `filters` optional so older callers still work; a
    # posting in a country nobody asked for must still pass in that mode.
    assert check(posting(location="Austin, Texas", country="US")).accepted


# ===========================================================================
# prefilter — geography
# ===========================================================================

def loc(**over) -> Filters:
    return Filters(location=LocationFilter(**over))


def test_an_excluded_city_is_refused_before_its_country_is_considered() -> None:
    result = check(posting(location="Munich, Germany", country="DE"),
                   filters=loc(countries=("DE",), exclude_cities=("Munich",)))
    assert not result.accepted
    assert result.reason == "city Munich excluded"


def test_the_city_exclusion_survives_the_boards_own_spelling() -> None:
    result = check(posting(location="München", country="DE"),
                   filters=loc(exclude_cities=("Munich",)))
    assert not result.accepted
    assert "Munich" in result.reason


def test_an_excluded_country_is_refused() -> None:
    result = check(posting(location="Zurich, Switzerland", country="CH"),
                   filters=loc(exclude_countries=("CH",)))
    assert not result.accepted
    assert result.reason == "country Switzerland excluded"


def test_a_country_outside_the_allow_list_is_refused() -> None:
    result = check(posting(location="Austin, Texas", country="US"),
                   filters=loc(regions=("DACH",)))
    assert not result.accepted
    assert result.reason == "country United States outside target region"


def test_remote_only_escapes_the_region_when_both_switches_are_on() -> None:
    """`remote_anywhere = false` means the employer's country still decides.

    The contract, the timezone, and the work-authorisation question all follow
    the employer rather than the desk, so a remote posting from outside the
    region is only in scope when the config says so twice.
    """
    out_of_region = posting(location="Austin, Texas", country="US", remote=True)

    assert check(out_of_region, filters=loc(
        regions=("DACH",), remote_ok=True, remote_anywhere=True)).accepted
    assert not check(out_of_region, filters=loc(
        regions=("DACH",), remote_ok=True, remote_anywhere=False)).accepted
    assert not check(out_of_region, filters=loc(
        regions=("DACH",), remote_ok=False, remote_anywhere=True)).accepted


def test_an_unreadable_location_passes_the_country_rule() -> None:
    # The one-sided rule: a location that resolves to nothing is scored, not
    # dropped. Every new board format arrives looking exactly like this.
    assert check(posting(location="EMEA", country=""),
                 filters=loc(regions=("DACH",))).accepted


def test_the_postings_own_country_field_outranks_the_free_text() -> None:
    result = check(posting(location="somewhere vague", country="us"),
                   filters=loc(regions=("DACH",)))
    assert not result.accepted
    assert "United States" in result.reason


def test_a_city_allow_list_narrows_within_an_allowed_country() -> None:
    filters = loc(countries=("DE",), cities=("Berlin", "Munich"))
    assert check(posting(location="Berlin, Germany", country="DE"),
                 filters=filters).accepted
    result = check(posting(location="Hamburg, Germany", country="DE"),
                   filters=filters)
    assert not result.accepted
    assert result.reason == "city Hamburg not in the city list"


def test_a_city_allow_list_is_written_in_whichever_spelling_you_think_in() -> None:
    filters = loc(countries=("DE",), cities=("Munich",))
    for spelling in ("München", "Muenchen", "Munich"):
        assert check(posting(location=f"{spelling}, Germany", country="DE"),
                     filters=filters).accepted, spelling


def test_remote_ok_alone_escapes_a_city_allow_list() -> None:
    # Unlike the region rule, no second switch: the city list narrows where the
    # desk is, and a remote posting has not got one.
    filters = loc(countries=("DE",), cities=("Berlin",), remote_ok=True)
    assert check(posting(location="Hamburg, Germany", country="DE", remote=True),
                 filters=filters).accepted
    assert not check(posting(location="Hamburg, Germany", country="DE",
                             remote=True),
                     filters=loc(countries=("DE",), cities=("Berlin",),
                                 remote_ok=False)).accepted


def test_a_city_allow_list_does_not_reject_a_city_it_could_not_read() -> None:
    assert check(posting(location="Anywhere in the DACH region", country="DE"),
                 filters=loc(countries=("DE",), cities=("Berlin",))).accepted


def test_no_geographic_configuration_at_all_accepts_everything() -> None:
    assert check(posting(location="Bengaluru, India", country="IN"),
                 filters=loc()).accepted


# ===========================================================================
# prefilter — seniority and experience
# ===========================================================================

def test_a_denied_seniority_band_is_refused() -> None:
    result = check(posting(title="Working Student Machine Learning"),
                   filters=Filters(seniority=SeniorityFilter(deny=("intern",))))
    assert not result.accepted
    assert result.reason == "seniority intern filtered out"


def test_a_title_with_no_stated_rank_survives_a_seniority_filter() -> None:
    assert check(posting(title="Machine Learning Engineer"),
                 filters=Filters(
                     seniority=SeniorityFilter(allow=("senior",)))).accepted


def test_experience_is_annotate_only_by_default() -> None:
    """Extracted, shown in the ping, weighed by the matcher — never acted on.

    Postings overstate the bar routinely, so a hard cut here would cost real
    matches for a number the employer half-invented.
    """
    wants_ten = posting(description="12+ years of experience required")
    assert check(wants_ten, filters=Filters()).accepted
    assert check(wants_ten, filters=Filters(
        experience=ExperienceFilter(mode="annotate", max_years=5))).accepted
    # mode = "filter" with no cap set is still not a filter.
    assert check(wants_ten, filters=Filters(
        experience=ExperienceFilter(mode="filter", max_years=0))).accepted


def test_experience_filtering_refuses_a_bar_above_the_cap() -> None:
    filters = Filters(experience=ExperienceFilter(mode="filter", max_years=5))
    result = check(posting(description="12+ years of experience required"),
                   filters=filters)
    assert not result.accepted
    assert result.reason == "asks 12+ yrs (cap 5)"


def test_experience_filtering_keeps_the_cap_and_anything_unreadable() -> None:
    filters = Filters(experience=ExperienceFilter(mode="filter", max_years=5))
    assert check(posting(description="5+ years of experience"),
                 filters=filters).accepted
    assert check(posting(description="A great team and a big mission"),
                 filters=filters).accepted


def test_a_posting_that_clears_every_rule_is_accepted() -> None:
    result = check(
        posting(title="Senior Machine Learning Engineer",
                location="Berlin, Germany", country="DE",
                description="3+ years of experience with PyTorch",
                posted_at=dt.date.today()),
        filters=Filters(
            location=LocationFilter(regions=("DACH",), cities=("Berlin",)),
            seniority=SeniorityFilter(allow=("senior", "mid")),
            experience=ExperienceFilter(mode="filter", max_years=8),
        ),
        title_deny=("sales",),
    )
    assert result == prefilter.FilterResult(True, "")


# ===========================================================================
# prefilter — the --explain CLI
# ===========================================================================

def test_explain_prints_what_the_config_expands_to(monkeypatch, capsys) -> None:
    """`regions = ["DACH"]` is only useful once you can see the codes it became."""
    sources = Sources(
        defaults=defaults(title_deny=("sales",)),
        ats=(), portals=(),
        filters=Filters(
            location=LocationFilter(regions=("DACH",), exclude_countries=("CH",),
                                    cities=("Munich",), exclude_cities=("Bonn",)),
            seniority=SeniorityFilter(allow=("senior",), deny=("intern",)),
            experience=ExperienceFilter(mode="filter", max_years=8),
        ),
    )
    monkeypatch.setattr("watcher.config.load_sources", lambda: sources)

    assert prefilter.main([]) == 0
    out = capsys.readouterr().out

    assert "AT, CH, DE" in out          # the region, resolved
    assert "-> allowed (3)" in out
    assert "-> excluded       CH" in out
    assert "-> cities         Munich" in out
    assert "-> excluded cities Bonn" in out
    assert "sales" in out
    assert "no allow-list" in out
    assert "max_years         8" in out


def test_explain_names_the_empty_cases_rather_than_printing_blanks(
        monkeypatch, capsys) -> None:
    sources = Sources(defaults=defaults(), ats=(), portals=(), filters=Filters())
    monkeypatch.setattr("watcher.config.load_sources", lambda: sources)

    assert prefilter.main(["--explain"]) == 0
    out = capsys.readouterr().out

    assert "anywhere" in out
    assert "any city in allowed countries" in out
    assert "every band" in out
    assert "not enforced" in out


def test_explain_warns_that_unknown_cannot_be_denied(monkeypatch, capsys) -> None:
    # Denying `unknown` in sources.toml does nothing, and silently doing nothing
    # is how someone spends a week wondering where the postings went.
    sources = Sources(
        defaults=defaults(), ats=(), portals=(),
        filters=Filters(seniority=SeniorityFilter(deny=("UNKNOWN",))),
    )
    monkeypatch.setattr("watcher.config.load_sources", lambda: sources)

    assert prefilter.main([]) == 0
    assert "'unknown' cannot be denied" in capsys.readouterr().out
