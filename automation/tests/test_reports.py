"""The terminal reports, and the grammar of a reply.

`watcher.status` and `watcher.health` are what someone runs when the phone
report has said something is wrong and they want to know what. Neither has any
business raising: a source with no health row, a database recorded before a
column existed, a config with a section missing — all of those are exactly the
states these reports exist to describe, so falling over on one is the failure
mode that matters.

`replies.parse` is the other end of the same conversation. It decides whether a
message in the chat starts a 40-minute build, and it is deliberately hard to
say yes by accident: a stray message with no keyword is UNKNOWN, and the
handler asks rather than building. Everything below is either a phrasing a
real reply has used, or a way to be misread as approval.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from watcher import health as health_cli
from watcher import replies, status, store
from watcher.config import (Config, Filters, LocationFilter, SeniorityFilter,
                            ExperienceFilter, SourceDefaults, Sources)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def health_row(source: str, *, disabled=0, failures=0, error="",
               parked=0, last_ok=None, disabled_at=None) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT ? AS source, ? AS disabled, ? AS consecutive_failures, "
        "? AS last_error, ? AS parked, ? AS last_ok_at, ? AS disabled_at",
        (source, disabled, failures, error, parked, last_ok, disabled_at)
    ).fetchone()


def sources(*, ats=(), portals=(), filters=None) -> Sources:
    return Sources(defaults=SourceDefaults(countries=("DE",), title_deny=()),
                   ats=tuple(ats), portals=tuple(portals),
                   filters=filters or Filters())


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    return path


@pytest.fixture()
def quiet_env(monkeypatch):
    """No real .env, no real config, no real sources — and no real token."""
    monkeypatch.setattr(status, "load_env", lambda: None)
    monkeypatch.setattr(health_cli, "load_config", lambda: Config())
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


# ===========================================================================
# formatting helpers
# ===========================================================================

def test_a_rule_fills_the_width_whatever_the_title(capsys) -> None:
    status.rule("POLLING")
    status.rule("A" * 200)
    first, second = [l for l in capsys.readouterr().out.splitlines() if l]
    assert len(first) == status.WIDTH
    assert second.endswith("───"), "a long title still gets a visible rule"


def test_a_long_list_wraps_under_its_label_rather_than_off_the_screen(
        capsys) -> None:
    status.wrapped("accept from", [f"country-{i}" for i in range(20)])
    lines = capsys.readouterr().out.rstrip().splitlines()
    assert len(lines) > 1
    assert lines[0].startswith("  accept from")
    assert all(len(line) <= status.WIDTH for line in lines)
    assert lines[1].startswith("  " + " " * 20), "continuations line up"


def test_an_empty_list_says_so_rather_than_printing_a_blank(capsys) -> None:
    status.wrapped("seniority deny", [])
    status.wrapped("cities", [], empty="(anywhere)")
    out = capsys.readouterr().out
    assert "(none)" in out and "(anywhere)" in out


def test_a_value_from_the_environment_is_marked_as_overriding_the_file(
        monkeypatch) -> None:
    """A knob read from .env looks like it came from the tracked file, and the
    two disagreeing is exactly what a status report exists to surface."""
    assert status.env_note("poll", "interval_minutes") == ""
    monkeypatch.setenv("WATCHER_POLL_INTERVAL_MINUTES", "5")
    assert status.env_note("poll", "interval_minutes") == \
        "   [WATCHER_POLL_INTERVAL_MINUTES]"
    monkeypatch.setenv("WATCHER_POLL_INTERVAL_MINUTES", "   ")
    assert status.env_note("poll", "interval_minutes") == "", \
        "a variable set to whitespace overrides nothing"


# ===========================================================================
# one source's state
# ===========================================================================

def test_a_source_that_has_never_been_polled_is_not_reported_as_healthy() -> None:
    """"ok" for a source that has never run once is the single most misleading
    thing this table could say."""
    assert status.state_of(None) == "unpolled"
    assert status.health_detail(None) == "no poll recorded yet"


@pytest.mark.parametrize("row,expected", [
    (health_row("a"), "ok"),
    (health_row("a", failures=3), "fail x3"),
    (health_row("a", disabled=1), "DISABLED"),
    (health_row("a", disabled=1, parked=1), "PARKED"),
])
def test_parked_gets_its_own_word(row, expected) -> None:
    """Strictly worse than disabled and wants a different response: one comes
    back on its own, the other waits for someone to change the fetcher."""
    assert status.state_of(row) == expected


def test_a_healthy_source_has_no_retry_note() -> None:
    assert status.retry_note(health_row("a")) == ""
    assert status.retry_note(None) == ""
    assert status.retry_note(health_row("a", failures=2)) == ""


def test_a_disabled_source_says_when_it_will_come_back(monkeypatch) -> None:
    row = health_row("a", disabled=1,
                     disabled_at=(dt.datetime.now()
                                  - dt.timedelta(minutes=6)).isoformat())
    note = status.retry_note(row, Config(poll={"retry_after_minutes": 30,
                                               "retry_backoff_factor": 1.0}))
    assert note == "retry in 24m"


def test_a_retry_already_due_says_so_rather_than_counting_down_past_zero() -> None:
    row = health_row("a", disabled=1,
                     disabled_at=(dt.datetime.now()
                                  - dt.timedelta(hours=4)).isoformat())
    assert status.retry_note(row, Config(poll={"retry_after_minutes": 30})) == \
        "retry due now"


def test_with_auto_retry_off_the_note_says_what_to_run(monkeypatch) -> None:
    row = health_row("a", disabled=1, disabled_at=dt.datetime.now().isoformat())
    assert status.retry_note(row, Config(poll={"retry_after_minutes": 0})) == \
        "no auto-retry"


def test_a_parked_source_says_retrying_will_not_help() -> None:
    """It is a work item, not weather, and no amount of waiting clears it."""
    row = health_row("a", disabled=1, parked=1,
                     disabled_at=dt.datetime.now().isoformat())
    assert "fix the fetcher" in status.retry_note(
        row, Config(poll={"retry_after_minutes": 30}))


def test_the_detail_line_carries_the_last_success_and_the_retry() -> None:
    assert status.health_detail(health_row("a")) == "last ok never"
    assert status.health_detail(health_row("a", last_ok="2026-08-04T10:00:00")) \
        == "last ok 2026-08-04T10:00:00"
    row = health_row("a", disabled=1, disabled_at=dt.datetime.now().isoformat())
    detail = status.health_detail(row, Config(poll={"retry_after_minutes": 30}))
    assert detail.startswith("last ok never") and "retry in" in detail


# ===========================================================================
# the source blocks
# ===========================================================================

ACME = {"company": "Acme", "provider": "greenhouse", "token": "acme"}


def test_a_company_board_shows_its_state_and_the_page_a_human_can_open(
        capsys) -> None:
    status.print_ats(sources(ats=[ACME]),
                     {"ats:Acme": health_row("ats:Acme")}, False, Config())
    out = capsys.readouterr().out
    assert "COMPANY BOARDS  (1 watched)" in out
    assert "ok" in out and "Acme" in out and "greenhouse" in out
    assert "https://job-boards.greenhouse.io/acme" in out
    assert "polled:" not in out, "the API endpoint is behind --verbose"


def test_verbose_adds_the_endpoint_actually_being_fetched(capsys) -> None:
    """The board page and the feed are different URLs, and when a board breaks
    the second is the one to paste into curl."""
    status.print_ats(sources(ats=[ACME]), {}, True, Config())
    out = capsys.readouterr().out
    assert "polled:" in out and "boards-api.greenhouse.io" in out


def test_a_half_written_entry_costs_a_url_not_the_report(capsys) -> None:
    """The poller would reject this outright, which is precisely why someone
    would be running the status report."""
    status.print_ats(sources(ats=[{"company": "Acme", "provider": "greenhouse"},
                                   {"company": "Other", "provider": "made-up"}]),
                     {}, True, Config())
    out = capsys.readouterr().out
    assert "Acme" in out and "Other" in out
    assert "greenhouse.io" not in out


def test_a_switched_off_source_is_not_shown_as_a_health_problem(capsys) -> None:
    """`enabled = false` is a choice someone made, not something to fix."""
    status.print_ats(
        sources(ats=[{**ACME, "enabled": False}]),
        {"ats:Acme": health_row("ats:Acme", disabled=1)}, False, Config())
    out = capsys.readouterr().out
    assert "(0 watched, 1 off)" in out
    assert "enabled = false in sources.toml" in out
    assert "DISABLED" not in out


def test_a_failing_source_prints_the_error_that_diagnoses_it(capsys) -> None:
    status.print_ats(
        sources(ats=[ACME]),
        {"ats:Acme": health_row("ats:Acme", failures=2,
                                error="HTTP 404 for https://example.test/x")},
        False, Config())
    assert "! HTTP 404 for https://example.test/x" in capsys.readouterr().out


def test_a_stale_error_from_a_source_now_working_is_not_shown(capsys) -> None:
    """The column already says `ok`; printing the error it recovered from
    beside that reads as a live failure."""
    status.print_ats(
        sources(ats=[ACME]),
        {"ats:Acme": health_row("ats:Acme", error="an old timeout")},
        False, Config())
    assert "an old timeout" not in capsys.readouterr().out


def test_a_portal_shows_its_tier_with_the_failures_it_is_allowed(capsys) -> None:
    """The tier decides how much failure the source gets before it switches
    itself off, so the word alone leaves the actual number a lookup."""
    status.print_portals(
        sources(portals=[{"name": "stepstone", "fragile": True,
                          "location": "Köln", "radius_km": 50,
                          "queries": ["data scientist"]}]),
        {}, False, Config(poll={"fragile_failures_before_disable": 2}))
    out = capsys.readouterr().out
    assert "PORTALS  (1 watched)" in out
    assert "fragile (x2)" in out
    assert "location: Köln +50km" in out
    assert "data scientist" in out


def test_a_portal_with_no_location_says_anywhere_rather_than_nothing(
        capsys) -> None:
    status.print_portals(
        sources(portals=[{"name": "hiringcafe", "queries": []}]), {}, True,
        Config())
    out = capsys.readouterr().out
    assert "location: (anywhere)" in out
    assert "stable" in out
    assert "data endpoint" in out


# ===========================================================================
# the settings blocks
# ===========================================================================

def test_the_filter_block_says_where_and_what(capsys) -> None:
    status.print_filters(sources(filters=Filters(
        location=LocationFilter(regions=("DACH",), exclude_countries=("CH",),
                                cities=("Berlin",), exclude_cities=("Bonn",),
                                remote_ok=True, remote_anywhere=False),
        seniority=SeniorityFilter(allow=("mid",), deny=("intern",)),
        experience=ExperienceFilter(mode="report"))))
    out = capsys.readouterr().out
    assert "accept from" in out and "DACH" in out
    assert "except" in out and "CH" in out
    assert "only in cities" in out and "Berlin" in out
    assert "not in cities" in out and "Bonn" in out
    assert "employer must sit in an allowed country" in out
    assert "unknown rank is never filtered" in out
    assert "shown in the ping, never acted on" in out


def test_no_geography_at_all_says_so_rather_than_showing_an_empty_list(
        capsys) -> None:
    status.print_filters(sources())
    out = capsys.readouterr().out
    assert "(anywhere)" in out
    assert "no geographic restriction" in out


def test_remote_switched_off_says_it_gets_no_special_treatment(capsys) -> None:
    status.print_filters(sources(filters=Filters(
        location=LocationFilter(countries=("DE",), remote_ok=False))))
    out = capsys.readouterr().out
    assert "no special treatment" in out
    assert "remote_anywhere" not in out, "nothing for it to qualify"


def test_a_wide_country_list_is_summarised_rather_than_printed(capsys) -> None:
    """A continent expands to fifty codes, and the point of this line is
    whether a restriction is in force at all."""
    status.print_filters(sources(filters=Filters(
        location=LocationFilter(continents=("Europe",)))))
    out = capsys.readouterr().out
    assert "countries" in out and "watcher.geo --expand" in out


def test_a_short_country_list_is_printed_in_full(capsys) -> None:
    status.print_filters(sources(filters=Filters(
        location=LocationFilter(countries=("DE", "AT", "CH")))))
    out = capsys.readouterr().out
    assert "AT" in out and "CH" in out and "DE" in out
    assert "watcher.geo" not in out


def test_remote_anywhere_is_reported_when_it_is_on(capsys) -> None:
    status.print_filters(sources(filters=Filters(
        location=LocationFilter(countries=("DE",), remote_ok=True,
                                remote_anywhere=True))))
    assert "remote roles outside those countries too" in capsys.readouterr().out


def test_a_filtering_experience_cap_names_the_number(capsys) -> None:
    status.print_filters(sources(filters=Filters(
        experience=ExperienceFilter(mode="filter", max_years=8))))
    assert "drop above 8 years" in capsys.readouterr().out


def test_the_behaviour_block_covers_every_knob_a_reply_can_change(
        capsys) -> None:
    status.print_behaviour(Config(
        poll={"interval_minutes": 30, "retry_after_minutes": 20},
        match={"notify_threshold": 70, "digest_threshold": 40},
        notify={"topics": {"new_posting": 12}},
        build={"enabled": True}, kb={"enabled": True}))
    out = capsys.readouterr().out
    assert "POLLING" in out and "every" in out and "30 min" in out
    assert "MATCHING" in out and "score ≥ 70" in out and "score ≥ 40" in out
    assert "NOTIFICATIONS" in out
    assert "HEADLESS BUILDS" in out and "an approval reply spawns a run" in out
    assert "WEEKLY KB PASS" in out and "only on an explicit yes" in out


def test_the_retry_ladder_is_shown_rather_than_just_the_first_step(
        capsys) -> None:
    """The interval doubles, so the first number alone understates how long a
    source stays dark after a bad afternoon."""
    status.print_behaviour(Config(poll={"retry_after_minutes": 15,
                                        "retry_backoff_factor": 2.0,
                                        "retry_backoff_max_minutes": 240}))
    out = capsys.readouterr().out
    assert "15m, 30m, 60m, 120m, … capped at 240 min" in out


def test_retries_switched_off_say_what_to_run_instead(capsys) -> None:
    status.print_behaviour(Config(poll={"retry_after_minutes": 0}))
    assert "never (manual --reset only)" in capsys.readouterr().out


def test_topic_routing_is_listed_only_when_it_is_on(capsys) -> None:
    status.print_behaviour(Config())
    assert "topics" not in capsys.readouterr().out

    status.print_behaviour(Config(notify={"topics": {"new_posting": 12}}))
    out = capsys.readouterr().out
    assert "new_posting" in out and "12" in out
    assert "General" in out, "the four unset kinds still land somewhere"


def test_builds_switched_off_are_reported_as_a_choice(capsys) -> None:
    status.print_behaviour(Config(build={"enabled": False}))
    assert "approvals are recorded, nothing is built" in capsys.readouterr().out


def test_the_kb_block_stops_at_one_line_when_the_pass_is_off(capsys) -> None:
    status.print_behaviour(Config(kb={"enabled": False}))
    out = capsys.readouterr().out
    assert "WEEKLY KB PASS" in out
    assert "runs" not in out.split("WEEKLY KB PASS")[1]


@pytest.mark.parametrize("weekday,name", [(0, "Monday"), (6, "Sunday"),
                                          (9, "Wednesday")])
def test_the_kb_day_is_named_rather_than_numbered(capsys, weekday, name) -> None:
    """Including a number outside the week, which config allows and which
    would otherwise be an IndexError in a read-only report."""
    status.print_behaviour(Config(kb={"enabled": True, "weekday": weekday}))
    assert name in capsys.readouterr().out


# ===========================================================================
# files and stored state
# ===========================================================================

def test_the_file_block_never_echoes_the_token(monkeypatch, capsys) -> None:
    """This output gets pasted into chats."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567:AAsecretsecretsecret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    status.print_files()
    out = capsys.readouterr().out
    assert "AAsecret" not in out and "1234567:" not in out
    assert "telegram token" in out and "set" in out
    assert "pinned (…7890)" in out


