"""One poll: which sources get asked, what survives, and what a failure costs.

`test_source_health.py` covers the state machine — how many failures disable a
source, how the cooldown grows, what parks one. This covers the half that
consumes it: a cycle deciding to skip a source, to probe it once, to escalate
it, or to say it came back. Those decisions are invisible in normal operation
and only surface weeks later, as a source that never returned or one that
hammered a dead endpoint every twenty minutes.

The rule underneath all of it is that **a poll never ends because of one
source**. Eighteen boards are asked in sequence inside a single transaction; an
exception that escapes costs every source after it in the list, and the failure
that would do it is always the boring one — a 500, a redirect to a login page, a
JSON body that became HTML overnight.

Two smaller invariants get their own tests because both have already gone wrong
here. Health writes are committed per source, not at the end: a poll spends
minutes in browser I/O between boards, and an uncommitted write transaction
across that window is what made a mid-cycle scoring write die on "database is
locked". And a description shorter than `TEASER_CHARS` is re-fetched before it
is judged — the guard used to test for an empty string, which is true of no
portal in the rotation, so the two largest sources were prefiltered and scored
on the opening sentence of the ad.
"""

from __future__ import annotations

import datetime as dt

import pytest

from watcher import poll as poll_module
from watcher import store
from watcher.config import Config, SourceDefaults, Sources
from watcher.fetchers import SourceNotImplemented
from watcher.fetchers.base import StructuralError
from watcher.normalize import Posting
from watcher.poll import TEASER_CHARS, PollReport, SourceCounts, poll_once

BODY = "Python, SQL and PyTorch. Remote-friendly, permanent. " * 30
TEASER = "We are hiring a data scientist in Cologne. "        # ~43 chars


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    store.init_db(path)
    return path


def posting(source: str, job_id: str, *, title: str = "Data Scientist",
            company: str | None = None, description: str = BODY,
            detail_url: str = "") -> Posting:
    return Posting(source=source, provider="greenhouse", source_job_id=job_id,
                   url=f"https://example.test/{source}/{job_id}",
                   company=company or f"Acme {source}",
                   title=f"{title} {job_id}", location="Köln", country="Germany",
                   description=description, detail_url=detail_url)


def sources(*names: str, fragile: bool = False, portals=()) -> Sources:
    defaults = SourceDefaults(countries=("DE",),
                              title_allow=("data scientist",), title_deny=())
    return Sources(defaults=defaults,
                   ats=tuple({"company": n, "provider": "greenhouse",
                              "token": n.lower(),
                              **({"fragile": True} if fragile else {})}
                             for n in names),
                   portals=tuple(portals))


@pytest.fixture()
def boards(monkeypatch):
    """What each source key returns — a list, or an exception to raise."""
    catalogue: dict[str, list[Posting] | Exception] = {}

    def fetch(entry, timeout):
        key = f"ats:{entry['company']}" if "company" in entry \
            else f"portal:{entry['name']}"
        found = catalogue.get(key, [])
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(poll_module, "fetch_source", fetch)
    return catalogue


# --------------------------------------------------------------------------
# the report's own arithmetic
# --------------------------------------------------------------------------

def test_the_summary_names_only_what_happened() -> None:
    """It goes in a log line per cycle. `errors 0, pending 0` on every one of
    seventy-two daily cycles is noise that hides the one that says otherwise."""
    plain = PollReport(fetched=300, already_known=290, filtered=8, stored=2)
    assert plain.summary() == "fetched 300, known 290, filtered 8, new 2"
    assert "errors" not in plain.summary()

    troubled = PollReport(fetched=1, errors={"ats:X": "boom"},
                          pending=["portal:Y: not implemented"])
    assert "errors 1" in troubled.summary()
    assert "pending 1" in troubled.summary()


def test_a_sources_error_rides_along_with_its_counts() -> None:
    counts = SourceCounts(error="HTTPError: 503")
    assert counts.as_dict()["error"] == "HTTPError: 503"
    assert "error" not in SourceCounts(fetched=3).as_dict()


