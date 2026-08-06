"""What reaches the phone: the ping, the digest, the notices, and /status.

Everything here is on the far side of a decision the user cannot revisit. A
posting is notified about exactly once — `store.unnotified_in_band` filters on
the record and every send writes one — so a message that is malformed, missing
a fact, or never sent at all is the last the user hears of that job. The
matcher can be re-run; this cannot.

Two properties get most of the attention below.

The first is that a send failing must never cost more than its own message.
Telegram rejects an over-length message outright rather than trimming it, and
rejects bad markup the same way, so one wide row or one unescaped `<` in a
company name would otherwise take the whole digest with it — and because the
recording happens after the send, the band keeps growing and every later digest
is larger still.

The second is that nothing operational may raise into the caller. The build
progress editor runs on a timer for forty minutes, the heartbeat runs from a
scheduler with nobody watching, and the source alerts fire precisely when
something is already wrong. All of them swallow and log.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3

import pytest
from telegram.error import BadRequest, NetworkError

from watcher import notifier as N
from watcher import store
from watcher.config import Config


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

COLUMNS = ("id", "score", "stop_and_ask", "stop_reason", "title", "company",
           "location", "country", "city", "remote", "provider", "url",
           "why_json", "gaps_json", "level", "years_required", "languages",
           "contract", "arrangement", "description")


def posting(**over) -> sqlite3.Row:
    """A verdict-joined row, built as a real `sqlite3.Row`.

    A dict would not reproduce the behaviour the formatters have to survive:
    `sqlite3.Row` raises on an unknown key rather than returning None, which is
    why `_field` exists and why the columns added by later migrations are the
    ones worth testing against.
    """
    fields = {
        "id": "p1", "score": 82, "stop_and_ask": 0, "stop_reason": None,
        "title": "Machine Learning Engineer", "company": "Example GmbH",
        "location": "Köln, Nordrhein-Westfalen", "country": "DE", "city": "Cologne",
        "remote": 0, "provider": "greenhouse",
        "url": "https://example.test/jobs/1",
        "why_json": json.dumps(["python", "nlp"]), "gaps_json": json.dumps(["k8s"]),
        "level": "senior", "years_required": 5, "languages": "German fluent",
        "contract": "permanent", "arrangement": "hybrid",
        "description": "Build models.",
    }
    fields.update(over)
    columns = [c for c in COLUMNS if c in fields]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sql = "SELECT " + ", ".join(f"? AS {c}" for c in columns)
    return conn.execute(sql, [fields[c] for c in columns]).fetchone()


class FakeBot:
    """Records sends and edits; can be told to fail on any of them."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.markup: list[int] = []
        self.fail_send: Exception | None = None
        self.fail_edit: Exception | None = None
        self.fail_markup: Exception | None = None
        self._next_id = 100

    async def send_message(self, **kwargs):
        if self.fail_send:
            raise self.fail_send
        self.sent.append(kwargs)
        self._next_id += 1
        return type("Msg", (), {"message_id": self._next_id})()

    async def edit_message_text(self, **kwargs):
        if self.fail_edit:
            raise self.fail_edit
        self.edits.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        if self.fail_markup:
            raise self.fail_markup
        self.markup.append(kwargs["message_id"])


@pytest.fixture()
def bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    fake = FakeBot()
    monkeypatch.setattr(N, "Bot", lambda token: fake)
    return fake


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    monkeypatch.setattr(N, "DB_PATH", path)
    return path


def store_posting(path, posting_id: str, score: int, **over) -> None:
    """A posting plus its verdict, the way a poll cycle leaves them."""
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
               canonical_url, company, title, location, country, description,
               first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (posting_id, f"k-{posting_id}", "portal:test", "test",
             f"https://example.test/{posting_id}",
             f"https://example.test/{posting_id}",
             over.get("company", "ExampleCo"), over.get("title", "ML Engineer"),
             over.get("location", "Berlin"), "DE", "Build models.",
             dt.datetime.now().isoformat(timespec="seconds")))
        store.save_verdict(conn, posting_id,
                           {"score": score, "verdict": "strong", "why": [],
                            "gaps": over.get("gaps", []), "stop_and_ask": False,
                            "stop_reason": None}, "haiku")


def run(coro):
    return asyncio.run(coro)


# ===========================================================================
# one posting on a phone screen
# ===========================================================================

def test_the_ping_leads_with_the_decision(bot) -> None:
    text = N.format_instant(posting())
    first, second = text.splitlines()[:2]
    assert first.startswith("🟢 <b>82</b> · Machine Learning Engineer")
    assert "Example GmbH — Köln, Nordrhein-Westfalen · hybrid · greenhouse" in second


