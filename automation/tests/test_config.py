"""The three config inputs, their precedence, and their failure modes.

Almost nothing here is interesting when it works. What makes it worth pinning
down is that this module is the watcher's only defence against being taken off
the air by a text file: it runs for weeks unattended, both TOML files are edited
live, and every edit passes through a moment where the file on disk is
syntactically broken.

So the tests are mostly about the bad paths — a half-saved file, a typo in an
environment variable, a topic id that is not a number, a knob written from
Telegram while the same knob is pinned in the environment. In each case the
watcher is supposed to keep running on the last thing that worked and say so.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
import types

import pytest

from watcher import config
from watcher.config import (
    Config, ExperienceFilter, Filters, LocationFilter, SeniorityFilter,
    SourceDefaults, Sources,
)


@pytest.fixture()
def clean_env(monkeypatch):
    """Undo anything the module writes into `os.environ`.

    `load_env` uses `setdefault`, which monkeypatch cannot roll back on its own.
    """
    saved = dict(os.environ)
    yield monkeypatch
    os.environ.clear()
    os.environ.update(saved)


# ===========================================================================
# Numeric knobs
# ===========================================================================

DEFAULTS = [
    ("interval_minutes", 30), ("max_age_days", 14), ("http_timeout", 30),
    ("failures_before_disable", 3), ("fragile_failures_before_disable", 6),
    ("retry_after_minutes", 60), ("retry_backoff_factor", 2.0),
    ("retry_backoff_max_minutes", 1440),
    ("match_model", "haiku"), ("batch_size", 8), ("notify_threshold", 70),
    ("digest_threshold", 40), ("description_chars", 4000),
    ("match_timeout", 180), ("max_score_attempts", 3),
    ("digest_hour", 19), ("heartbeat_hour", 9), ("snooze_days", 7),
    ("digest_at", (19, 5)), ("heartbeat_at", (9, 15)),
    ("build_enabled", False), ("claude_bin", "claude"), ("build_model", "sonnet"),
    ("build_timeout_minutes", 45), ("build_retries", 1),
    ("build_retry_delay_seconds", 120), ("build_progress_updates", True),
    ("build_progress_refresh_seconds", 30),
    ("build_progress_min_interval_seconds", 5),
    ("duplicate_title_ratio", 0.8), ("duplicate_lookback_days", 365),
    ("kb_enabled", True), ("kb_model", "sonnet"), ("kb_weekday", 6),
    ("kb_hour", 18), ("kb_at", (18, 25)), ("kb_min_decisions", 5),
    ("kb_lookback", 60), ("kb_timeout", 180),
    ("topics", {}), ("topics_enabled", False),
]


@pytest.mark.parametrize("name,expected", DEFAULTS, ids=[n for n, _ in DEFAULTS])
def test_an_empty_config_still_answers_every_question(name, expected) -> None:
    """A config.toml with a section deleted must not crash the watcher."""
    assert getattr(Config(), name) == expected


@pytest.mark.parametrize("section,key,attr,written,expected", [
    ("poll", "interval_minutes", "interval_minutes", 15, 15),
    ("match", "notify_threshold", "notify_threshold", 55, 55),
    ("build", "timeout_minutes", "build_timeout_minutes", 90, 90),
    ("kb", "hour", "kb_hour", 7, 7),
])
def test_the_file_beats_the_default(section, key, attr, written, expected) -> None:
    assert getattr(Config(**{section: {key: written}}), attr) == expected


def test_the_environment_beats_the_file(clean_env) -> None:
    clean_env.setenv("WATCHER_MATCH_NOTIFY_THRESHOLD", "55")
    assert Config(match={"notify_threshold": 70}).notify_threshold == 55


def test_a_blank_environment_variable_is_not_an_override(clean_env) -> None:
    clean_env.setenv("WATCHER_MATCH_NOTIFY_THRESHOLD", "   ")
    assert Config(match={"notify_threshold": 70}).notify_threshold == 70


def test_a_malformed_override_warns_and_falls_through(clean_env, caplog) -> None:
    """A typo in an env var must not stop an always-on watcher from starting."""
    clean_env.setenv("WATCHER_MATCH_NOTIFY_THRESHOLD", "seventy")
    with caplog.at_level(logging.WARNING, logger="watcher.config"):
        assert Config(match={"notify_threshold": 70}).notify_threshold == 70
    assert "WATCHER_MATCH_NOTIFY_THRESHOLD" in caplog.text


def test_the_override_name_is_mechanical() -> None:
    assert config.env_name("match", "notify_threshold") == "WATCHER_MATCH_NOTIFY_THRESHOLD"
    assert config.env_name("kb", "hour") == "WATCHER_KB_HOUR"


def test_the_knobs_with_a_floor_cannot_be_configured_below_it() -> None:
    assert Config(build={"retries": -3}).build_retries == 0
    assert Config(build={"retry_delay_seconds": -1}).build_retry_delay_seconds == 0
    assert Config(build={"progress_refresh_seconds": 0}
                  ).build_progress_refresh_seconds == 1
    assert Config(poll={"retry_backoff_factor": 0.5}).retry_backoff_factor == 1.0


def test_a_fragile_source_gets_the_longer_allowance() -> None:
    """Browser scraping fails for a bad afternoon; a JSON API failing means more."""
    cfg = Config(poll={"failures_before_disable": 3,
                       "fragile_failures_before_disable": 6})
    assert cfg.failures_allowed({"name": "stepstone", "fragile": True}) == 6
    assert cfg.failures_allowed({"company": "Example"}) == 3
    assert cfg.failures_allowed({"fragile": False}) == 3


def test_the_daily_jobs_are_staggered_off_the_hour() -> None:
    """Three jobs on one tick means two waiting on a sweep of 4000 postings."""
    cfg = Config(notify={"digest_hour": 19, "heartbeat_hour": 9},
                 kb={"hour": 18})
    minutes = {cfg.digest_at[1], cfg.heartbeat_at[1], cfg.kb_at[1]}
    assert len(minutes) == 3
    assert 0 not in minutes


def test_clock_formats_a_pair_for_display() -> None:
    assert config.clock((9, 5)) == "09:05"
    assert config.clock((19, 15)) == "19:15"


# ===========================================================================
# Forum topics
# ===========================================================================

def test_configured_topics_are_read_by_kind() -> None:
    cfg = Config(notify={"topics": {"new_posting": 12, "completed_build": 34}})
    assert cfg.topics == {"new_posting": 12, "completed_build": 34}
    assert cfg.topic_for("new_posting") == 12
    assert cfg.topics_enabled is True


@pytest.mark.parametrize("kind", [None, "", "heartbeat", "targeted_build"])
def test_an_unrouted_kind_goes_to_general(kind) -> None:
    """None is the answer for unconfigured, unknown, and no-topics-at-all —
    which is what keeps a chat without forum topics behaving as it always did."""
    cfg = Config(notify={"topics": {"new_posting": 12}})
    assert cfg.topic_for(kind) is None


def test_a_zero_or_blank_topic_is_left_out_rather_than_stored() -> None:
    cfg = Config(notify={"topics": {"new_posting": 0, "failed_build": ""}})
    assert cfg.topics == {}
    assert cfg.topics_enabled is False


def test_a_junk_topic_id_costs_that_topic_and_nothing_else(caplog) -> None:
    cfg = Config(notify={"topics": {"new_posting": "General",
                                    "failed_build": 34}})
    with caplog.at_level(logging.WARNING, logger="watcher.config"):
        assert cfg.topics == {"failed_build": 34}
    assert "new_posting" in caplog.text


def test_a_topic_id_can_be_overridden_from_the_environment(clean_env) -> None:
    clean_env.setenv("WATCHER_NOTIFY_TOPICS_NEW_POSTING", "99")
    assert Config(notify={"topics": {"new_posting": 12}}).topics["new_posting"] == 99


def test_a_missing_topics_section_is_the_chat_id_only_case() -> None:
    assert Config(notify={"topics": None}).topics == {}
    assert Config().topics == {}


# ===========================================================================
# .env
# ===========================================================================

def test_env_file_is_parsed_and_real_variables_win(tmp_path, clean_env) -> None:
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "TELEGRAM_BOT_TOKEN=\"from-file\"\n"
        "ALREADY_SET='ignored'\n"
        "not a setting\n"
        "  SPACED  =  value  \n",
        encoding="utf-8")
    clean_env.setattr(config, "ENV_PATH", tmp_path / ".env")
    clean_env.setenv("ALREADY_SET", "from-shell")

    config.load_env()

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-file"
    assert os.environ["ALREADY_SET"] == "from-shell"
    assert os.environ["SPACED"] == "value"


def test_a_missing_env_file_is_not_an_error(tmp_path, clean_env) -> None:
    clean_env.setattr(config, "ENV_PATH", tmp_path / "nope.env")
    config.load_env()


def test_require_env_names_the_file_to_fix(tmp_path, clean_env) -> None:
    clean_env.setattr(config, "ENV_PATH", tmp_path / "nope.env")
    clean_env.setenv("PRESENT", "yes")
    assert config.require_env("PRESENT") == "yes"

    clean_env.delenv("ABSENT", raising=False)
    with pytest.raises(RuntimeError, match="ABSENT"):
        config.require_env("ABSENT")


# ===========================================================================
# Paths
# ===========================================================================

def test_the_browser_profile_can_be_pointed_elsewhere(tmp_path, clean_env) -> None:
    """Chromium locks a profile to one process, so the re-hydration sweep needs
    its own copy to run at all while the watcher holds the real one."""
    clean_env.delenv(config.BROWSER_PROFILE_ENV, raising=False)
    assert config.browser_profile_dir() == config.BROWSER_PROFILE_DIR

    clean_env.setenv(config.BROWSER_PROFILE_ENV, str(tmp_path / "seeded"))
    assert config.browser_profile_dir() == tmp_path / "seeded"

    clean_env.setenv(config.BROWSER_PROFILE_ENV, "   ")
    assert config.browser_profile_dir() == config.BROWSER_PROFILE_DIR


def test_ensure_dirs_is_safe_to_call_twice(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BUILD_LOG_DIR", tmp_path / "logs" / "builds")
    config.ensure_dirs()
    config.ensure_dirs()
    assert (tmp_path / "logs" / "builds").is_dir()


def test_stamp_reports_nothing_for_a_file_that_is_not_there(tmp_path) -> None:
    assert config._stamp(tmp_path / "nope.toml") is None
    present = tmp_path / "there.toml"
    present.write_text("", encoding="utf-8")
    assert config._stamp(present) is not None


def test_reading_a_missing_toml_says_which_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="nope.toml"):
        config._read_toml(tmp_path / "nope.toml")


# ===========================================================================
# Hot reload
# ===========================================================================

def reloader(path, parse=lambda raw: raw.get("value", 0)):
    return config._Reloader(path, parse, path.name)


def test_the_first_load_of_a_broken_file_has_to_raise(tmp_path) -> None:
    """There is no last good value to fall back to yet."""
    path = tmp_path / "c.toml"
    path.write_text("this is not [ toml", encoding="utf-8")
    with pytest.raises(Exception):
        reloader(path)()

    missing = reloader(tmp_path / "gone.toml")
    with pytest.raises(FileNotFoundError):
        missing()


def test_an_unchanged_file_is_parsed_once(tmp_path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("value = 1", encoding="utf-8")
    calls = []

    load = reloader(path, lambda raw: calls.append(1) or raw["value"])
    assert load() == 1
    assert load() == 1
    assert len(calls) == 1


def test_an_edit_takes_effect_without_a_restart(tmp_path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("value = 1", encoding="utf-8")
    load = reloader(path)
    assert load() == 1

    path.write_text("value = 2", encoding="utf-8")
    os.utime(path, (0, 0))          # force a distinct mtime, not a clock race
    assert load() == 2


def test_a_half_saved_file_serves_the_last_good_version(tmp_path, caplog) -> None:
    """An editor mid-save must not take the watcher down with it."""
    path = tmp_path / "c.toml"
    path.write_text("value = 1", encoding="utf-8")
    load = reloader(path)
    assert load() == 1

    path.write_text("value = [", encoding="utf-8")
    os.utime(path, (0, 0))
    with caplog.at_level(logging.ERROR, logger="watcher.config"):
        assert load() == 1
        assert load() == 1               # still complaining, still serving
    assert caplog.text.count("unreadable") == 1, "should complain once, not per access"

    # The bad stamp is never adopted, so the fix is picked up immediately.
    path.write_text("value = 3", encoding="utf-8")
    os.utime(path, (1, 1))
    with caplog.at_level(logging.INFO, logger="watcher.config"):
        assert load() == 3
    assert "reloaded cleanly" in caplog.text


def test_a_file_deleted_under_the_watcher_keeps_the_last_good_version(tmp_path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("value = 1", encoding="utf-8")
    load = reloader(path)
    assert load() == 1

    path.unlink()
    assert load() == 1


def test_invalidate_forces_a_re_read_the_mtime_would_have_hidden(tmp_path) -> None:
    """For the one case mtime cannot cover: this process wrote the file itself."""
    path = tmp_path / "c.toml"
    path.write_text("value = 1", encoding="utf-8")
    load = reloader(path)
    assert load() == 1

    stamp = path.stat().st_mtime
    path.write_text("value = 2", encoding="utf-8")
    os.utime(path, (stamp, stamp))       # same mtime, different content
    assert load() == 1

    load.invalidate()
    assert load() == 2


def test_the_shipped_config_files_parse(caplog) -> None:
    """A guard on the two files the watcher will not start without.

    `sources.toml` names the companies the owner is watching, so it is
    git-ignored and a fresh checkout has only the example. Both are worth
    parsing — the example is what a new install copies, and a typo in it breaks
    that install's first poll — but only the real file can be asked whether
    anything is still switched on. The example ships with every source
    commented out on purpose.
    """
    real = config.SOURCES_PATH
    path = real if real.exists() else real.parent / "sources.toml.example"

    with caplog.at_level(logging.ERROR, logger="watcher.config"):
        cfg = config.load_config()
        sources = reloader(path, config._parse_sources)()

    assert isinstance(cfg, Config)
    assert isinstance(sources, Sources)
    if path == real:
        assert sources.all_enabled(), "every source in sources.toml is off"
    assert "unreadable" not in caplog.text


# ===========================================================================
# set_number
# ===========================================================================

SAMPLE = """\
# Behaviour knobs.
[match]
# The paragraph above a setting is its calibration record.
notify_threshold = 70
digest_threshold = 40