def test_counters_are_created_on_first_mention() -> None:
    report = PollReport()
    report.source("ats:New").fetched += 1
    assert report.by_source["ats:New"].fetched == 1


# --------------------------------------------------------------------------
# which sources get asked
# --------------------------------------------------------------------------

def test_one_source_can_be_polled_alone(db, boards) -> None:
    """`--source ats:Bayer`, for checking a fetcher without a full cycle."""
    boards["ats:Alpha"] = [posting("ats:Alpha", "1")]
    boards["ats:Beta"] = [posting("ats:Beta", "1")]

    report = poll_once(only="ats:Alpha", config=Config(),
                       sources=sources("Alpha", "Beta"))
    assert set(report.by_source) == {"ats:Alpha"}


def test_a_disabled_source_is_skipped_while_its_cooldown_runs(
        db, boards, caplog) -> None:
    boards["ats:Broken"] = [posting("ats:Broken", "1")]
    with store.connect(db) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "ats:Broken", "boom", 3)
        conn.commit()

    report = poll_once(config=Config(poll={"retry_after_minutes": 600}),
                       sources=sources("Broken"))
    assert "ats:Broken" not in report.by_source
    assert report.recovered == []


def test_a_disabled_source_gets_exactly_one_probe_once_the_cooldown_elapses(
        db, boards) -> None:
    boards["ats:Broken"] = [posting("ats:Broken", "1")]
    with store.connect(db) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "ats:Broken", "boom", 3)
        _age(conn, "ats:Broken", hours=3)
        conn.commit()

    report = poll_once(config=Config(poll={"retry_after_minutes": 60}),
                       sources=sources("Broken"))
    assert report.recovered == ["ats:Broken"]
    assert report.by_source["ats:Broken"].fetched == 1
    with store.connect(db) as conn:
        assert not store.is_source_disabled(conn, "ats:Broken")


def test_a_probe_that_fails_widens_the_cooldown_and_stays_quiet(
        db, boards) -> None:
    """The one notification went out when the source first switched off. A
    second every hour for a week is how a person learns to ignore them."""
    boards["ats:Broken"] = RuntimeError("still down")
    with store.connect(db) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "ats:Broken", "boom", 3)
        _age(conn, "ats:Broken", hours=3)
        conn.commit()

    report = poll_once(config=Config(poll={"retry_after_minutes": 60}),
                       sources=sources("Broken"))
    assert report.newly_disabled == [], "it was already off"
    assert report.recovered == []
    with store.connect(db) as conn:
        assert store.retry_attempts(conn, "ats:Broken") >= 1


def test_a_dry_run_asks_even_the_disabled_ones(db, boards) -> None:
    """It writes nothing, so the cooldown is not what it is for — someone
    typing `--dry-run` is checking whether the fetcher works now."""
    boards["ats:Broken"] = [posting("ats:Broken", "1")]
    with store.connect(db) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "ats:Broken", "boom", 3)
        conn.commit()

    report = poll_once(dry_run=True, config=Config(poll={"retry_after_minutes": 0}),
                       sources=sources("Broken"))
    assert report.by_source["ats:Broken"].fetched == 1


def _age(conn, source: str, *, hours: int) -> None:
    """Backdate a source's last failure so its cooldown has elapsed."""
    stamp = (dt.datetime.now() - dt.timedelta(hours=hours)).isoformat(
        timespec="seconds")
    conn.execute("UPDATE source_health SET disabled_at = ? WHERE source = ?",
                 (stamp, source))


# --------------------------------------------------------------------------
# what a failure costs
# --------------------------------------------------------------------------

def test_one_broken_source_does_not_end_the_poll(db, boards) -> None:
    """Eighteen boards in one transaction. An exception that escapes costs
    every source after it in the list."""
    boards["ats:Alpha"] = RuntimeError("500 from the board")
    boards["ats:Beta"] = [posting("ats:Beta", "1")]

    report = poll_once(config=Config(), sources=sources("Alpha", "Beta"))
    assert report.stored == 1
    assert "ats:Alpha" in report.errors