def test_the_source_location_beats_the_canonical_city() -> None:
    """"Köln, Nordrhein-Westfalen" says more than the one word geo resolves."""
    assert "Köln, Nordrhein-Westfalen" in N.format_instant(posting())
    assert "Cologne" not in N.format_instant(posting())
    assert "Cologne" in N.format_instant(posting(location=None))
    assert "DE" in N.format_instant(posting(location=None, city=""))
    assert "location unknown" in N.format_instant(
        posting(location=None, city="", country=None))


def test_a_remote_flag_shows_when_no_arrangement_was_read() -> None:
    assert "· remote" in N.format_instant(posting(arrangement="", remote=1))
    assert "· hybrid" in N.format_instant(posting(arrangement="hybrid", remote=1))


def test_the_language_bar_gets_its_own_line() -> None:
    """The fact most likely to disqualify a posting outright, and the one
    buried deepest in the ad — it has to survive a glance at a preview."""
    assert "🗣 German fluent" in N.format_instant(posting())
    assert "🗣" not in N.format_instant(posting(languages=""))


def test_the_rank_and_contract_share_a_line_when_present() -> None:
    assert "· senior · asks 5+ yrs · permanent" in N.format_instant(posting())
    text = N.format_instant(posting(level="", years_required=None, contract=""))
    assert "· asks" not in text


@pytest.mark.parametrize("score,icon", [(90, "🟢"), (75, "🟢"), (74, "🔵")])
def test_the_band_icon_follows_the_score(score, icon) -> None:
    assert N.format_instant(posting(score=score)).startswith(icon)


def test_a_posting_needing_a_decision_is_marked_amber() -> None:
    """Amber over green even at 90: the score says it is worth applying for and
    the flag says it cannot be, and the second is the one to act on."""
    text = N.format_instant(posting(score=90, stop_and_ask=1,
                                    stop_reason="requires a clearance"))
    assert text.startswith("🟡")
    assert "❗ Needs a decision: requires a clearance" in text


def test_a_flag_with_no_reason_does_not_leave_a_dangling_line() -> None:
    text = N.format_instant(posting(stop_and_ask=1, stop_reason=None))
    assert "❗" not in text and text.startswith("🟡")


def test_markup_in_a_company_name_cannot_break_the_message(bot) -> None:
    """Telegram rejects unbalanced HTML outright, and the whole send fails —
    so one `<` in a company name would cost the notification, not the tag."""
    text = N.format_instant(posting(company="A <b>Bold</b> & Co",
                                    title="Dev <script>"))
    assert "&lt;b&gt;Bold&lt;/b&gt; &amp; Co" in text
    assert "<b>" in text.splitlines()[0], "the score's own tags stay real"
    assert "<script>" not in text


def test_the_url_sits_last_so_it_does_not_push_the_rest_out_of_preview() -> None:
    lines = [l for l in N.format_instant(posting()).splitlines() if l]
    assert lines[-2] == "https://example.test/jobs/1"
    assert "Reply: yes / no / later" in lines[-1]


def test_a_posting_with_no_reasoning_still_formats() -> None:
    text = N.format_instant(posting(why_json=None, gaps_json="not json"))
    assert "✓" not in text and "⚠" not in text


def test_the_readings_are_recomputed_when_the_columns_are_empty() -> None:
    """Those columns arrived in a later migration, so every posting already in
    the database has them blank — without the fallback the language bar would
    only ever appear on jobs found after the upgrade."""
    text = N.format_instant(posting(
        languages="", contract="", arrangement="", remote=0,
        description="Unbefristete Festanstellung. Sie sprechen fließend Deutsch."))
    assert "🗣 German fluent" in text
    assert "permanent" in text


def test_nothing_to_recompute_from_is_not_an_error() -> None:
    text = N.format_instant(posting(languages="", contract="", arrangement="",
                                    description=""))
    assert "🗣" not in text


def test_a_row_missing_the_later_columns_entirely_still_formats() -> None:
    """`sqlite3.Row` raises on an unknown key, and not every query selects the
    lot — a formatting helper must not fall over a column that is merely absent."""
    bare = posting()
    trimmed = sqlite3.connect(":memory:")
    trimmed.row_factory = sqlite3.Row
    row = trimmed.execute(
        "SELECT ? AS id, ? AS score, ? AS stop_and_ask, ? AS stop_reason, "
        "? AS title, ? AS company, ? AS location, ? AS country, ? AS remote, "
        "? AS provider, ? AS url, ? AS why_json, ? AS gaps_json",
        (bare["id"], 82, 0, None, bare["title"], bare["company"], "Berlin",
         "DE", 0, "greenhouse", bare["url"], "[]", "[]")).fetchone()
    assert "82" in N.format_instant(row)


# ===========================================================================
# the digest
# ===========================================================================

def test_an_empty_digest_is_an_empty_string() -> None:
    assert N.format_digest([]) == ""
    assert N.format_digest_chunks([]) == []


