"""The weekly audit: it measures, and it must not decide.

Two properties are load-bearing and everything here exists to pin them down.

The first is that it writes nothing. A drop that re-scores at 90 is a finding,
not a posting — promoting it would put a row in `verdicts` for an id that has
no row in `postings`, and the digest reads verdicts. So the audit's only write
is `drops.audited_at`, and that is asserted directly against the tables rather
than through a mock, because a `persist=True` slipping into one call is
exactly the kind of change that passes every behavioural test.

The second is that the denominator stays honest. A posting whose body cannot
be re-fetched, and a batch the matcher failed on, are unknowns — counting
either as "correctly dropped" would make the miss rate fall as the feeds got
worse, which is the one direction a quality metric must never move.
"""

from __future__ import annotations

import datetime as dt

import pytest

from watcher import matcher, recall, store
from watcher.config import Config
from watcher.normalize import Posting

BODY = "x" * 2000  # comfortably past recall.MIN_BODY_CHARS


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    store.init_db(path)
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in this file is allowed to make an HTTP call or a model call.

    Both are overridden per test where they matter; this is the backstop that
    turns a forgotten patch into a failure instead of a live fetch.
    """
    monkeypatch.setattr(recall, "hydrate",
                        lambda *a, **k: pytest.fail("hydrate was not patched"))
    monkeypatch.setattr(matcher, "run_json",
                        lambda *a, **k: pytest.fail("run_json was not patched"))
    monkeypatch.setattr(matcher.profile, "get_digest", lambda *a, **k: "DIGEST")
    monkeypatch.setattr(matcher.profile, "get_kb", lambda: "KB")
    # The public-page fallback is a second and a third way out to the network.
    # Default them to "the page is gone", which is what every test asserting
    # `no_body` means, rather than to a failure — the fallback runs on every
    # row whose hydrator came back short, so making it fail loudly would just
    # mean patching it in every test in the file.
    monkeypatch.setattr(recall, "get_text", lambda *a, **k: "")
    monkeypatch.setattr(recall, "session", lambda: None)
    monkeypatch.setattr(recall.browser, "page_text", lambda *a, **k: "")


def config(**recall_overrides) -> Config:
    return Config(
        match={"notify_threshold": 70, "digest_threshold": 40},
        recall={"sample_size": 40, "lookback_days": 7, "min_drops": 5,
                **recall_overrides},
    )


def posting(job_id: str, title: str = "Data Scientist",
            company: str = "Acme") -> Posting:
    return Posting(source="ats", provider="greenhouse", source_job_id=job_id,
                   url=f"https://example.test/{job_id}", company=company,
                   title=f"{title} {job_id}", location="Berlin", country="DE",
                   detail_url=f"https://example.test/{job_id}/detail")


def drop(conn, job_id: str, stage: str = "triage", **kwargs) -> str:
    p = posting(job_id, **kwargs)
    store.record_drop(conn, p, stage, "reason", digest_key="KEY")
    return p.fingerprint


def seed(db, count: int, stage: str = "triage") -> list[str]:
    with store.connect(db) as conn:
        return [drop(conn, f"{stage}-{i}", stage=stage) for i in range(count)]


def scores(monkeypatch, by_index):
    """Make the matcher return a fixed score per posting, in id order."""
    def fake(prompt, **kwargs):
        ids = [line.split("### id: ", 1)[1].strip()
               for line in prompt.splitlines() if line.startswith("### id: ")]
        return {"results": [
            {"id": posting_id, "score": by_index(index), "verdict": "maybe",
             "why": [], "gaps": []}
            for index, posting_id in enumerate(ids)]}
    monkeypatch.setattr(matcher, "run_json", fake)


# --------------------------------------------------------------------------
# plan_sample — stratification
# --------------------------------------------------------------------------

def test_triage_is_capped_at_its_share_however_large_it_is():
    plan = recall.plan_sample({"triage": 5000, "location": 500}, 40)
    assert plan["triage"] == 24
    assert plan["location"] == 16


def test_small_stages_get_the_floor_even_beside_a_huge_triage():
    plan = recall.plan_sample(
        {"triage": 1200, "location": 400, "title": 30, "age": 12}, 40)
    assert plan["title"] >= recall.MIN_PER_STAGE
    assert plan["age"] >= recall.MIN_PER_STAGE
    assert sum(plan.values()) <= 40


def test_a_stage_is_never_asked_for_more_than_it_has():
    plan = recall.plan_sample({"triage": 3, "location": 1}, 40)
    assert plan == {"triage": 3, "location": 1}


def test_triage_absorbs_the_budget_when_it_is_the_only_stage():
    assert recall.plan_sample({"triage": 500}, 40) == {"triage": 40}


def test_deterministic_stages_absorb_the_budget_when_triage_is_empty():
    plan = recall.plan_sample({"location": 100, "title": 100}, 40)
    assert sum(plan.values()) == 40
    assert "triage" not in plan


def test_stages_with_no_drops_are_left_out_entirely():
    plan = recall.plan_sample({"triage": 100, "location": 0}, 10)
    assert plan == {"triage": 10}


def test_nothing_to_sample_yields_an_empty_plan():
    assert recall.plan_sample({}, 40) == {}
    assert recall.plan_sample({"triage": 10}, 0) == {}


# --------------------------------------------------------------------------
# audit — the writes it must not make
# --------------------------------------------------------------------------

def test_audit_writes_no_verdicts_and_no_postings(db, monkeypatch):
    seed(db, 10)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: BODY)
    scores(monkeypatch, lambda i: 95)  # every one a screaming miss

    report = recall.audit(config())

    assert report.would_ping == 10
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM verdicts").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM notifications"
                            ).fetchone()["c"] == 0


def test_audited_rows_are_marked_and_never_sampled_again(db, monkeypatch):
    seed(db, 10)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: BODY)
    scores(monkeypatch, lambda i: 20)

    first = recall.audit(config())
    assert first.sampled == 10

    with store.connect(db) as conn:
        assert store.sample_drops(conn, "triage", 50) == []
        row = conn.execute("SELECT audited_at, audit_score FROM drops"
                           ).fetchone()
    assert row["audited_at"]
    assert row["audit_score"] == 20

    assert recall.audit(config()) is None


def test_below_min_drops_the_audit_does_not_run_at_all(db, monkeypatch):
    seed(db, 3)
    assert recall.audit(config(min_drops=20)) is None
    with store.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM drops WHERE audited_at IS NOT NULL"
        ).fetchone()["c"] == 0


def test_drops_outside_the_lookback_window_do_not_count_toward_the_minimum(
        db, monkeypatch):
    seed(db, 10)
    old = (dt.date.today() - dt.timedelta(days=60)).isoformat() + "T09:00:00"
    with store.connect(db) as conn:
        conn.execute("UPDATE drops SET last_seen_at = ?", (old,))

    assert recall.audit(config(min_drops=5, lookback_days=7)) is None


# --------------------------------------------------------------------------
# audit — the honest denominator
# --------------------------------------------------------------------------

def test_a_posting_that_cannot_be_refetched_counts_as_no_body(db, monkeypatch):
    seed(db, 6)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: "")
    monkeypatch.setattr(matcher, "run_json",
                        lambda *a, **k: pytest.fail("nothing was scorable"))

    report = recall.audit(config())

    assert report.sampled == 6
    assert report.no_body == 6
    assert report.scored == 0
    assert report.miss_rate == 0.0


def test_a_teaser_length_body_is_not_scored(db, monkeypatch):
    seed(db, 6)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: "short" * 10)
    monkeypatch.setattr(matcher, "run_json",
                        lambda *a, **k: pytest.fail("nothing was scorable"))

    report = recall.audit(config())
    assert report.no_body == 6


def test_a_degraded_batch_is_no_body_not_a_correct_drop(db, monkeypatch):
    from watcher.claude_cli import ClaudeError

    seed(db, 6)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: BODY)
    monkeypatch.setattr(matcher, "run_json", lambda *a, **k: (
        _ for _ in ()).throw(ClaudeError("upstream 503")))

    report = recall.audit(config())

    assert report.scored == 0
    assert report.no_body == 6
    # The fallback verdict is 45/maybe, which is above digest_threshold. If a
    # degraded result reached the counters it would show up right here.
    assert report.would_digest == 0
    assert report.would_ping == 0


def test_miss_rate_is_over_scored_not_over_sampled(db, monkeypatch):
    ids = seed(db, 10)
    monkeypatch.setattr(recall, "hydrate",
                        lambda p, t: BODY if p.detail_url.endswith(
                            tuple(f"{i}/detail" for i in range(5))) else "")
    scores(monkeypatch, lambda i: 90 if i == 0 else 10)

    report = recall.audit(config())

    assert report.sampled == 10
    assert report.scored + report.no_body == 10
    assert report.would_ping == 1
    assert report.miss_rate == pytest.approx(1 / report.scored)
    assert len(ids) == 10


# --------------------------------------------------------------------------
# audit — the two routes back to a description
# --------------------------------------------------------------------------

def no_detail_urls(db) -> None:
    """Strip the stored `detail_url`, the way most of the live table is.

    Greenhouse, Ashby and Personio inline the description in their list
    response, so the poller never records a detail URL for them — and between
    them they are over a third of the drops table.
    """
    with store.connect(db) as conn:
        conn.execute("UPDATE drops SET detail_url = NULL")


def test_a_drop_with_no_detail_url_is_scored_from_its_public_page(
        db, monkeypatch):
    """The defect this second route exists for: the first live audit hydrated
    1 of its 40 rows and called the other 39 `no_body` — honest, and useless.

    `hydrate` is deliberately left on the autouse backstop that fails when
    called: with no detail_url there is nothing to hand it, and reaching it
    anyway would be a request no provider can answer.
    """
    seed(db, 6)
    no_detail_urls(db)
    monkeypatch.setattr(recall, "get_text", lambda *a, **k: f"<div>{BODY}</div>")
    scores(monkeypatch, lambda i: 10)

    report = recall.audit(config())

    assert report.scored == 6
    assert report.no_body == 0


def test_a_shell_page_falls_through_to_the_browser(db, monkeypatch):
    """Ashby serves an empty shell over a 200, not an error — so the fallback
    has to trigger on length rather than on an exception."""
    seed(db, 6)
    no_detail_urls(db)
    monkeypatch.setattr(recall, "get_text",
                        lambda *a, **k: "<html><body><div id=root></div></body></html>")
    monkeypatch.setattr(recall.browser, "page_text", lambda *a, **k: BODY)
    scores(monkeypatch, lambda i: 10)

    report = recall.audit(config())

    assert report.scored == 6
    assert report.no_body == 0


def test_the_public_page_is_tried_after_a_hydrator_returns_a_teaser(
        db, monkeypatch):
    """A stored detail_url is not proof the hydrator still answers with a body.
    The public page is tried *after* it, not instead of it."""
    seed(db, 6)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: "too short")
    monkeypatch.setattr(recall, "get_text", lambda *a, **k: BODY)
    scores(monkeypatch, lambda i: 10)

    assert recall.audit(config()).scored == 6


def test_a_row_whose_every_route_raises_is_no_body_not_a_crash(db, monkeypatch):
    """A 404 is the *expected* case for anything older than a few weeks — the
    posting was filled or withdrawn. It costs that row, never the audit."""
    def boom(*a, **k):
        raise RuntimeError("410 Gone")

    seed(db, 5)
    monkeypatch.setattr(recall, "hydrate", boom)
    monkeypatch.setattr(recall, "get_text", boom)
    monkeypatch.setattr(recall.browser, "page_text", boom)
    monkeypatch.setattr(matcher, "run_json",
                        lambda *a, **k: pytest.fail("nothing was scorable"))

    report = recall.audit(config())

    assert report.sampled == 5
    assert report.no_body == 5
    assert report.scored == 0


def test_a_drop_with_no_url_left_is_no_body_without_a_request(db, monkeypatch):
    """Nothing to fetch is an unknown, not a fetch of the empty string."""
    seed(db, 6)
    with store.connect(db) as conn:
        conn.execute("UPDATE drops SET detail_url = NULL, url = '', "
                     "canonical_url = ''")
    monkeypatch.setattr(recall, "get_text",
                        lambda *a, **k: pytest.fail("fetched an empty URL"))
    monkeypatch.setattr(recall.browser, "page_text",
                        lambda *a, **k: pytest.fail("rendered an empty URL"))
    monkeypatch.setattr(matcher, "run_json",
                        lambda *a, **k: pytest.fail("nothing was scorable"))

    assert recall.audit(config()).no_body == 6


# --------------------------------------------------------------------------
# audit — the per-stage split, which is the actual finding
# --------------------------------------------------------------------------

def test_misses_are_attributed_to_the_stage_that_dropped_them(db, monkeypatch):
    with store.connect(db) as conn:
        for i in range(6):
            drop(conn, f"t{i}", stage="triage")
        for i in range(6):
            drop(conn, f"l{i}", stage="location")

    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: BODY)
    # Only the location drops score high — the finding must not smear across
    # both stages just because both were sampled.
    lookup: dict[str, int] = {}
    with store.connect(db) as conn:
        for row in conn.execute("SELECT id, stage FROM drops"):
            lookup[row["id"]] = 90 if row["stage"] == "location" else 10

    def fake(prompt, **kwargs):
        ids = [line.split("### id: ", 1)[1].strip()
               for line in prompt.splitlines() if line.startswith("### id: ")]
        return {"results": [{"id": i, "score": lookup[i], "verdict": "maybe",
                             "why": [], "gaps": []} for i in ids]}
    monkeypatch.setattr(matcher, "run_json", fake)

    report = recall.audit(config())

    assert report.by_stage["location"]["pinged"] == report.would_ping
    assert report.by_stage["triage"]["pinged"] == 0
    assert {m["stage"] for m in report.misses} == {"location"}


def test_misses_are_reported_best_first(db, monkeypatch):
    seed(db, 5)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: BODY)
    scores(monkeypatch, lambda i: 70 + i * 5)

    report = recall.audit(config())
    assert [m["score"] for m in report.misses] == sorted(
        [m["score"] for m in report.misses], reverse=True)


def test_the_population_is_every_drop_in_the_window_not_the_sample(
        db, monkeypatch):
    seed(db, 30)
    monkeypatch.setattr(recall, "hydrate", lambda *a, **k: BODY)
    scores(monkeypatch, lambda i: 10)

    report = recall.audit(config(sample_size=5))
    assert report.population == 30
    assert report.sampled == 5


# --------------------------------------------------------------------------
# the Telegram message
# --------------------------------------------------------------------------

def _report(**kwargs) -> recall.RecallReport:
    report = recall.RecallReport(population=1284, sampled=40, scored=36,
                                 no_body=4, would_ping=2, would_digest=6)
    report.by_stage.update({
        "triage": {"sampled": 24, "pinged": 2, "population": 1100},
        "location": {"sampled": 8, "pinged": 0, "population": 150},
        "title": {"sampled": 5, "pinged": 0, "population": 34},
    })
    report.misses = [{"score": 72, "company": "Zalando",
                      "title": "Applied Scientist, Search", "stage": "triage",
                      "reason": "", "url": "https://example.test/z"}]
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


def test_recall_message_leads_with_the_miss_rate():
    from watcher import notifier

    text = notifier.format_recall_audit(_report())
    assert "40 of 1,284 drops re-scored" in text
    assert "would have pinged: <b>2</b> (6%)" in text
    assert "would have made the digest: 6" in text
    assert "no description available: 4" in text


def test_recall_message_breaks_the_finding_out_by_stage():
    from watcher import notifier

    text = notifier.format_recall_audit(_report())
    assert "By stage" in text
    assert "triage" in text and "location" in text
    assert "Zalando" in text
    assert "Nothing was written." in text


def test_recall_message_survives_a_zero_sample_denominator():
    from watcher import notifier

    text = notifier.format_recall_audit(
        _report(scored=0, would_ping=0, would_digest=0, misses=[]))
    assert "(0%)" in text


def test_recall_message_caps_the_miss_list():
    from watcher import notifier

    many = [{"score": 90 - i, "company": f"Co{i}", "title": "Role",
             "stage": "triage", "reason": "", "url": ""} for i in range(9)]
    text = notifier.format_recall_audit(_report(misses=many, would_ping=9))
    assert f"+{9 - notifier.RECALL_TOP_MISSES} more" in text
    assert "Co8" not in text
