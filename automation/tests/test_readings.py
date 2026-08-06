"""What the watcher reads out of a posting, and how it recognises one it has.

Three modules with one job between them: turn whatever a board sent into the
handful of facts a person needs to answer `yes` or `no` in a Telegram thread —
the language bar, the contract, where the desk is, and whether this is a job
already applied for.

None of these readings filter anything, so the failure mode is not a missing
posting but a confidently wrong line in the ping. `terms` and `normalize` answer
`""`/`None` rather than guessing for that reason, and the tests below spend most
of their effort on the abstaining half rather than the happy path.

`dedupe` is the exception: it does block a build, and it reaches outside the
watcher's own database to do it, into a folder tree and a workbook that predate
this system entirely.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from watcher import dedupe, normalize, store, terms
from watcher.normalize import Posting


# ===========================================================================
# terms — languages
# ===========================================================================

@pytest.mark.parametrize("text,expected", terms._LANGUAGE_FIXTURES)
def test_the_shipped_language_fixtures_all_resolve(text, expected) -> None:
    assert terms.languages(text) == expected


def test_a_cefr_level_outranks_a_prose_qualifier() -> None:
    # "C1" is a standard; "sehr gut" is an opinion.
    assert terms.languages("Sehr gute Deutschkenntnisse (mind. C1)") == ["German C1"]


def test_the_strongest_prose_qualifier_wins() -> None:
    # "sehr gute" matches both the fluent and the good pattern.
    assert terms.languages("Sehr gute Deutschkenntnisse") == ["German fluent"]


def test_each_level_is_assigned_to_its_nearest_language() -> None:
    """Postings state both languages in one breath.

    Overlapping windows used to hand C1 to both, reporting a German bar on a
    role that only wanted conversational English.
    """
    assert terms.languages(
        "Verhandlungssicheres Deutsch und gutes Englisch"
    ) == ["German fluent", "English good"]


def test_a_bullet_break_stops_a_qualifier_reaching_the_next_claim() -> None:
    assert terms.languages(
        "* Projektkoordination wünschenswert\n* Sehr gute Deutschkenntnisse"
    ) == ["German fluent"]


def test_a_heading_with_a_colon_governs_the_bullets_under_it() -> None:
    assert terms.languages(
        "Nice to have:\n - German language proficiency (native level)"
    ) == ["German native (a plus)"]


def test_a_heading_only_reaches_forwards() -> None:
    # The same words *after* the bullet are that bullet's own business.
    assert terms.languages(
        "- Fluent German required\nNice to have:\n - Polish"
    ) == ["German fluent", "Polish (a plus)"]


def test_a_language_word_outside_a_language_context_is_not_a_requirement() -> None:
    """`German` appears in the boilerplate of nearly every posting here.

    Without the context check, "German market", "German engineering", and a
    Berlin address all read as a language bar.
    """
    for text in (
        "We are a German company building German engineering software",
        "Our office is in the German capital",
        "Join our French subsidiary in Lyon",
    ):
        assert terms.languages(text) == []


def test_a_salary_adjective_does_not_become_a_language_bar() -> None:
    # A bare `gut\w*` read "gutes Gehalt" two lines up as a German level.
    assert terms.languages(
        "Wir bieten ein gutes Gehalt. Deutschkenntnisse erforderlich."
    ) == ["German"]


@pytest.mark.parametrize("label,text", [
    ("French", "Fluent French language skills"),
    ("Spanish", "Spanish language proficiency required"),
    ("Dutch", "Written and spoken Dutch"),
    ("Italian", "Italian language level B2"),
    ("Polish", "Polish language skills are a plus"),
])
def test_every_listed_language_can_be_recognised(label, text) -> None:
    assert any(found.startswith(label) for found in terms.languages(text))


def test_the_languages_come_back_in_a_fixed_order() -> None:
    # German and English first, because they are the two that decide anything.
    assert terms.languages(
        "Fluent English, some Polish, and native German language skills"
    ) == ["German native", "English fluent", "Polish"]


def test_nothing_to_read_is_an_empty_list() -> None:
    assert terms.languages("") == []
    assert terms.languages(None) == []
    assert terms.languages("No language requirement is stated") == []


def test_the_language_reading_spans_several_fragments() -> None:
    assert terms.languages("Data Scientist (m/w/d)",
                           "Sie sprechen fließend Deutsch") == ["German fluent"]


# ===========================================================================
# terms — contract and arrangement
# ===========================================================================

@pytest.mark.parametrize("text,expected", terms._CONTRACT_FIXTURES)
def test_the_shipped_contract_fixtures_all_resolve(text, expected) -> None:
    assert terms.contract(text) == expected


def test_the_more_specific_contract_wins() -> None:
    # A posting saying both is a fixed-term employment contract, not a
    # permanent one.
    assert terms.contract(
        "Festanstellung, zunächst befristet auf 2 Jahre") == "fixed-term"
    assert terms.contract(
        "Werkstudent im Rahmen einer Festanstellung") == "working student"


def test_part_time_is_reported_over_full_time() -> None:
    # A posting offering both hours is reported as the part-time one: full-time
    # is the assumption anyway, so it is the other half that carries news.
    assert terms.contract("Permanent position, full-time or part-time"
                          ) == "permanent · part-time"
    assert terms.contract("Vollzeit oder Teilzeit") == "part-time"


def test_hours_alone_are_still_worth_reporting() -> None:
    assert terms.contract("Teilzeit möglich") == "part-time"
    assert terms.contract("This is a full-time role") == "full-time"


def test_a_posting_that_says_nothing_about_the_contract_says_nothing() -> None:
    assert terms.contract("") == ""
    assert terms.contract(None) == ""
    assert terms.contract("A great role on a great team") == ""


@pytest.mark.parametrize("text,flag,expected", terms._ARRANGEMENT_FIXTURES)
def test_the_shipped_arrangement_fixtures_all_resolve(text, flag, expected) -> None:
    assert terms.arrangement(text, remote_flag=flag) == expected


def test_hybrid_is_tested_before_remote() -> None:
    # A posting describing "hybrid - 2 days remote" mentions remote but is not.
    assert terms.arrangement("Hybrid setup with home office on Fridays") == "hybrid"


def test_the_body_overrules_the_boards_own_remote_flag() -> None:
    """Boards mark a hybrid role as remote routinely."""
    assert terms.arrangement("Hybrides Arbeiten in München",
                             remote_flag=True) == "hybrid"
    assert terms.arrangement("Arbeit vor Ort", remote_flag=True) == "onsite"
    # ...but the flag is still the answer when the body is silent.
    assert terms.arrangement("Nothing said here", remote_flag=True) == "remote"
    assert terms.arrangement("", remote_flag=True) == "remote"
    assert terms.arrangement("", remote_flag=False) == ""


@pytest.mark.parametrize("langs,contract_label,arrangement_label,expected", [
    (["German C1"], "permanent · full-time", "hybrid",
     "hybrid · permanent · full-time · German C1"),
    (["German C1", "English fluent"], "", "", "German C1, English fluent"),
    ([], "freelance", "", "freelance"),
    (None, "", "remote", "remote"),
    (None, "", "", ""),
])
def test_describe_joins_only_what_was_read(langs, contract_label,
                                           arrangement_label, expected) -> None:
    assert terms.describe(langs, contract_label, arrangement_label) == expected


def test_terms_self_test_passes_and_main_defaults_to_it(capsys) -> None:
    assert terms._self_test() == 0
    capsys.readouterr()
    assert terms.main([]) == 0
    assert "passed." in capsys.readouterr().out


def test_terms_main_can_read_one_body(capsys) -> None:
    assert terms.main(["--text", "Unbefristete Festanstellung, Deutsch C1"]) == 0
    out = capsys.readouterr().out
    assert "German C1" in out
    assert "permanent" in out


# ===========================================================================
# terms — backfill
# ===========================================================================

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A real database at a throwaway path, wired in the way `backfill` finds it."""
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    return path