def test_a_single_part_digest_says_nothing_about_parts() -> None:
    text = N.format_digest([posting(id="a"), posting(id="b")])
    assert "2 posting(s) worth a look" in text
    assert "part 1/" not in text
    assert "Numbers count from 1 in each part" not in text
    assert "1. <b>82</b>" in text and "2. <b>82</b>" in text


def test_one_absurd_entry_is_truncated_rather_than_dropped() -> None:
    """It would exceed the limit on its own, so packing cannot save it. The
    tag pair closes around the score on the first line, so cutting the tail
    leaves the HTML balanced."""
    entry = N._digest_entry(1, posting(title="X" * 6000))
    assert len(entry) <= N.DIGEST_ENTRY_MAX
    assert entry.endswith("…")
    assert entry.count("<b>") == entry.count("</b>")


# ===========================================================================
# the kb proposal and the targeted record
# ===========================================================================

def test_the_proposal_shows_which_decisions_produced_each_rule() -> None:
    """Approving a matching rule without seeing that is how a one-off "no, too
    much devops" becomes a standing filter nobody remembers agreeing to."""
    text = N.format_kb_proposal({
        "proposals": [{"section": "Avoid", "text": "Pure devops",
                       "because": "3 skips in a row"},
                      {"section": "Prefer", "text": "Remote-first"}],
        "reviewed": 9, "summary": "two rules",
    })
    assert "🧠 <b>Matching rules — 2 proposed</b>" in text
    assert "from 9 recent decision(s)" in text
    assert "1. <b>Avoid</b> — Pure devops" in text
    assert "<i>3 skips in a row</i>" in text
    assert "two rules" in text
    assert "Reply “yes”" in text


def test_a_proposal_with_nothing_in_it_still_answers() -> None:
    text = N.format_kb_proposal({})
    assert "0 proposed" in text and "from 0 recent" in text


def test_the_targeted_record_carries_the_decision_not_the_ping() -> None:
    """The ping lives in another topic and says nothing about the approval;
    what makes this worth keeping is when it was taken and with what note."""
    when = dt.datetime(2026, 8, 4, 21, 30)
    text = N.format_targeted("Example GmbH", "ML Engineer",
                             "https://example.test/1", 88, "add German", when)
    assert "✅ <b>Example GmbH</b> — ML Engineer" in text
    assert "score 88 · approved 04 Aug 21:30" in text
    assert "📝 add German" in text
    assert "https://example.test/1" in text


def test_a_targeted_record_without_a_score_or_note_is_still_a_record() -> None:
    text = N.format_targeted("Example", "Role", "", None, "")
    assert "approved" in text and "score" not in text
    assert "📝" not in text
    assert text.count("\n") == 1


# ===========================================================================
# the small readings /status is built from
# ===========================================================================

@pytest.mark.parametrize("seconds,expected", [
    (0, "0m"), (60 * 61, "1h 1m"), (86400 * 2 + 3600 * 3, "2d 3h"),
])
def test_uptime_drops_the_unit_below_the_one_that_matters(seconds, expected) -> None:
    since = dt.datetime.now() - dt.timedelta(seconds=seconds)
    assert N._uptime(since) == expected


def test_an_unknown_start_time_says_so_rather_than_showing_zero() -> None:
    assert N._uptime(None) == "unknown"


@pytest.mark.parametrize("seconds,expected", [
    (5, "5s"), (89, "89s"), (90, "1m"), (5399, "89m"), (5400, "1h 30m"),
    (86400 * 2 - 1, "47h 59m"), (86400 * 2, "2d 0h"), (86400 * 3 + 3600, "3d 1h"),
])
def test_a_span_uses_the_largest_unit_that_still_says_something(
        seconds, expected) -> None:
    assert N._span(seconds) == expected


def test_a_negative_span_reads_as_zero() -> None:
    assert N._span(-10) == "0s"


def test_a_timestamp_carries_both_the_clock_and_the_distance() -> None:
    """The clock time is what gets compared against the schedule; the age is
    what answers "is this thing stuck"."""
    now = dt.datetime(2026, 8, 4, 14, 44)
    assert N._when("2026-08-04T14:32:00", now) == "14:32:00 (12m ago)"
    assert N._when("2026-08-01T09:00:00", now) == "01 Aug 09:00 (3d 5h ago)"


def test_a_stamp_from_the_future_shows_the_time_alone() -> None:
    """A clock that moved. "-4m ago" reads as a bug in the report."""
    now = dt.datetime(2026, 8, 4, 14, 0)
    assert N._when("2026-08-04T14:30:00", now) == "04 Aug 14:30"


@pytest.mark.parametrize("value", [None, "", "not a timestamp", 42])
def test_an_unreadable_stamp_is_never_rather_than_a_crash(value) -> None:
    assert N._when(value) == "never"
    assert N._moment(value) is None


def test_a_datetime_passes_straight_through() -> None:
    moment = dt.datetime(2026, 8, 4)
    assert N._moment(moment) is moment