[kb]
hour = 18
"""


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def test_setting_a_knob_rewrites_one_line_and_keeps_the_comments(
        config_file, clean_env) -> None:
    clean_env.delenv("WATCHER_MATCH_NOTIFY_THRESHOLD", raising=False)
    assert config.set_number("match", "notify_threshold", 60) == 70

    text = config_file.read_text(encoding="utf-8")
    assert "notify_threshold = 60" in text
    assert "calibration record" in text, "a TOML round-trip would have dropped this"
    assert "digest_threshold = 40" in text


def test_setting_a_knob_to_what_it_already_is_writes_nothing(
        config_file, clean_env) -> None:
    clean_env.delenv("WATCHER_MATCH_NOTIFY_THRESHOLD", raising=False)
    before = config_file.stat().st_mtime_ns
    assert config.set_number("match", "notify_threshold", 70) == 70
    assert config_file.stat().st_mtime_ns == before


def test_a_key_is_only_matched_inside_its_own_section(config_file, clean_env) -> None:
    """`[kb] hour` and a `[notify] hour` are different settings with one name."""
    clean_env.delenv("WATCHER_KB_HOUR", raising=False)
    assert config.set_number("kb", "hour", 7) == 18
    assert "hour = 7" in config_file.read_text(encoding="utf-8")

    with pytest.raises(config.ConfigWriteError, match="notify"):
        config.set_number("notify", "hour", 7)


def test_an_unknown_key_is_refused_rather_than_appended(config_file) -> None:
    with pytest.raises(config.ConfigWriteError, match="renamed or removed"):
        config.set_number("match", "no_such_knob", 1)


def test_an_edit_the_environment_would_shadow_is_refused(config_file, clean_env) -> None:
    """It would appear to work while changing nothing at all."""
    clean_env.setenv("WATCHER_MATCH_NOTIFY_THRESHOLD", "55")
    with pytest.raises(config.ConfigWriteError, match="overrides config.toml"):
        config.set_number("match", "notify_threshold", 60)
    assert "notify_threshold = 70" in config_file.read_text(encoding="utf-8")


def test_an_unreadable_config_file_is_reported_not_raised_raw(
        tmp_path, monkeypatch, clean_env) -> None:
    clean_env.delenv("WATCHER_MATCH_NOTIFY_THRESHOLD", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "nope.toml")
    with pytest.raises(config.ConfigWriteError, match="could not read"):
        config.set_number("match", "notify_threshold", 60)


def test_a_failed_write_leaves_no_temporary_file_behind(
        config_file, monkeypatch, clean_env) -> None:
    """The rename is atomic so a crash cannot truncate the watcher's own config."""
    clean_env.delenv("WATCHER_MATCH_NOTIFY_THRESHOLD", raising=False)
    monkeypatch.setattr(config.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(config.ConfigWriteError, match="could not write"):
        config.set_number("match", "notify_threshold", 60)

    assert "notify_threshold = 70" in config_file.read_text(encoding="utf-8")
    assert not list(config_file.parent.glob("*.tmp"))


# ===========================================================================
# sources.toml
# ===========================================================================

def parse_sources(raw: dict) -> Sources:
    return config._parse_sources(raw)


def test_a_source_list_is_split_by_kind_and_tagged() -> None:
    sources = parse_sources({
        "ats": [{"company": "Example", "provider": "greenhouse", "token": "ex"},
                {"company": "Off", "provider": "lever", "enabled": False}],
        "portal": [{"name": "stepstone"},
                   {"name": "disabled", "enabled": False}],
    })
    assert [e["company"] for e in sources.enabled_ats()] == ["Example"]
    assert [e["name"] for e in sources.enabled_portals()] == ["stepstone"]
    assert [e["kind"] for e in sources.all_enabled()] == ["ats", "portal"]


def test_a_source_is_enabled_unless_it_says_otherwise() -> None:
    sources = parse_sources({"ats": [{"company": "Example"}], "portal": []})
    assert len(sources.enabled_ats()) == 1


@pytest.mark.parametrize("entry,expected", [
    ({"kind": "ats", "company": "Example"}, "ats:Example"),
    ({"provider": "greenhouse", "company": "Example"}, "ats:Example"),
    ({"provider": "lever", "token": "spotify"}, "ats:spotify"),
    ({"kind": "portal", "name": "stepstone"}, "portal:stepstone"),
    ({"name": "hiringcafe"}, "portal:hiringcafe"),
    ({}, "portal:?"),
    ({"kind": "ats"}, "ats:?"),
])
def test_every_source_gets_a_stable_key(entry, expected) -> None:
    """It is what health tracking and `--source` both address a source by."""
    assert config.source_key(entry) == expected


def test_title_deny_is_lowered_so_the_file_need_not_be() -> None:
    sources = parse_sources({"defaults": {"title_deny": ["Sales"]}})
    assert sources.defaults.title_deny == ("sales",)


def test_a_leftover_title_allow_key_is_ignored_not_rejected() -> None:
    """An un-migrated sources.toml must keep loading after the allow-list
    was retired — the key going quiet must not stop the watcher."""
    sources = parse_sources({"defaults": {
        "title_allow": ["Data Scientist"], "title_deny": ["Sales"],
    }})
    assert sources.defaults.title_deny == ("sales",)
    assert not hasattr(sources.defaults, "title_allow")


def test_the_legacy_countries_setting_is_still_honoured() -> None:
    """An un-migrated sources.toml must keep behaving exactly as it did.

    Folding it in rather than replacing it is what stops an upgrade from
    silently widening where jobs may come from.
    """
    sources = parse_sources({"defaults": {"countries": ["DE", "AT"]}})
    assert sources.filters.location.countries == ("DE", "AT")
    assert sources.filters.location.allowed == frozenset({"DE", "AT"})


def test_the_filters_section_wins_over_the_legacy_setting() -> None:
    sources = parse_sources({
        "defaults": {"countries": ["DE"]},
        "filters": {"location": {"countries": ["NL"]}},
    })
    assert sources.filters.location.countries == ("NL",)


def test_a_setting_written_as_one_string_is_read_as_a_list() -> None:
    sources = parse_sources({"filters": {"location": {"regions": "DACH"}}})
    assert sources.filters.location.regions == ("DACH",)
    assert sources.filters.location.allowed == frozenset({"DE", "AT", "CH"})


def test_blank_entries_in_a_list_are_dropped() -> None:
    assert config._strs({"k": ["DE", "  ", "", "AT"]}, "k") == ("DE", "AT")
    assert config._strs({}, "k") == ()


def test_seniority_bands_are_folded_so_the_file_can_be_written_naturally() -> None:
    sources = parse_sources({"filters": {"seniority": {
        "allow": ["Mid", "SENIOR"], "deny": ["Intern"]}}})
    assert sources.filters.seniority.allow == ("mid", "senior")
    assert sources.filters.seniority.deny == ("intern",)


def test_experience_filtering_needs_both_the_mode_and_a_cap() -> None:
    """Annotate-only by default: read the number, show it, never act on it."""
    assert not ExperienceFilter().filtering
    assert not ExperienceFilter(mode="filter", max_years=0).filtering
    assert not ExperienceFilter(mode="annotate", max_years=8).filtering
    assert ExperienceFilter(mode="Filter ", max_years=8).filtering


def test_an_experience_cap_of_none_reads_as_no_cap() -> None:
    sources = parse_sources({"filters": {"experience": {"max_years": None}}})
    assert sources.filters.experience.max_years == 0
    assert not sources.filters.experience.filtering


def test_a_sources_file_with_nothing_in_it_still_builds() -> None:
    sources = parse_sources({})
    assert sources.all_enabled() == []
    assert sources.filters.location.allowed == frozenset()
    assert sources.defaults == SourceDefaults((), ())


def test_the_geographic_lists_are_additive_and_interchangeable() -> None:
    """`regions = ["DACH"]` and `countries = ["DE","AT","CH"]` mean the same."""
    by_region = LocationFilter(regions=("DACH",))
    by_country = LocationFilter(countries=("de", "AT", "ch"))
    assert by_region.allowed == by_country.allowed

    mixed = LocationFilter(continents=("Europe",), countries=("US",))
    assert {"DE", "US"} <= mixed.allowed


def test_the_city_lists_resolve_to_canonical_keys() -> None:
    loc = LocationFilter(cities=("München", "Berlin"), exclude_cities=("Bonn",))
    assert loc.allowed_cities == LocationFilter(
        cities=("Munich", "berlin")).allowed_cities
    assert loc.excluded_cities == frozenset({"bonn"})


def test_the_default_filters_restrict_nothing() -> None:
    f = Filters()
    assert f.location.allowed == frozenset()
    assert f.location.remote_ok is True
    assert f.location.remote_anywhere is False
    assert f.seniority == SeniorityFilter()
    assert not f.experience.filtering


# ===========================================================================
# build settings
# ===========================================================================

def test_build_settings_are_re_rendered_from_the_template(monkeypatch) -> None:
    """Called before every headless build, so an edited template applies at once."""
    seen = {}
    stub = types.ModuleType("build_settings")

    def record(root, path):
        seen["args"] = (root, path)
        return "written"

    stub.sync = record
    monkeypatch.setitem(sys.modules, "build_settings", stub)

    assert config.sync_build_settings() == "written"
    assert seen["args"] == (config.REPO_ROOT, config.BUILD_SETTINGS_PATH)


def test_a_template_that_would_deny_the_build_its_own_files_raises(monkeypatch) -> None:
    stub = types.ModuleType("build_settings")

    def refuse(root, path):
        raise RuntimeError("would deny write access to the workspace")

    stub.sync = refuse
    monkeypatch.setitem(sys.modules, "build_settings", stub)

    with pytest.raises(RuntimeError, match="deny write access"):
        config.sync_build_settings()


# ===========================================================================
# [triage] — a batch has to be able to finish
# ===========================================================================
#
# `batch_size = 200` with `timeout_seconds = 120` shipped and ran for a day.
# Every batch timed out, and because triage fails open every posting in a
# timed-out batch degrades to `unsure` — which keeps it. So the gate was
# inert, the funnel was wide open, and nothing anywhere said so: a broken
# fail-open gate and a working one produce the same postings. The only
# evidence was two ERROR lines in a log nobody reads at 01:15 in the morning.
#
# The invariant is not "these two happen to be the numbers we chose". It is
# that the batch size and the ceiling have to be consistent with how long a
# batch actually takes, which is why the measurement is written down here
# instead of living in a commit message.

#: Seconds per posting on a typical call, from timing real batches of live
#: postings through `score_batch`: 10 items/20.1s, 25/91.5s, 50/128.4s,
#: 100/188.9s. Cost is close to linear in the item count with almost no fixed
#: overhead — a batch of 200 needs about six minutes, which is why no ceiling
#: worth setting would have saved the old size.
MEASURED_SECONDS_PER_POSTING = 1.9

#: The 25-item call above took 91.5s where that rate predicts 49s — an 87%
#: overshoot on an identical prompt. Latency varies that much between calls,
#: so a ceiling only just above the typical duration loses whole batches to
#: ordinary noise. Doubling covers what was actually observed.
SLOW_CALL_ALLOWANCE = 2.0


def _shipped_config():
    """The real config.toml, not a default-constructed Config.

    Production runs on the file. A test that only checked `Config()` would
    have passed happily throughout the outage described above, because the
    broken values were in the file.
    """
    return config._parse_config(
        tomllib.loads(config.CONFIG_PATH.read_text(encoding="utf-8")))


def _sized(label: str):
    """Both places a batch size can come from: the file, and the fallback.

    They have to hold the invariant separately — the file is what production
    reads, and the default is what a config.toml with the section deleted
    falls back to.
    """
    return config.Config() if label == "defaults" else _shipped_config()


@pytest.mark.parametrize("label", ["defaults", "config.toml"])
def test_a_triage_batch_finishes_inside_its_timeout_even_on_a_slow_call(
        label) -> None:
    cfg = _sized(label)
    typical = cfg.triage_batch_size * MEASURED_SECONDS_PER_POSTING
    assert typical * SLOW_CALL_ALLOWANCE <= cfg.triage_timeout, (
        f"{label}: a batch of {cfg.triage_batch_size} takes about "
        f"{typical:.0f}s and can take twice that, against a ceiling of "
        f"{cfg.triage_timeout}s. Batches that overrun degrade to unsure, and "
        f"triage fails open, so the gate stops gating without failing"
    )


def test_a_full_cycle_of_triage_fits_inside_the_poll_interval() -> None:
    """`max_per_cycle` is a ceiling; the cycle still has to finish.

    Batches run one after another, so the cap and the interval are coupled:
    a cap that cannot be triaged within one interval means the scheduler is
    still in the previous cycle when the next is due.
    """
    cfg = _shipped_config()
    projected = cfg.triage_max_per_cycle * MEASURED_SECONDS_PER_POSTING
    assert projected <= cfg.interval_minutes * 60, (
        f"triaging {cfg.triage_max_per_cycle} postings projects to "
        f"{projected / 60:.0f} minutes, but a poll cycle comes round every "
        f"{cfg.interval_minutes}"
    )