def add_posting(path, posting_id: str, **over) -> None:
    row = {"title": "Data Scientist", "description": "", "remote": 0,
           "languages": "", "contract": "", "arrangement": ""}
    row.update(over)
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO postings
               (id, loose_key, source, provider, url, canonical_url, company,
                title, description, remote, languages, contract, arrangement,
                first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (posting_id, f"key-{posting_id}", "portal:test", "test",
             f"https://example.test/{posting_id}",
             f"https://example.test/{posting_id}", "ExampleCo",
             row["title"], row["description"], row["remote"],
             row["languages"], row["contract"], row["arrangement"],
             "2026-08-04T09:00:00+00:00"),
        )


def read(path, posting_id: str) -> sqlite3.Row:
    with store.connect(path) as conn:
        return conn.execute("SELECT * FROM postings WHERE id = ?",
                            (posting_id,)).fetchone()


def test_backfill_fills_the_three_columns_from_stored_text(db, capsys) -> None:
    add_posting(db, "a", description="Unbefristete Festanstellung in Vollzeit. "
                                     "Verhandlungssicheres Deutsch (C1). "
                                     "Homeoffice möglich.")
    assert terms.backfill() == 0

    row = read(db, "a")
    assert row["languages"] == "German C1"
    assert row["contract"] == "permanent · full-time"
    assert row["arrangement"] == "remote"
    assert "1 of 1" in capsys.readouterr().out