def test_a_missing_token_is_called_out_rather_than_left_blank(monkeypatch,
                                                              capsys) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    status.print_files()
    out = capsys.readouterr().out
    assert "MISSING — the watcher cannot notify" in out
    assert "not pinned" in out


def test_a_missing_file_is_reported_rather_than_raising(monkeypatch, tmp_path,
                                                        capsys) -> None:
    monkeypatch.setattr(status, "CONFIG_PATH", tmp_path / "nope.toml")
    status.print_files()
    assert "missing" in capsys.readouterr().out


def test_a_size_is_scaled_to_the_file(tmp_path) -> None:
    small = tmp_path / "small.txt"
    small.write_bytes(b"x" * 2048)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    assert status._size(small) == "2 KB"
    assert status._size(big) == "3.0 MB"
    assert status._size(tmp_path) == "ok"
    assert status._size(tmp_path / "gone") == "missing"


def test_a_path_outside_the_repo_is_printed_whole(tmp_path) -> None:
    assert status._short(tmp_path) == str(tmp_path)
    assert "/" in status._short(status.CONFIG_PATH)


def test_the_state_block_reads_the_database(db, capsys) -> None:
    with store.connect() as conn:
        store.set_meta(conn, store.LAST_CYCLE_KEY, "")
    status.print_state(Config())
    out = capsys.readouterr().out
    assert "postings seen" in out and "scored" in out
    assert "waiting for a ping" in out and "waiting for digest" in out
    assert "never — no cycle has been recorded yet" in out