def test_the_threshold_failure_is_reported_as_newly_disabled(db, boards) -> None:
    boards["ats:Broken"] = RuntimeError("timeout")
    config = Config(poll={"failures_before_disable": 2})

    first = poll_once(config=config, sources=sources("Broken"))
    assert first.newly_disabled == []
    second = poll_once(config=config, sources=sources("Broken"))
    assert second.newly_disabled == ["ats:Broken"]


def test_a_fragile_source_is_allowed_more_bad_afternoons(db, boards) -> None:
    """Browser-driven scraping against pages with anti-bot checks. Holding it
    to a JSON API's three strikes is what makes a scraper switch itself off
    over one bad afternoon."""
    boards["ats:Flaky"] = RuntimeError("challenge page")
    config = Config(poll={"failures_before_disable": 2,
                          "fragile_failures_before_disable": 5})

    for _ in range(4):
        report = poll_once(config=config, sources=sources("Flaky", fragile=True))
        assert report.newly_disabled == []
    assert poll_once(config=config,
                     sources=sources("Flaky", fragile=True)
                     ).newly_disabled == ["ats:Flaky"]


def test_a_structural_failure_parks_with_the_error_in_the_alert(db,
                                                                boards) -> None:
    """Probing reproduces a 404 exactly, so there is nothing to retry. The
    escalation carries the error because someone has to read it and act."""
    boards["ats:Moved"] = StructuralError("HTTP 404 for /v1/boards/acme/jobs")
    config = Config(poll={"failures_before_disable": 1})

    report = poll_once(config=config, sources=sources("Moved"))
    assert report.newly_parked and report.newly_parked[0][0] == "ats:Moved"
    assert "404" in report.newly_parked[0][1]
    assert report.newly_disabled == [], "parked is not disabled"


def test_a_source_that_goes_structural_after_being_disabled_is_parked_now(
        db, boards) -> None:
    """Waiting for the next trip would mean waiting forever — a disabled
    source's failure count no longer climbs to a threshold."""
    with store.connect(db) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "ats:Moved", "timeout", 3)
        _age(conn, "ats:Moved", hours=99)
        conn.commit()

    boards["ats:Moved"] = StructuralError("HTTP 410 gone")
    report = poll_once(config=Config(poll={"retry_after_minutes": 1}),
                       sources=sources("Moved"))
    assert [key for key, _ in report.newly_parked] == ["ats:Moved"]
    with store.connect(db) as conn:
        assert store.is_source_parked(conn, "ats:Moved")


def test_a_source_with_no_fetcher_yet_is_pending_not_failing(db,
                                                              monkeypatch) -> None:
    """It has never worked, so counting it towards a disable threshold would
    file a to-do item as an outage."""
    def fetch(entry, timeout):
        raise SourceNotImplemented("no fetcher for provider 'taleo'")

    monkeypatch.setattr(poll_module, "fetch_source", fetch)
    report = poll_once(config=Config(), sources=sources("Legacy"))
    assert report.pending and "taleo" in report.pending[0]
    assert report.errors == {}
    with store.connect(db) as conn:
        assert not store.is_source_disabled(conn, "ats:Legacy")


def test_health_is_committed_per_source_not_at_the_end(db, boards,
                                                       monkeypatch) -> None:
    """A poll spends minutes in network and browser I/O between sources. An
    uncommitted write transaction across that window is what made a mid-cycle
    scoring write die on "database is locked"."""
    seen: list[bool] = []

    boards["ats:Alpha"] = [posting("ats:Alpha", "1")]

    def fetch(entry, timeout):
        # Read through a second connection, as the matcher would.
        with store.connect(db) as other:
            seen.append(bool(other.execute(
                "SELECT COUNT(*) FROM source_health").fetchone()[0]))
        return [posting("ats:Beta", "1")]

    original = poll_module.fetch_source
    order = {"n": 0}

    def dispatch(entry, timeout):
        order["n"] += 1
        return original(entry, timeout) if order["n"] == 1 else fetch(entry, timeout)

    monkeypatch.setattr(poll_module, "fetch_source", dispatch)
    poll_once(config=Config(), sources=sources("Alpha", "Beta"))
    assert seen == [True], "the first source's health row was already visible"