def test_backfill_is_a_dry_run_when_asked(db, capsys) -> None:
    add_posting(db, "a", description="Deutsch C1 erforderlich")
    assert terms.backfill(dry_run=True) == 0

    assert read(db, "a")["languages"] == ""
    assert "nothing written" in capsys.readouterr().out


def test_backfill_skips_rows_it_cannot_improve(db, capsys) -> None:
    add_posting(db, "empty", description="")           # nothing to read from
    add_posting(db, "silent", description="A great team on a great mission")
    add_posting(db, "done", description="Deutsch C1", languages="German C1")

    assert terms.backfill() == 0
    out = capsys.readouterr().out

    # `done` is not even selected — the query only takes rows with all three
    # columns blank — so the denominator is 2, and neither of those two
    # yielded a reading.
    assert "0 of 2" in out
    assert read(db, "done")["contract"] == ""


def test_backfill_reads_the_boards_remote_flag_when_the_body_is_silent(db) -> None:
    add_posting(db, "a", description="Deutsch C1 erforderlich", remote=1)
    assert terms.backfill() == 0
    assert read(db, "a")["arrangement"] == "remote"


def test_terms_main_routes_backfill_and_its_dry_run(db, capsys) -> None:
    add_posting(db, "a", description="Deutsch C1 erforderlich")

    assert terms.main(["--backfill", "--dry-run"]) == 0
    assert "nothing written" in capsys.readouterr().out
    assert read(db, "a")["languages"] == ""

    assert terms.main(["--backfill"]) == 0
    assert read(db, "a")["languages"] == "German C1"


# ===========================================================================
# normalize — identity keys
# ===========================================================================

def test_umlauts_fold_the_way_germans_transliterate_them() -> None:
    # `ü` -> `ue`, not the bare `u` NFKD would leave behind.
    assert normalize.strip_accents("Müller") == "Mueller"
    assert normalize.strip_accents("Weiß") == "Weiss"
    assert normalize.strip_accents("Café") == "Cafe"


def test_the_legal_form_goes_in_every_spelling_the_employer_uses() -> None:
    """A finished application was reported as a failed build over this.

    The posting said `PFALZWERKE AKTIENGESELLSCHAFT` while the folder said
    `PFALZWERKE`; the two keyed differently, so the duplicate check never fired.
    """
    assert (normalize.company_key("PFALZWERKE AKTIENGESELLSCHAFT")
            == normalize.company_key("Pfalzwerke"))
    assert (normalize.company_key("Example GmbH & Co. KG")
            == normalize.company_key("example"))
    assert (normalize.company_key("Acme, Inc.") == normalize.company_key("ACME"))


def test_a_name_made_entirely_of_legal_forms_still_keys_to_something() -> None:
    assert normalize.company_key("GmbH") != ""
    assert normalize.company_key("") == ""


def test_clean_title_removes_gender_tags_and_boilerplate() -> None:
    assert normalize.clean_title("Data Scientist (m/w/d)") == "Data Scientist"
    assert normalize.clean_title("Data Scientist – Remote") == "Data Scientist"
    assert normalize.clean_title("Senior ML Engineer (Full-Time),") == "Senior ML Engineer"
    assert normalize.clean_title("") == ""