def test_a_missing_database_reports_a_size_rather_than_raising(monkeypatch,
                                                              tmp_path) -> None:
    monkeypatch.setattr(N, "DB_PATH", tmp_path / "gone.db")
    assert N._db_size() == "missing"


# ===========================================================================
# source health, read for the report
# ===========================================================================

def health_row(name: str, *, disabled=0, failures=0, error="", parked=0):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT ? AS source, ? AS disabled, ? AS consecutive_failures, "
        "? AS last_error, ? AS parked, ? AS last_ok_at, ? AS disabled_at",
        (name, disabled, failures, error, parked, None, None)).fetchone()


@pytest.fixture()
def health(db, monkeypatch):
    """The source-health table, scripted. The rest of the database is real."""
    rows: list = []
    monkeypatch.setattr(store, "source_health", lambda conn: rows)
    return rows


def test_the_tally_separates_parked_from_merely_disabled(health) -> None:
    """Parked wants a code change; disabled retries itself. Counting them
    together would hide the one that needs a person."""
    health += [health_row("a"), health_row("b", failures=2),
               health_row("c", disabled=1),
               health_row("d", disabled=1, parked=1)]
    assert N._source_tally() == (1, 1, 1, 1)


def test_an_unreadable_health_table_costs_that_line_and_nothing_else(
        monkeypatch) -> None:
    """`/status` is often the thing someone reaches for *because* something is
    wrong, so it cannot be the thing that breaks when it is."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(N.store, "connect", boom)
    assert N._source_tally() is None
    assert N._ailing_sources() == []


def test_the_ailing_list_puts_the_worst_first_and_caps_itself(health) -> None:
    """Parked first: it is the only one nothing will fix on its own."""
    health += [health_row("fine"),
               health_row("flaky", failures=3),
               health_row("off", disabled=1),
               health_row("stuck", disabled=1, parked=1)]
    assert N._ailing_sources() == ["stuck (parked)", "off (disabled)",
                                   "flaky (failing ×3)"]


def test_the_ailing_list_is_a_hint_not_an_inventory(health) -> None:
    health += [health_row(f"s{i}", disabled=1) for i in range(9)]
    assert len(N._ailing_sources()) == 4
    assert len(N._ailing_sources(limit=2)) == 2


# ===========================================================================
# /status
# ===========================================================================

def cycle(**over) -> dict:
    base = {
        "finished_at": "2026-08-04T14:32:00", "started_at": "2026-08-04T14:30:00",
        "seconds": 42, "fetched": 4087, "already_known": 590, "filtered": 3497,
        "stored": 0, "scored": 0, "notified": 0,
    }
    base.update(over)
    return base


def test_the_report_says_when_the_last_cycle_ran(db, health) -> None:
    """A watcher that has quietly stopped polling looks exactly like one with
    nothing to report unless the time of the last fetch is on the screen."""
    text = N.format_status(Config(), cycle(), {}, dt.datetime.now())
    assert "<b>Last cycle:</b>" in text and "took 42s" in text
    assert "4087 fetched → 590 already seen → 3497 filtered → <b>0 new</b>" in text


def test_an_overdue_cycle_is_called_out(db, health) -> None:
    text = N.format_status(
        Config(poll={"interval_minutes": 30}),
        cycle(started_at=(dt.datetime.now()
                          - dt.timedelta(hours=3)).isoformat()),
        {}, dt.datetime.now())
    assert "<b>Next cycle:</b>" in text and "overdue" in text


def test_a_cycle_running_to_schedule_is_not(db, health) -> None:
    text = N.format_status(
        Config(poll={"interval_minutes": 30}),
        cycle(started_at=dt.datetime.now().isoformat()), {}, dt.datetime.now())
    assert "Next cycle:" in text and "overdue" not in text


def test_a_record_predating_the_wider_funnel_is_not_shown_as_zeros(
        db, health) -> None:
    """Three fields of zeros would read as a broken poller rather than an
    out-of-date row."""
    old = cycle()
    del old["already_known"]
    text = N.format_status(Config(), old, {}, dt.datetime.now())
    assert "already seen" not in text
    assert "4087 fetched → <b>0 new</b>" in text


def test_no_cycle_at_all_says_so(db, health) -> None:
    assert "<b>Last cycle:</b> none on record" in N.format_status(
        Config(), {}, {}, dt.datetime.now())


def test_a_failed_source_is_named_in_the_cycle_block(db, health) -> None:
    text = N.format_status(Config(),
                           cycle(sources_failed=["portal:stepstone"]),
                           {}, dt.datetime.now())
    assert "⚠️ failed: portal:stepstone" in text


def test_the_deferred_count_only_appears_when_there_is_one(db, health) -> None:
    assert "deferred" not in N.format_status(Config(), cycle(), {},
                                             dt.datetime.now())
    assert "3 deferred" in N.format_status(Config(), cycle(deferred=3), {},
                                           dt.datetime.now())


def test_the_build_line_distinguishes_idle_from_switched_off(db, health) -> None:
    on = Config(build={"enabled": True})
    assert "<b>Builds:</b> idle" in N.format_status(on, {}, {}, None)
    assert "1 running, 2 queued" in N.format_status(
        on, {}, {"running": 1, "queued": 2}, None)
    assert "off — approvals recorded, nothing built" in N.format_status(
        Config(build={"enabled": False}), {}, {}, None)


def test_the_database_block_reflects_what_is_stored(db, health) -> None:
    store_posting(db, "a", 88, title="Strong One")
    store_posting(db, "b", 50)
    text = N.format_status(Config(), {}, {}, None)
    assert "<b>Database:</b> 2 postings · 2 scored · 0 awaiting score" in text
    assert "Today: 2 new · 2 scored · 0 pinged" in text
    assert "Latest verdict:" in text
    assert "<b>Unsent:</b> 1 at ≥70 · 1 in the digest band" in text
    assert "/recheck sends them" in text


def test_unjudged_postings_get_the_command_that_moves_them(db, health) -> None:
    """"Unjudged" reads like a queue and is the opposite — these are finished,
    parked at a score nobody meant, and nothing picks them up on its own."""
    store_posting(db, "a", 45, gaps=[store.DEGRADED_GAP])
    text = N.format_status(Config(), {}, {}, None)
    assert "1 unjudged" in text and "/rescore re-queues them" in text


def test_an_unreadable_database_still_produces_a_report(monkeypatch,
                                                        health) -> None:
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(N.store, "connect", boom)
    text = N.format_status(Config(), cycle(), {}, dt.datetime.now())
    assert "<b>Last cycle:</b>" in text
    assert "<b>Database:</b>" not in text
    assert "unreadable — see watcher.log" in text


def test_the_ailing_sources_are_listed_under_the_tally(db, health) -> None:
    health += [health_row("ok-one"), health_row("portal:stepstone", disabled=1)]
    text = N.format_status(Config(), {}, {}, None)
    assert "<b>Sources:</b> 1 ok · 1 disabled" in text
    assert "⚠️ portal:stepstone (disabled)" in text


def test_a_state_with_nothing_in_it_is_left_out_of_the_tally(db, health) -> None:
    """"3 ok · 0 failing · 0 disabled · 0 parked" is four numbers to read to
    learn one thing. Only the states that apply are shown."""
    health += [health_row("a"), health_row("b", failures=2),
               health_row("c", disabled=1), health_row("d", disabled=1, parked=1)]
    text = N.format_status(Config(), {}, {}, None)
    assert "<b>Sources:</b> 1 ok · 1 failing · 1 disabled · 1 parked" in text

    health[:] = [health_row("a")]
    assert "<b>Sources:</b> 1 ok\n" in N.format_status(Config(), {}, {}, None)


def test_topic_routing_is_reported_only_when_it_is_on(db, health) -> None:
    assert "Topics:" not in N.format_status(Config(), {}, {}, None)
    routed = Config(notify={"topics": {"new_posting": 12}})
    assert "<i>Topics: 1 of 5 routed</i>" in N.format_status(routed, {}, {}, None)


def test_the_report_fits_a_message_with_every_source_failing(db, health) -> None:
    """The realistic worst case: a full table, and each row carrying the body
    of the error page that came back instead of listings."""
    store_posting(db, "a", 88, company="A Very Long Company Name GmbH & Co. KG",
                  title="Senior Machine Learning Engineer, Platform Group")
    wide = cycle(sources={
        f"portal:job-board-number-{i}": {
            "error": "<html><head><title>502 Bad Gateway</title></head>"}
        for i in range(40)})
    text = N.format_status(Config(), wide, {}, dt.datetime.now())
    assert len(text) <= N.TELEGRAM_MAX_CHARS


def test_an_over_length_report_drops_the_funnel_rather_than_failing(
        db, health, monkeypatch, caplog) -> None:
    """Telegram rejects an over-length message outright, so a wide source table
    would cost the whole reply. The funnel is the least load-bearing part."""
    monkeypatch.setattr(N, "TELEGRAM_MAX_CHARS", 900)
    wide = cycle(sources={f"portal:source-number-{i}": {
        "fetched": 100, "already_known": 1, "filtered": 2, "stored": 3}
        for i in range(30)})
    text = N.format_status(Config(), wide, {}, dt.datetime.now())
    assert "Per source:" not in text
    assert "more (0 with nothing new)" not in text
    assert "<b>Last cycle:</b>" in text, "the block /status is actually for"
    assert "4087 fetched" in text


def test_the_funnel_block_names_a_source_that_failed(db, health) -> None:
    """Its counts would all be zero and read as "this board has nothing",
    which is the opposite of what happened."""
    text = N.format_status(
        Config(), cycle(sources={"portal:stepstone": {"error": "HTTP 403"}}),
        {}, dt.datetime.now())
    assert "portal:stepstone" in text and "failed — HTTP 403" in text


def test_the_funnel_summarises_past_twenty_sources(db, health) -> None:
    text = N.format_status(
        Config(),
        cycle(sources={f"ats:c{i}": {"fetched": 1, "stored": 0}
                       for i in range(25)}),
        {}, dt.datetime.now())
    assert "+5 more (5 with nothing new)" in text


def test_a_cycle_predating_the_per_source_data_shows_no_table(db, health) -> None:
    """An empty table would read as "no sources configured"."""
    assert "Per source:" not in N.format_status(Config(), cycle(), {}, None)
    assert N._source_funnel_lines({"sources": {}}) == []
    assert N._source_funnel_lines({"sources": "not a dict"}) == []
    assert N._source_funnel_lines({"sources": {"a": "not a dict"}}) == []


def test_a_long_source_name_is_elided_rather_than_shifting_the_columns(
        db, health) -> None:
    text = N.format_status(
        Config(),
        cycle(sources={"portal:an-extremely-long-source-name": {"fetched": 1}}),
        {}, dt.datetime.now())
    assert "portal:an-extremely-l…" in text


# ===========================================================================
# Notifier — sending
# ===========================================================================

def test_a_ping_is_recorded_so_it_cannot_be_sent_twice(bot, db) -> None:
    store_posting(db, "a", 88)
    notifier = N.Notifier(Config())

    assert run(notifier.send_instant()) == 1
    assert run(notifier.send_instant()) == 0, "the second poll finds nothing"
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == "-100123"


def test_one_failed_ping_does_not_stop_the_others(bot, db, caplog) -> None:
    """And the failed one stays in the band, so it goes out next cycle."""
    store_posting(db, "a", 88)
    store_posting(db, "b", 89)
    notifier = N.Notifier(Config())

    original = bot.send_message
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NetworkError("connection reset")
        return await original(**kwargs)

    bot.send_message = flaky
    assert run(notifier.send_instant()) == 1
    with store.connect(db) as conn:
        assert len(store.unnotified_in_band(conn, 70)) == 1


def test_the_overflow_waits_for_the_digest_rather_than_being_dropped(
        bot, db) -> None:
    for i in range(8):
        store_posting(db, f"p{i}", 90 - i)
    notifier = N.Notifier(Config())

    assert run(notifier.send_instant(limit=6)) == 6
    with store.connect(db) as conn:
        assert len(store.unnotified_in_band(conn, 70)) == 2


def test_nothing_in_the_band_is_not_a_send(bot, db) -> None:
    assert run(N.Notifier(Config()).send_instant()) == 0
    assert bot.sent == []


def test_the_digest_records_every_row_of_the_part_that_sent(bot, db) -> None:
    for i in range(4):
        store_posting(db, f"p{i}", 50)
    notifier = N.Notifier(Config())

    assert run(notifier.send_digest()) == 4
    assert len(bot.sent) == 1
    with store.connect(db) as conn:
        assert store.unnotified_in_band(conn, 40) == []


def test_a_rejected_digest_part_costs_only_its_own_rows(bot, db, monkeypatch,
                                                        caplog) -> None:
    """Recorded per part: the rest still go out, and these stay in the band for
    the next digest instead of being marked sent or lost."""
    for i in range(4):
        store_posting(db, f"p{i}", 50)
    rows_seen: list = []

    def two_parts(rows):
        rows = list(rows)
        rows_seen.extend(rows)
        return [("part one", rows[:2]), ("part two", rows[2:])]

    monkeypatch.setattr(N, "format_digest_chunks", two_parts)
    bot.fail_send = BadRequest("message is too long")
    notifier = N.Notifier(Config())
    assert run(notifier.send_digest()) == 0

    bot.fail_send = None
    assert run(notifier.send_digest()) == 4


def test_an_empty_digest_is_logged_not_sent(bot, db, caplog) -> None:
    assert run(N.Notifier(Config()).send_digest()) == 0
    assert bot.sent == []


def test_a_notice_that_fails_to_send_never_reaches_the_caller(bot, db) -> None:
    """The heartbeat runs from a scheduler with nobody watching; a raise here
    would take the job down rather than skip a message."""
    bot.fail_send = NetworkError("down")
    run(N.Notifier(Config()).send_notice("hello"))


def test_the_targeted_record_is_a_no_op_without_its_topic(bot, db) -> None:
    """It has no equivalent in the chat-id-only setup, and posting it in
    General would add traffic to a chat this feature must not touch."""
    run(N.Notifier(Config()).send_targeted("Example", "Role", "u", 80, ""))
    assert bot.sent == []

    routed = Config(notify={"topics": {"targeted_build": 12}})
    run(N.Notifier(routed).send_targeted("Example", "Role", "u", 80, "note"))
    assert len(bot.sent) == 1
    assert bot.sent[0]["message_thread_id"] == 12


def test_a_failed_targeted_record_does_not_take_the_build_with_it(
        bot, db) -> None:
    bot.fail_send = NetworkError("down")
    routed = Config(notify={"topics": {"targeted_build": 12}})
    run(N.Notifier(routed).send_targeted("Example", "Role", "u", 80, ""))


# ===========================================================================
# Notifier — operational notices
# ===========================================================================

def test_the_disable_notice_says_when_it_will_retry_itself(bot, db) -> None:
    run(N.Notifier(Config(poll={"retry_after_minutes": 30}))
        .send_source_alerts(["portal:stepstone"]))
    text = bot.sent[0]["text"]
    assert "Source disabled after repeated failures" in text
    assert "Retrying automatically in ~30 min" in text


def test_with_auto_retry_off_the_notice_says_that_instead(bot, db) -> None:
    run(N.Notifier(Config(poll={"retry_after_minutes": 0}))
        .send_source_alerts(["portal:stepstone"]))
    assert "Auto-recovery is off" in bot.sent[0]["text"]


def test_a_parked_source_is_escalated_with_the_error_that_diagnoses_it(
        bot, db) -> None:
    """Louder and more specific than the disable notice: this one is a work
    item, not weather — and nothing will send it again."""
    run(N.Notifier(Config()).send_source_parked(
        [("portal:stepstone", "HTTP 404 for https://example.test/api")]))
    text = bot.sent[0]["text"]
    assert "Source parked — needs a code change" in text
    assert "<code>HTTP 404 for https://example.test/api</code>" in text


def test_recovery_is_announced_so_the_disable_notice_is_not_the_last_word(
        bot, db) -> None:
    run(N.Notifier(Config()).send_source_recovered(["portal:stepstone"]))
    assert "Source recovered" in bot.sent[0]["text"]


@pytest.mark.parametrize("method,arg", [
    ("send_source_alerts", []), ("send_source_parked", []),
    ("send_source_recovered", []),
])
def test_nothing_to_report_sends_nothing(bot, db, method, arg) -> None:
    run(getattr(N.Notifier(Config()), method)(arg))
    assert bot.sent == []


class Report:
    def __init__(self, failed=0, deferred=0, exhausted=0) -> None:
        self.failed, self.deferred, self.exhausted = failed, deferred, exhausted


def test_a_degraded_cycle_is_announced_because_silence_looks_the_same(
        bot, db) -> None:
    """A broken scorer and a quiet job market are both silence from outside.
    The one time they were confused it cost 49 buried postings."""
    run(N.Notifier(Config()).send_scoring_degraded(Report(5, 3, 2)))
    text = bot.sent[0]["text"]
    assert "5 posting(s) could not be judged" in text
    assert "3 will be re-scored automatically" in text
    assert "2 ran out of attempts" in text and "watcherctl.py rescore" in text


def test_a_clean_cycle_says_nothing(bot, db) -> None:
    run(N.Notifier(Config()).send_scoring_degraded(Report()))
    run(N.Notifier(Config()).send_scoring_degraded(object()))
    assert bot.sent == []


def test_the_heartbeat_reports_the_last_poll_and_the_broken_sources(
        bot, db, monkeypatch) -> None:
    monkeypatch.setattr(store, "source_health",
                        lambda conn: [health_row("ok-one"),
                                      health_row("bad", disabled=1)])
    run(N.Notifier(Config()).send_heartbeat(
        {"fetched": 100, "stored": 4, "notified": 1,
         "finished_at": dt.datetime.now().isoformat()}))
    text = bot.sent[0]["text"]
    assert "Watcher alive" in text
    assert "100 fetched, 4 new, 1 notified" in text
    assert "sources: 1 ok, 1 disabled" in text
    assert "disabled: bad" in text


def test_a_restart_before_the_heartbeat_reads_the_cycle_from_the_database(
        bot, db, monkeypatch) -> None:
    """"0 fetched, 0 new" is the one thing this message must never say when
    the watcher is in fact fine."""
    monkeypatch.setattr(store, "source_health", lambda conn: [])
    with store.connect(db) as conn:
        store.save_cycle(conn, {"fetched": 77, "stored": 2, "notified": 1})
    run(N.Notifier(Config()).send_heartbeat({}))
    assert "77 fetched, 2 new, 1 notified" in bot.sent[0]["text"]


# ===========================================================================
# Notifier — editing, and the pings whose message vanished
# ===========================================================================

def test_an_edit_lands_in_whatever_topic_the_original_went_to(bot, db) -> None:
    notifier = N.Notifier(Config(notify={"topics": {"processing_build": 12}}))
    assert run(notifier.edit(500, "updated")) is True
    assert bot.edits[0]["message_id"] == 500
    assert "message_thread_id" not in bot.edits[0]


def test_re_rendering_to_the_same_text_is_ordinary(bot, db) -> None:
    """The checklist ticks every 30s and mostly has nothing new to say."""
    bot.fail_edit = BadRequest("Message is not modified")
    assert run(N.Notifier(Config()).edit(500, "same")) is True


def test_a_deleted_message_gives_up_its_place(bot, db, caplog) -> None:
    bot.fail_edit = BadRequest("Message to edit not found")
    assert run(N.Notifier(Config()).edit(500, "x")) is False
    bot.fail_edit = BadRequest("Message can't be edited")
    assert run(N.Notifier(Config()).edit(500, "x")) is False


@pytest.mark.parametrize("failure", [
    BadRequest("Too many requests"), NetworkError("connection reset"),
    RuntimeError("something else entirely"),
])
def test_any_other_edit_failure_gets_another_go_next_tick(bot, db, failure) -> None:
    """A progress message is a convenience; the build behind it is not, and
    nothing here may end one."""
    bot.fail_edit = failure
    assert run(N.Notifier(Config()).edit(500, "x")) is True


def test_a_present_message_is_probed_without_changing_it(bot, db) -> None:
    assert run(N.Notifier(Config()).message_exists(500)) is True
    assert bot.markup == [500]

    bot.fail_markup = BadRequest("Message is not modified")
    assert run(N.Notifier(Config()).message_exists(500)) is True


def test_only_a_positive_missing_answer_counts_as_gone(bot, db) -> None:
    bot.fail_markup = BadRequest("Message to edit not found")
    assert run(N.Notifier(Config()).message_exists(500)) is False

    bot.fail_markup = NetworkError("down")
    assert run(N.Notifier(Config()).message_exists(500)) is True, \
        "network trouble is not evidence of deletion"


def test_a_cleared_chat_leaves_pings_recorded_for_messages_that_are_gone(
        bot, db) -> None:
    """The record and the chat disagreeing is a silent, permanent hole: the
    posting counts as notified, so nothing will ever raise it again."""
    store_posting(db, "a", 88)
    store_posting(db, "b", 89)
    notifier = N.Notifier(Config())
    run(notifier.send_instant())

    bot.fail_markup = BadRequest("Message to edit not found")
    vanished = run(notifier.forget_vanished(dry_run=True))
    assert len(vanished) == 2
    with store.connect(db) as conn:
        assert store.unnotified_in_band(conn, 70) == [], "a dry run changes nothing"

    run(notifier.forget_vanished(dry_run=False))
    with store.connect(db) as conn:
        assert len(store.unnotified_in_band(conn, 70)) == 2


def test_a_probe_that_could_not_be_answered_under_reports(bot, db) -> None:
    """Rather than resending something the user already has."""
    store_posting(db, "a", 88)
    notifier = N.Notifier(Config())
    run(notifier.send_instant())

    bot.fail_markup = NetworkError("down")
    assert run(notifier.forget_vanished(dry_run=False)) == []


def test_the_score_floor_narrows_the_sweep(bot, db) -> None:
    store_posting(db, "a", 88)
    notifier = N.Notifier(Config())
    run(notifier.send_instant())
    bot.fail_markup = BadRequest("Message to edit not found")
    assert run(notifier.forget_vanished(min_score=95)) == []


# ===========================================================================
# Notifier — construction and the maintenance CLI
# ===========================================================================

def test_config_resolves_live_unless_one_was_pinned(bot, db, monkeypatch) -> None:
    """A watcher up for weeks has to see an edit to config.toml."""
    loaded = []
    monkeypatch.setattr(N, "load_config", lambda: loaded.append(1) or Config())
    N.Notifier().config
    assert loaded

    pinned = Config()
    assert N.Notifier(pinned).config is pinned


def test_the_bot_is_exposed_for_the_reply_handler(bot, db) -> None:
    assert N.Notifier(Config()).bot is bot


def test_the_cli_reports_before_it_touches_anything(bot, db, capsys) -> None:
    store_posting(db, "a", 88)
    run(N.Notifier(Config()).send_instant())
    bot.fail_markup = BadRequest("Message to edit not found")

    assert N.main(["--forget-vanished"]) == 0
    out = capsys.readouterr().out
    assert "Would forget 1 ping(s)" in out
    assert "Re-run with --apply" in out
    with store.connect(db) as conn:
        assert store.unnotified_in_band(conn, 70) == []

    assert N.main(["--forget-vanished", "--apply"]) == 0
    assert "Forgot 1 ping(s)" in capsys.readouterr().out
    with store.connect(db) as conn:
        assert len(store.unnotified_in_band(conn, 70)) == 1


def test_the_cli_says_when_there_is_nothing_to_do(bot, db, capsys) -> None:
    assert N.main(["--forget-vanished"]) == 0
    assert "Every recorded ping is still in the chat" in capsys.readouterr().out


def test_the_cli_with_no_action_shows_the_help(bot, db, capsys) -> None:
    assert N.main([]) == 2
    assert "--forget-vanished" in capsys.readouterr().out