def test_the_state_block_shows_the_last_cycle_and_its_funnel(db, capsys) -> None:
    with store.connect() as conn:
        store.save_cycle(conn, {
            "finished_at": "2026-08-04T14:32:00", "seconds": 42,
            "fetched": 100, "already_known": 90, "filtered": 5, "stored": 5,
            "scored": 5, "deferred": 1, "notified": 2,
            "sources_failed": ["portal:stepstone"],
            "sources": {"ats:Acme": {"fetched": 60, "already_known": 55,
                                     "filtered": 3, "stored": 2},
                        "portal:stepstone": {"error": "HTTP 403 Forbidden"}}})
    status.print_state(Config())
    out = capsys.readouterr().out
    assert "100 fetched → 90 already seen → 5 filtered → 5 stored" in out
    assert "5 scored, 1 deferred, 2 pinged" in out
    assert "sources failed" in out and "portal:stepstone" in out
    assert "ats:Acme" in out and "60" in out
    assert "failed — HTTP 403 Forbidden" in out


def test_a_cycle_predating_the_wider_funnel_is_not_shown_as_zeros(db,
                                                                  capsys) -> None:
    with store.connect() as conn:
        store.save_cycle(conn, {"fetched": 41, "stored": 3})
    status.print_state(Config())
    out = capsys.readouterr().out
    assert "41 fetched → 3 stored" in out
    assert "already seen" not in out