def test_title_similarity_forgives_rank_but_not_subject() -> None:
    """The two failures that shaped this: 0.83 for unrelated roles, 0.67 for one.

    `AI Engineer` vs `Data Engineer` scores high on plain string similarity
    purely because both end in `Engineer`, which would suppress a legitimate
    application. `Data Scientist` vs `Senior Data Scientist` scores low on plain
    token overlap, which would let us apply to the same job twice.
    """
    assert normalize.title_similarity("Data Scientist", "Senior Data Scientist") >= 0.8
    assert normalize.title_similarity("AI Engineer", "Data Engineer") < 0.8
    assert normalize.title_similarity("Data Scientist", "Data Scientist (m/w/d)") == 1.0
    assert normalize.title_similarity("Data Scientist", "") == 0.0
    assert normalize.title_similarity("", "") == 0.0


def test_containment_treats_an_empty_side_as_no_evidence() -> None:
    assert normalize._containment(set(), set()) == 1.0
    assert normalize._containment({"a"}, set()) == 0.0
    assert normalize._containment({"a", "b"}, {"a"}) == 1.0


def test_canonical_url_drops_tracking_and_trailing_noise() -> None:
    assert normalize.canonical_url(
        "HTTPS://WWW.Example.test/jobs/1/?utm_source=x&gh_src=y&id=7"
    ) == "https://example.test/jobs/1?id=7"
    assert normalize.canonical_url("https://example.test/") == "https://example.test/"
    assert normalize.canonical_url("") == ""


def test_canonical_url_orders_the_parameters_it_keeps() -> None:
    # Two spellings of one URL must collapse, or the same job is stored twice.
    assert (normalize.canonical_url("https://x.test/j?b=2&a=1")
            == normalize.canonical_url("https://x.test/j?a=1&b=2"))


# ===========================================================================
# normalize — to_text
# ===========================================================================

def test_entity_encoded_html_is_decoded_all_the_way() -> None:
    """Greenhouse serves HTML that has itself been entity-encoded.

    One unescape pass turned `&lt;p&gt;` into a live `<p>` and stopped there, so
    every Greenhouse body reached the scorer wrapped in its own markup — tag
    attributes and all.
    """
    got = normalize.to_text(
        "&lt;div class=\"content-intro\"&gt;We need &lt;strong&gt;Python"
        "&lt;/strong&gt; and SQL&lt;/div&gt;")
    assert "<" not in got and ">" not in got
    assert "content-intro" not in got
    assert "Python" in got and "SQL" in got


def test_plain_html_is_stripped_in_one_pass() -> None:
    got = normalize.to_text("<p>We need <strong>Python</strong></p>")
    assert "<" not in got
    assert "Python" in got


def test_prose_that_merely_contains_angle_brackets_is_left_alone() -> None:
    """Handing this to an HTML parser deletes the middle of the sentence."""
    assert normalize.to_text("Requires &lt;5 years and &gt;3 projects"
                             ) == "Requires <5 years and >3 projects"
    assert normalize.to_text("a < b > c is prose") == "a < b > c is prose"


def test_whitespace_is_collapsed_without_losing_paragraphs() -> None:
    assert normalize.to_text("one   two\t\tthree") == "one two three"
    assert normalize.to_text("a\n\n\n\n\nb") == "a\n\nb"
    assert normalize.to_text("hard\xa0space") == "hard space"
    assert normalize.to_text("  padded  ") == "padded"


def test_nothing_in_is_nothing_out() -> None:
    assert normalize.to_text("") == ""
    assert normalize.to_text(None) == ""