# --------------------------------------------------------------------------
# dedupe within a batch
# --------------------------------------------------------------------------

def test_two_sources_surfacing_one_role_in_a_single_poll_store_it_once(
        db, boards) -> None:
    """Neither is in the database yet, so the stored-ids check cannot see it."""
    same = dict(company="Acme GmbH", title="Data Scientist")
    boards["ats:Alpha"] = [posting("ats:Alpha", "1", **same)]
    boards["ats:Beta"] = [posting("ats:Beta", "1", **same)]

    report = poll_once(config=Config(), sources=sources("Alpha", "Beta"))
    assert report.stored == 1
    assert report.already_known == 1


def test_the_identical_listing_twice_from_one_source_stores_once(db,
                                                                 boards) -> None:
    listing = posting("ats:Alpha", "1")
    boards["ats:Alpha"] = [listing, listing]

    report = poll_once(config=Config(), sources=sources("Alpha"))
    assert report.stored == 1 and report.already_known == 1


# --------------------------------------------------------------------------
# hydration and the re-check
# --------------------------------------------------------------------------

@pytest.fixture()
def bodies(monkeypatch):
    """What the detail page hands back, keyed by the posting's job id."""
    pages: dict[str, str | Exception] = {}

    def hydrate(post, timeout):
        answer = pages.get(post.source_job_id, post.description)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(poll_module, "hydrate", hydrate)
    return pages


def test_a_teaser_is_refetched_before_it_is_judged(db, boards, bodies) -> None:
    """The guard used to test for an empty description, which is true of no
    portal in the rotation. stepstone and hiringcafe both ship the search
    tile's one-line snippet, so 198 of the first 225 postings were prefiltered
    and scored on the opening sentence of the ad."""
    boards["ats:Alpha"] = [posting("ats:Alpha", "1", description=TEASER,
                                   detail_url="https://example.test/detail/1")]
    bodies["1"] = BODY

    poll_once(config=Config(), sources=sources("Alpha"))
    with store.connect(db) as conn:
        stored = conn.execute("SELECT description FROM postings").fetchone()[0]
    assert len(stored) >= TEASER_CHARS


def test_a_full_body_is_not_refetched(db, boards, bodies) -> None:
    """A page load per posting, eighteen sources deep, for text already held."""
    boards["ats:Alpha"] = [posting("ats:Alpha", "1",
                                   detail_url="https://example.test/detail/1")]
    bodies["1"] = RuntimeError("must not be called")
    assert poll_once(config=Config(), sources=sources("Alpha")).stored == 1


def test_a_teaser_with_no_detail_url_is_kept_rather_than_dropped(
        db, boards, bodies) -> None:
    boards["ats:Alpha"] = [posting("ats:Alpha", "1", description=TEASER)]
    assert poll_once(config=Config(), sources=sources("Alpha")).stored == 1


def test_a_hydrate_that_raises_keeps_the_teaser_and_the_posting(
        db, boards, bodies, caplog) -> None:
    """A dead detail page is not a reason to lose the listing — the title and
    company still carry enough to judge it on."""
    boards["ats:Alpha"] = [posting("ats:Alpha", "1", description=TEASER,
                                   detail_url="https://example.test/detail/1")]
    bodies["1"] = TimeoutError("navigation timed out")

    report = poll_once(config=Config(), sources=sources("Alpha"))
    assert report.stored == 1
    assert "hydrate failed" in caplog.text