def test_the_state_block_names_the_newest_posting(db, capsys) -> None:
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
               canonical_url, company, title, first_seen_at)
               VALUES ('a','k','portal:test','test','https://e.test/1',
                       'https://e.test/1','ExampleCo','ML Engineer',?)""",
            (dt.datetime.now().isoformat(timespec="seconds"),))
    status.print_state(Config())
    assert "ExampleCo — ML Engineer" in capsys.readouterr().out


def test_the_unjudged_count_comes_with_the_command_that_clears_it(db,
                                                                  capsys) -> None:
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
               canonical_url, company, title, first_seen_at)
               VALUES ('a','k','portal:test','test','https://e.test/1',
                       'https://e.test/1','ExampleCo','ML Engineer',?)""",
            (dt.datetime.now().isoformat(timespec="seconds"),))
        store.save_verdict(conn, "a", {"score": 45, "verdict": "maybe",
                                       "gaps": [store.DEGRADED_GAP]}, "haiku")
    status.print_state(Config())
    out = capsys.readouterr().out
    assert "unjudged" in out and "watcherctl rescore" in out


# ===========================================================================
# the status CLI
# ===========================================================================

def test_the_report_runs_end_to_end(db, quiet_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr(status, "load_config", lambda: Config())
    monkeypatch.setattr(status, "load_sources", lambda: sources(
        ats=[{"company": "Acme", "provider": "greenhouse", "board": "acme"}],
        portals=[{"name": "stepstone", "queries": ["data scientist"]}]))
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "WATCHING" in out and "SETTINGS" in out
    assert "COMPANY BOARDS" in out and "PORTALS" in out
    assert "FILTERS — where" in out and "POLLING" in out
    assert "FILES" in out and "STORED STATE" in out


def test_sources_only_skips_the_settings(db, quiet_env, monkeypatch,
                                          capsys) -> None:
    monkeypatch.setattr(status, "load_config", lambda: Config())
    monkeypatch.setattr(status, "load_sources", lambda: sources())
    assert status.main(["--sources-only"]) == 0
    out = capsys.readouterr().out
    assert "WATCHING" in out
    assert "SETTINGS" not in out and "STORED STATE" not in out


# ===========================================================================
# the health CLI
# ===========================================================================

def test_no_polls_yet_says_what_to_run(db, quiet_env, monkeypatch,
                                        capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    assert health_cli.main([]) == 0
    assert "No polls recorded yet" in capsys.readouterr().out


def fail(conn, source: str, error: str = "timeout", *, times: int = 1,
         structural: bool = False, threshold: int = 3) -> None:
    for _ in range(times):
        store.mark_source_failed(conn, source, error, threshold, structural)


def test_the_table_carries_the_state_the_failures_and_the_last_success(
        db, quiet_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    with store.connect() as conn:
        fail(conn, "portal:stepstone", "HTTP 403", times=2)
        store.mark_source_ok(conn, "ats:Acme")
    assert health_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "source" in out and "state" in out and "fails" in out
    assert "ats:Acme" in out and "ok" in out
    assert "portal:stepstone" in out and "2" in out
    assert "HTTP 403" in out


def test_a_disabled_source_gets_its_retry_line(db, quiet_env, monkeypatch,
                                                capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    monkeypatch.setattr(health_cli, "load_config",
                        lambda: Config(poll={"retry_after_minutes": 30}))
    with store.connect() as conn:
        fail(conn, "portal:stepstone", times=3)
    assert health_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "DISABLED" in out
    assert "retry in" in out or "retry due now" in out


def test_a_parked_source_says_retrying_cannot_fix_it(db, quiet_env, monkeypatch,
                                                      capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    with store.connect() as conn:
        fail(conn, "portal:stepstone", "HTTP 404 not found", times=3,
             structural=True)
    assert health_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "PARKED" in out
    assert "retrying cannot fix this" in out


def test_no_auto_retry_points_at_reset(db, quiet_env, monkeypatch,
                                        capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    monkeypatch.setattr(health_cli, "load_config",
                        lambda: Config(poll={"retry_after_minutes": 0}))
    with store.connect() as conn:
        fail(conn, "portal:stepstone", times=3)
    assert health_cli.main([]) == 0
    assert "no auto-retry — use --reset" in capsys.readouterr().out


def test_a_configured_source_that_has_never_polled_is_still_listed(
        db, quiet_env, monkeypatch, capsys) -> None:
    """Otherwise a source that has never once run reads as absent from the
    table rather than as the thing to look at."""
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources(ats=[ACME]))
    with store.connect() as conn:
        store.mark_source_ok(conn, "portal:other")
    assert health_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "ats:Acme" in out and "not polled" in out


def test_reset_clears_one_source(db, quiet_env, monkeypatch, capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    with store.connect() as conn:
        fail(conn, "portal:stepstone", times=3)
        assert store.is_source_disabled(conn, "portal:stepstone")

    assert health_cli.main(["--reset", "portal:stepstone"]) == 0
    assert "re-enabled: portal:stepstone" in capsys.readouterr().out
    with store.connect() as conn:
        assert not store.is_source_disabled(conn, "portal:stepstone")


def test_reset_all_clears_every_recorded_source(db, quiet_env, monkeypatch,
                                                 capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    with store.connect() as conn:
        for name in ("portal:a", "portal:b"):
            fail(conn, name, times=3)

    assert health_cli.main(["--reset", "ALL"]) == 0
    assert "portal:a" in capsys.readouterr().out
    with store.connect() as conn:
        assert not any(row["disabled"] for row in store.source_health(conn))


def test_resetting_with_nothing_recorded_is_not_an_error(db, quiet_env,
                                                          monkeypatch,
                                                          capsys) -> None:
    monkeypatch.setattr(health_cli, "load_sources", lambda: sources())
    assert health_cli.main(["--reset", "all"]) == 0
    assert "(nothing)" in capsys.readouterr().out


# ===========================================================================
# replies
# ===========================================================================

@pytest.mark.parametrize("text", ["yes", "Yes", "  y  ", "yeah", "yep", "ok",
                                  "okay", "go", "do it", "build", "apply",
                                  "ja", "los"])
def test_the_words_a_yes_arrives_as(text) -> None:
    assert replies.parse(text).action == replies.APPROVE


@pytest.mark.parametrize("text", ["no", "N", "nope", "skip", "nein", "pass",
                                  "drop"])
def test_the_words_a_no_arrives_as(text) -> None:
    assert replies.parse(text).action == replies.SKIP


@pytest.mark.parametrize("text", ["later", "snooze", "wait", "hold", "not now",
                                  "später", "spaeter"])
def test_the_words_a_not_yet_arrives_as(text) -> None:
    assert replies.parse(text).action == replies.SNOOZE


def test_a_multi_word_phrase_beats_the_word_inside_it() -> None:
    """"not now" starts with "no". Matched shortest-first it becomes a skip,
    which drops the posting for good instead of resurfacing it in three days."""
    assert replies.parse("not now").action == replies.SNOOZE
    assert replies.parse("not now, maybe friday").action == replies.SNOOZE
    assert replies.parse("not now — busy week").note == "— busy week"


def test_a_multi_word_approval_keeps_what_follows_it() -> None:
    assert replies.parse("do it, add German") == \
        replies.Reply(replies.APPROVE, "add German", None)


def test_the_note_reaches_the_pipeline_in_the_phrasing_it_was_typed_in() -> None:
    """It is passed through to the build prompt untouched, and the Trigger
    section of the project instructions reads it as written."""
    assert replies.parse("yes, add German").note == "add German"
    assert replies.parse("yes - add German").note == "add German"
    assert replies.parse("Yes: apply as Data Scientist").note == \
        "apply as Data Scientist"


def test_a_verb_that_governs_what_follows_it_is_kept(text=None) -> None:
    """"apply as Data Scientist" has to reach the pipeline whole. Dropping the
    keyword leaves "as Data Scientist", which reads as a fragment."""
    assert replies.parse("apply as Data Scientist").note == \
        "apply as Data Scientist"
    assert replies.parse("build the German one too").note == \
        "build the German one too"
    assert replies.parse("go with the Lebenslauf as well").note == \
        "go with the Lebenslauf as well"


def test_a_bare_number_is_a_request_to_build_that_line() -> None:
    assert replies.parse("3") == replies.Reply(replies.APPROVE, "", 3)
    assert replies.parse("#3") == replies.Reply(replies.APPROVE, "", 3)
    assert replies.parse("build 3") == replies.Reply(replies.APPROVE, "", 3)
    assert replies.parse("build3") == replies.Reply(replies.APPROVE, "", 3)


def test_an_index_carries_its_note(text=None) -> None:
    assert replies.parse("3, add German") == \
        replies.Reply(replies.APPROVE, "add German", 3)
    assert replies.parse("build 2 apply as Data Scientist") == \
        replies.Reply(replies.APPROVE, "apply as Data Scientist", 2)


def test_an_index_can_be_declined_as_well_as_approved() -> None:
    assert replies.parse("2 no") == replies.Reply(replies.SKIP, "", 2)
    assert replies.parse("2 later") == replies.Reply(replies.SNOOZE, "", 2)


def test_only_a_plausible_digest_line_is_read_as_an_index() -> None:
    """Three digits is a year, a salary, or a typo — never a digest line."""
    assert replies.parse("2026 was a quiet year").index is None
    assert replies.parse("99").index == 99


def test_a_stray_message_never_starts_a_build() -> None:
    """The chat is a chat. Anything without a keyword or an index is UNKNOWN
    and the handler asks — a 40-minute run must not be one typo away."""
    for text in ("thanks", "what about the Berlin one", "👍", "maybe"):
        parsed = replies.parse(text)
        assert parsed.action == replies.UNKNOWN
        assert not parsed.is_actionable
        assert parsed.note == text


def test_an_empty_message_is_not_an_answer() -> None:
    assert replies.parse("").action == replies.UNKNOWN
    assert replies.parse("   ").action == replies.UNKNOWN
    assert replies.parse(None).action == replies.UNKNOWN


def test_a_keyword_buried_mid_sentence_is_not_an_approval() -> None:
    """Only the first word decides. "I said no to the last one" would
    otherwise be read as a skip of this one."""
    assert replies.parse("I said no to the last one").action == replies.UNKNOWN
    assert replies.parse("the salary is ok").action == replies.UNKNOWN


def test_only_the_three_real_actions_are_actionable() -> None:
    assert replies.Reply(replies.APPROVE).is_actionable
    assert replies.Reply(replies.SKIP).is_actionable
    assert replies.Reply(replies.SNOOZE).is_actionable
    assert not replies.Reply(replies.UNKNOWN).is_actionable