def test_a_missing_html_parser_must_not_lose_the_body(monkeypatch) -> None:
    """The regex fallback is lossier, but losing the text outright is worse."""
    import builtins

    real_import = builtins.__import__

    def no_bs4(name, *args, **kwargs):
        if name == "bs4":
            raise ImportError("no bs4 here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_bs4)
    got = normalize.to_text("<p>We need <strong>Python</strong> and SQL</p>")
    assert "Python" in got and "SQL" in got
    assert "<" not in got


# ===========================================================================
# normalize — parse_date
# ===========================================================================

@pytest.mark.parametrize("value,expected", [
    ("2026-08-04", dt.date(2026, 8, 4)),
    ("2026-08-04T09:30:00Z", dt.date(2026, 8, 4)),
    ("2026-08-04T09:30:00+02:00", dt.date(2026, 8, 4)),
    ("04.08.2026", dt.date(2026, 8, 4)),
    ("04/08/2026", dt.date(2026, 8, 4)),
    ("Aug 4, 2026", dt.date(2026, 8, 4)),
    ("4 Aug 2026", dt.date(2026, 8, 4)),
    (dt.date(2026, 8, 4), dt.date(2026, 8, 4)),
    (dt.datetime(2026, 8, 4, 9, 30), dt.date(2026, 8, 4)),
])
def test_every_date_format_the_sources_emit(value, expected) -> None:
    assert normalize.parse_date(value) == expected


def test_epoch_seconds_and_milliseconds_are_told_apart() -> None:
    # Lever uses milliseconds; Workday sometimes seconds.
    seconds = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    assert normalize.parse_date(seconds) == dt.date(2026, 8, 4)
    assert normalize.parse_date(seconds * 1000) == dt.date(2026, 8, 4)


def test_a_nonsensical_timestamp_is_refused_rather_than_raising() -> None:
    assert normalize.parse_date(1e18) is None


@pytest.mark.parametrize("text,days", [
    ("Posted 3 Days Ago", 3),
    ("Posted 30+ Days Ago", 30),
    ("2 weeks ago", 14),
    ("1 month ago", 30),
])
def test_workday_style_relative_dates(text, days) -> None:
    assert normalize.parse_date(text) == dt.date.today() - dt.timedelta(days=days)


@pytest.mark.parametrize("text", ["Today", "heute", "Just posted"])
def test_todays_postings_say_so_in_words(text) -> None:
    assert normalize.parse_date(text) == dt.date.today()


@pytest.mark.parametrize("value", [None, "", "   ", "sometime soon", "not a date"])
def test_an_unreadable_date_is_none(value) -> None:
    assert normalize.parse_date(value) is None


# ===========================================================================
# normalize — Posting
# ===========================================================================

def make(**over) -> Posting:
    fields = {
        "source": "ats:Example", "provider": "greenhouse", "source_job_id": "1",
        "url": "  https://www.example.test/jobs/1/?utm_source=x  ",
        "company": "  Example GmbH ", "title": " Data Scientist (m/w/d) ",
        "location": " Berlin, Germany ",
    }
    fields.update(over)
    return Posting(**fields)


def test_a_posting_tidies_its_own_fields_on_construction() -> None:
    post = make()
    assert post.company == "Example GmbH"
    assert post.title == "Data Scientist"
    assert post.location == "Berlin, Germany"
    assert post.canonical_url == "https://example.test/jobs/1"


def test_the_fingerprint_collapses_a_mirror_of_the_same_posting() -> None:
    """Built from employer + role + URL path, not the source's own job id.

    The same role on a company board and on an aggregator that mirrors it are
    one job, and storing both means pinging twice about one opening.
    """
    board = make(source_job_id="gh-1")
    mirror = make(source_job_id="agg-99", provider="arbeitsagentur",
                  company="Example Gesellschaft mit beschränkter Haftung",
                  title="Data Scientist")
    assert board.fingerprint == mirror.fingerprint


def test_a_different_role_at_the_same_employer_is_a_different_posting() -> None:
    assert make().fingerprint != make(title="Data Engineer",
                                      url="https://example.test/jobs/2").fingerprint


def test_the_loose_key_keeps_countries_apart() -> None:
    """Large employers list one title in a dozen countries.

    Without the country in the key, the Basel opening and the Hyderabad opening
    collapse into one record — and if the one that arrived first was outside the
    target region, the good one is silently discarded by the country prefilter.
    """
    basel = make(country="CH", url="https://example.test/jobs/basel")
    hyderabad = make(country="IN", url="https://example.test/jobs/hyd")
    assert basel.loose_key != hyderabad.loose_key
    assert basel.loose_key == make(country="ch",
                                   url="https://example.test/other").loose_key


def test_age_days_admits_when_there_is_no_date() -> None:
    assert make(posted_at=None).age_days is None
    assert make(posted_at=dt.date.today()).age_days == 0
    assert make(posted_at=dt.date.today() - dt.timedelta(days=7)).age_days == 7


def test_the_derived_readings_follow_a_description_filled_in_later() -> None:
    """Properties, not fields, because hydration happens after construction.

    Sources that need a second HTTP call arrive with an empty body, so a value
    computed once at construction would be stale for exactly the postings that
    survive far enough to matter.
    """
    post = make(title="Senior Data Scientist")
    assert post.years_required is None
    assert post.languages == []

    post.description = ("5+ years of experience required. "
                        "Verhandlungssicheres Deutsch (C1). Vollzeit, hybrid.")
    assert post.years_required == 5
    assert post.languages == ["German C1"]
    assert post.contract == "full-time"
    assert post.arrangement == "hybrid"
    assert post.level == "senior"
    assert post.city == "Berlin"


def test_the_summary_line_names_what_it_could_not_read() -> None:
    assert "Berlin, Germany" in make().summary()
    assert "?" in make(location="", remote=False).summary()
    assert "Remote" in make(location="", remote=True).summary()


# ===========================================================================
# dedupe
# ===========================================================================

@pytest.fixture()
def identity(monkeypatch):
    """`identity.toml` is git-ignored, so no checkout can rely on the real one."""
    class _Identity:
        file_prefix = "Doe, Jane"

        def doc_name(self, label: str, suffix: str = ".tex") -> str:
            return f"{self.file_prefix} - {label}{suffix}"

    monkeypatch.setattr(dedupe, "load_identity", lambda: _Identity())
    return _Identity()


def application(root, company: str, date: str, role: str,
                documents: tuple[str, ...] = ()) -> str:
    folder = root / date[:4] / company / f"{date} - {role}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in documents:
        (folder / name).write_bytes(b"%PDF-1.7\n")
    return str(folder)


def tracker(root, year: int, rows: list[tuple], headers: tuple[str, ...] | None = None):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Applications"
    sheet.append(list(headers or ("Company", "Position Applied", "Date Applied",
                                  "Application Folder")))
    for row in rows:
        sheet.append(list(row))
    (root / str(year)).mkdir(parents=True, exist_ok=True)
    workbook.save(root / str(year) / f"Job Applications Tracker {year}.xlsx")


def test_a_prior_application_in_the_folder_tree_is_found(tmp_path) -> None:
    today = dt.date.today()
    application(tmp_path, "Example GmbH", today.isoformat(), "Data Scientist")

    hit = dedupe.find_existing("Example GmbH", "Data Scientist", root=tmp_path)
    assert hit is not None
    assert hit.origin == "folder"
    assert hit.applied_on == today
    assert "Data Scientist" in hit.describe()


def test_the_employer_is_matched_through_its_legal_form(tmp_path) -> None:
    today = dt.date.today().isoformat()
    application(tmp_path, "PFALZWERKE", today, "Data Scientist")

    assert dedupe.find_existing("PFALZWERKE AKTIENGESELLSCHAFT",
                                "Data Scientist", root=tmp_path) is not None


def test_a_different_role_at_the_same_employer_is_not_a_duplicate(tmp_path) -> None:
    application(tmp_path, "Example", dt.date.today().isoformat(), "Sales Manager")
    assert dedupe.find_existing("Example", "Data Scientist", root=tmp_path) is None


def test_an_application_older_than_the_lookback_is_forgotten(tmp_path) -> None:
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    application(tmp_path, "Example", old, "Data Scientist")

    assert dedupe.find_existing("Example", "Data Scientist", root=tmp_path) is None
    assert dedupe.find_existing("Example", "Data Scientist", lookback_days=500,
                                root=tmp_path) is not None


@pytest.mark.parametrize("name", [
    "not-a-date - Data Scientist",   # no ISO date at the front
    "2026-13-45 - Data Scientist",   # a date shape that is not a date
    "Data Scientist",                # no date at all
])
def test_a_folder_that_is_not_an_application_is_skipped(tmp_path, name) -> None:
    (tmp_path / "2026" / "Example" / name).mkdir(parents=True)
    (tmp_path / "2026" / "Example" / "loose.txt").write_text("x", encoding="utf-8")
    assert dedupe.find_existing("Example", "Data Scientist", root=tmp_path) is None


def test_a_directory_that_is_not_a_year_is_not_walked(tmp_path) -> None:
    application(tmp_path, "Example", dt.date.today().isoformat(), "Data Scientist")
    (tmp_path / "master" / "Example").mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    assert len(dedupe.collect_existing("Example", "Data Scientist",
                                       root=tmp_path)) == 1


def test_a_tracker_row_counts_as_a_prior_application(tmp_path) -> None:
    year = dt.date.today().year
    tracker(tmp_path, year, [("Example GmbH", "Data Scientist",
                              dt.date.today(), "")])

    hit = dedupe.find_existing("Example GmbH", "Data Scientist", root=tmp_path)
    assert hit is not None
    assert hit.origin == "tracker"


def test_a_folder_outranks_a_tracker_row_for_the_same_application(tmp_path) -> None:
    """They usually agree; when they disagree the folder wins.

    A missing tracker row is a known non-fatal outcome of the pipeline.
    """
    today = dt.date.today()
    application(tmp_path, "Example", today.isoformat(), "Data Scientist")
    tracker(tmp_path, today.year,
            [("Example", "Data Scientist", today, "")])

    hits = dedupe.collect_existing("Example", "Data Scientist", root=tmp_path)
    assert [h.origin for h in hits] == ["folder", "tracker"]


def test_the_other_employers_in_the_tracker_are_passed_over(tmp_path) -> None:
    """The workbook holds every application ever sent, not just this employer's."""
    today = dt.date.today()
    tracker(tmp_path, today.year, [
        ("Other GmbH", "Data Scientist", today, ""),
        ("", "Data Scientist", today, ""),            # a half-filled row
        ("Example", "Data Scientist", today, ""),
    ])

    hits = dedupe.collect_existing("Example", "Data Scientist", root=tmp_path)
    assert [h.company for h in hits] == ["Example"]


def test_a_tracker_row_reports_the_folder_it_names(tmp_path) -> None:
    today = dt.date.today()
    tracker(tmp_path, today.year,
            [("Example", "Data Scientist", today, "2026/Example/x")])

    hit = dedupe.find_existing("Example", "Data Scientist", root=tmp_path)
    assert hit.folder == "2026/Example/x"
    assert "2026/Example/x" in hit.describe()


def test_a_tracker_datetime_is_read_as_a_date(tmp_path) -> None:
    now = dt.datetime.now()
    tracker(tmp_path, now.year, [("Example", "Data Scientist", now, "")])

    hit = dedupe.find_existing("Example", "Data Scientist", root=tmp_path)
    assert hit.applied_on == now.date()


def test_a_tracker_row_with_no_usable_date_is_still_a_hit(tmp_path) -> None:
    # The date column is the user's to edit; a blank one must not hide the row.
    tracker(tmp_path, dt.date.today().year,
            [("Example", "Data Scientist", "", "")])

    hit = dedupe.find_existing("Example", "Data Scientist", root=tmp_path)
    assert hit is not None
    assert hit.applied_on is None
    assert "unknown date" in hit.describe()


def test_an_old_tracker_row_falls_outside_the_lookback(tmp_path) -> None:
    old = dt.date.today() - dt.timedelta(days=400)
    tracker(tmp_path, dt.date.today().year, [("Example", "Data Scientist", old, "")])

    assert dedupe.find_existing("Example", "Data Scientist", root=tmp_path) is None


def test_a_tracker_missing_the_columns_we_need_is_ignored(tmp_path) -> None:
    """The user may add or remove columns in Excel; that must not raise."""
    tracker(tmp_path, dt.date.today().year,
            [("Example", "Data Scientist")], headers=("Company", "Role"))

    assert dedupe.collect_existing("Example", "Data Scientist", root=tmp_path) == []


def test_a_locked_or_malformed_workbook_does_not_block_a_build(tmp_path) -> None:
    year = dt.date.today().year
    (tmp_path / str(year)).mkdir(parents=True)
    (tmp_path / str(year) / f"Job Applications Tracker {year}.xlsx").write_text(
        "this is not a workbook", encoding="utf-8")

    assert dedupe.collect_existing("Example", "Data Scientist", root=tmp_path) == []


def test_a_tracker_sheet_under_another_name_is_ignored(tmp_path) -> None:
    from openpyxl import Workbook

    year = dt.date.today().year
    workbook = Workbook()
    workbook.active.title = "Sheet1"
    (tmp_path / str(year)).mkdir(parents=True)
    workbook.save(tmp_path / str(year) / f"Job Applications Tracker {year}.xlsx")

    assert dedupe.collect_existing("Example", "Data Scientist", root=tmp_path) == []


def test_no_tracker_and_no_folders_is_not_a_duplicate(tmp_path) -> None:
    assert dedupe.collect_existing("Example", "Data Scientist", root=tmp_path) == []
    assert dedupe.find_existing("Example", "Data Scientist", root=tmp_path) is None


def test_openpyxl_being_absent_leaves_the_folder_check_working(
        tmp_path, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_openpyxl(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl here")
        return real_import(name, *args, **kwargs)

    application(tmp_path, "Example", dt.date.today().isoformat(), "Data Scientist")
    monkeypatch.setattr(builtins, "__import__", no_openpyxl)

    hits = dedupe.collect_existing("Example", "Data Scientist", root=tmp_path)
    assert [h.origin for h in hits] == ["folder"]


# --- completeness ----------------------------------------------------------

def test_the_required_and_optional_documents_are_named_from_the_identity(
        identity) -> None:
    assert dedupe.required_pdfs() == ("Doe, Jane - CV.pdf",
                                      "Doe, Jane - Cover Letter.pdf")
    assert dedupe.optional_pdfs() == ("Doe, Jane - Lebenslauf.pdf",
                                      "Doe, Jane - Anschreiben.pdf",
                                      "Doe, Jane - Interview Prep.pdf")


def test_a_folder_with_no_documents_is_not_an_application(tmp_path, identity) -> None:
    """A run that died partway leaves the folder behind with no PDFs in it.

    Treating that as "already applied" would block the role from ever being
    retried, so the duplicate check asks for the documents, not the directory.
    """
    folder = application(tmp_path, "Example", "2026-08-04", "Data Scientist")
    assert dedupe.missing_documents(folder) == list(dedupe.required_pdfs())
    assert dedupe.present_documents(folder) == []


def test_a_folder_that_is_not_there_is_missing_everything(tmp_path, identity) -> None:
    assert dedupe.missing_documents(tmp_path / "nope") == list(dedupe.required_pdfs())
    assert dedupe.present_documents(tmp_path / "nope") == []


def test_a_half_finished_run_names_what_it_still_owes(tmp_path, identity) -> None:
    folder = application(tmp_path, "Example", "2026-08-04", "Data Scientist",
                         documents=("Doe, Jane - CV.pdf",))
    assert dedupe.missing_documents(folder) == ["Doe, Jane - Cover Letter.pdf"]


def test_the_conditional_documents_are_reported_but_never_required(
        tmp_path, identity) -> None:
    folder = application(
        tmp_path, "Example", "2026-08-04", "Data Scientist",
        documents=("Doe, Jane - CV.pdf", "Doe, Jane - Cover Letter.pdf",
                   "Doe, Jane - Interview Prep.pdf", "notes.txt"))
    assert dedupe.missing_documents(folder) == []
    assert dedupe.present_documents(folder) == [
        "Doe, Jane - CV.pdf", "Doe, Jane - Cover Letter.pdf",
        "Doe, Jane - Interview Prep.pdf",
    ]


def test_is_complete_asks_the_folder_when_there_is_one(tmp_path, identity) -> None:
    today = dt.date.today()
    empty = application(tmp_path, "Example", today.isoformat(), "Data Scientist")
    hit = dedupe.find_existing("Example", "Data Scientist", root=tmp_path)
    assert hit.folder == empty
    assert not dedupe.is_complete(hit)

    for name in dedupe.required_pdfs():
        (tmp_path / today.strftime("%Y") / "Example"
         / f"{today.isoformat()} - Data Scientist" / name).write_bytes(b"%PDF")
    assert dedupe.is_complete(hit)


def test_a_tracker_row_without_a_folder_is_taken_at_its_word(identity) -> None:
    """The tracker is only written at step 10, after final QA passed."""
    row = dedupe.ExistingApplication(
        company="Example", title="Data Scientist", applied_on=dt.date.today(),
        folder="", similarity=1.0, origin="tracker")
    assert dedupe.is_complete(row)

    orphan = dedupe.ExistingApplication(
        company="Example", title="Data Scientist", applied_on=dt.date.today(),
        folder="", similarity=1.0, origin="folder")
    assert not dedupe.is_complete(orphan)