def test_the_body_is_rechecked_because_the_blockers_live_in_it(db, boards,
                                                                bodies) -> None:
    """The title says Data Scientist and passes; the ad says the role requires
    a security clearance. The re-check is the only place that is seen."""
    boards["ats:Alpha"] = [posting("ats:Alpha", "1", description=TEASER,
                                   detail_url="https://example.test/detail/1")]
    bodies["1"] = ("This position requires an active security clearance. " * 30)

    report = poll_once(config=Config(), sources=sources("Alpha"))
    assert report.stored == 0
    assert report.filtered == 1
    assert report.filtered_out[0][1] == "explicit hard blocker"


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------

@pytest.fixture()
def quiet(monkeypatch):
    """`main()` loads config and sources from disk; the tests supply both."""
    monkeypatch.setattr(poll_module, "setup", lambda *a: None)
    monkeypatch.setattr(poll_module, "load_sources", lambda: sources("Alpha"))
    monkeypatch.setattr(poll_module, "load_config", lambda: Config())


def test_the_cli_lists_what_is_new_with_its_link(db, boards, quiet,
                                                  capsys) -> None:
    boards["ats:Alpha"] = [posting("ats:Alpha", "1")]
    assert poll_module.main([]) == 0
    out = capsys.readouterr().out
    assert "NEW" in out and "Data Scientist" in out
    assert "https://example.test/" in out
    assert "new 1" in out


def test_the_cli_says_dry_run_so_a_transcript_is_not_misread(
        db, boards, quiet, capsys) -> None:
    boards["ats:Alpha"] = [posting("ats:Alpha", "1")]
    assert poll_module.main(["--dry-run"]) == 0
    assert "(dry run)" in capsys.readouterr().out


def test_the_cli_can_show_why_things_were_filtered(db, boards, quiet,
                                                    capsys) -> None:
    """The default is silent about it — a cycle drops hundreds — but it is the
    first thing to look at when a board goes quiet."""
    boards["ats:Alpha"] = [posting("ats:Alpha", "1", title="Chef")]

    poll_module.main([])
    assert "skip" not in capsys.readouterr().out

    poll_module.main(["--show-filtered", "--verbose"])
    assert "skip" in capsys.readouterr().out


def test_the_cli_reports_failures_parks_and_recoveries(db, boards, quiet,
                                                        monkeypatch,
                                                        capsys) -> None:
    boards["ats:Moved"] = StructuralError("HTTP 404")
    boards["ats:Broken"] = RuntimeError("timeout")
    monkeypatch.setattr(poll_module, "load_sources",
                        lambda: sources("Moved", "Broken"))
    monkeypatch.setattr(poll_module, "load_config",
                        lambda: Config(poll={"failures_before_disable": 1}))

    assert poll_module.main([]) == 0
    out = capsys.readouterr().out
    assert "FAIL ats:Broken" in out
    assert "PARK ats:Moved" in out
    assert "no auto-retry until --reset" in out


def test_the_cli_names_a_source_with_no_fetcher_as_a_todo(db, quiet,
                                                           monkeypatch,
                                                           capsys) -> None:
    def fetch(entry, timeout):
        raise SourceNotImplemented("no fetcher for provider 'taleo'")

    monkeypatch.setattr(poll_module, "fetch_source", fetch)
    monkeypatch.setattr(poll_module, "load_sources", lambda: sources("Legacy"))
    monkeypatch.setattr(poll_module, "load_config", lambda: Config())

    assert poll_module.main([]) == 0
    assert "todo ats:Legacy" in capsys.readouterr().out


def test_the_cli_announces_a_source_coming_back(db, boards, quiet, monkeypatch,
                                                 capsys) -> None:
    boards["ats:Broken"] = [posting("ats:Broken", "1")]
    with store.connect(db) as conn:
        for _ in range(3):
            store.mark_source_failed(conn, "ats:Broken", "boom", 3)
        _age(conn, "ats:Broken", hours=3)
        conn.commit()
    monkeypatch.setattr(poll_module, "load_sources", lambda: sources("Broken"))
    monkeypatch.setattr(poll_module, "load_config",
                        lambda: Config(poll={"retry_after_minutes": 60}))

    assert poll_module.main([]) == 0
    assert "BACK ats:Broken" in capsys.readouterr().out
