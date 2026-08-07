"""The discovery gate: title/company/location only, and safe to get wrong.

This replaced `title_allow`, a hand-written substring list that decided what
was ever *seen*, ahead of the profile that decides what is worth applying to
— and was never measured against it. Triage asks the same question against
the actual profile instead, but only after the free deterministic rules in
prefilter.py have already run, and only on the three fields that cost nothing
to have read wrong.

Everything here follows from one asymmetry: an `unsure` costs one description
read later, for free, downstream. A wrong `drop` costs the job outright, and
nobody ever sees it happen. So the tests below exist to pin down every way
that asymmetry could quietly invert — a malformed decision string resolving
to drop instead of unsure, a failed batch persisting its fallback, a
suppressed repeat re-judged under a profile that has since changed, a cap
silently discarding the postings it was supposed to defer.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from watcher import store, triage
from watcher.claude_cli import ClaudeError
from watcher.config import Config
from watcher.normalize import Posting


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    store.init_db(path)
    return path


def posting(job_id: str, title: str = "Data Scientist", company: str = "Acme") -> Posting:
    return Posting(source="ats", provider="greenhouse", source_job_id=job_id,
                   url=f"https://example.test/{job_id}", company=company,
                   title=f"{title} {job_id}", location="Berlin", country="DE")


def config(**triage_overrides) -> Config:
    return Config(triage={"batch_size": 200, "max_per_cycle": 600, **triage_overrides})


@pytest.fixture(autouse=True)
def profile_fixed(monkeypatch):
    """Every test gets a fixed digest/kb unless it overrides them itself."""
    monkeypatch.setattr(triage.profile, "get_digest", lambda *a, **k: "DIGEST")
    monkeypatch.setattr(triage.profile, "get_kb", lambda: "KB")


# --------------------------------------------------------------------------
# score_batch: fail-open, never a manufactured drop
# --------------------------------------------------------------------------

def test_claude_error_degrades_every_item_to_unsure_never_drop(monkeypatch):
    postings = [posting("1"), posting("2"), posting("3")]
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: (_ for _ in ()).throw(
        ClaudeError("upstream 503")))

    results = triage.score_batch(postings, "DIGEST", "KB", config())

    for p in postings:
        assert results[p.fingerprint]["decision"] == "unsure"
        assert results[p.fingerprint]["degraded"]


def test_missing_id_in_response_degrades_only_that_posting(monkeypatch):
    postings = [posting("1"), posting("2")]
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: {
        "results": [{"id": postings[0].fingerprint, "decision": "drop", "why": "wrong field"}]})

    results = triage.score_batch(postings, "DIGEST", "KB", config())

    assert results[postings[0].fingerprint]["decision"] == "drop"
    assert "degraded" not in results[postings[0].fingerprint]
    assert results[postings[1].fingerprint]["decision"] == "unsure"
    assert results[postings[1].fingerprint]["degraded"]


@pytest.mark.parametrize("raw_decision", ["maybe", "KEEP!", None, 42, "Drop", "  drop  ", ""])
def test_unrecognised_decision_resolves_to_unsure(monkeypatch, raw_decision):
    """Only the exact string "drop" drops. Everything else, however close it
    looks, is unsure — never manufactured into a drop or silently ignored."""
    p = posting("1")
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: {
        "results": [{"id": p.fingerprint, "decision": raw_decision, "why": ""}]})

    results = triage.score_batch([p], "DIGEST", "KB", config())

    assert results[p.fingerprint]["decision"] == "unsure"
    assert "degraded" not in results[p.fingerprint]


def test_exact_keep_is_preserved_not_collapsed_to_unsure(monkeypatch):
    p = posting("1")
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: {
        "results": [{"id": p.fingerprint, "decision": "keep", "why": ""}]})

    results = triage.score_batch([p], "DIGEST", "KB", config())

    assert results[p.fingerprint]["decision"] == "keep"


# --------------------------------------------------------------------------
# the prompt: title/company/location only, never a description
# --------------------------------------------------------------------------

def test_prompt_carries_title_company_location_never_a_description():
    p = Posting(source="ats", provider="greenhouse", source_job_id="1",
                url="https://example.test/1", company="Acme GmbH",
                title="Senior Data Scientist", location="Cologne", country="DE",
                description="SECRET-BODY-TEXT should never appear in a triage prompt")

    prompt = triage.build_prompt([p], "DIGEST-TEXT", "KB-TEXT")

    assert "Senior Data Scientist" in prompt
    assert "Acme GmbH" in prompt
    assert "Cologne" in prompt
    assert "DIGEST-TEXT" in prompt
    assert "KB-TEXT" in prompt
    assert "SECRET-BODY-TEXT" not in prompt


# --------------------------------------------------------------------------
# triage_postings: persistence, suppression, and the cap
# --------------------------------------------------------------------------

def test_degraded_batch_writes_zero_drop_rows(db, monkeypatch):
    postings = [posting("1"), posting("2")]
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: (_ for _ in ()).throw(
        ClaudeError("boom")))

    results, report = triage.triage_postings(postings, config(), persist=True)

    assert report.degraded == 2
    assert report.dropped == 0
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM drops").fetchone()["c"] == 0


def test_only_the_drop_decision_is_persisted(db, monkeypatch):
    postings = [posting("1"), posting("2"), posting("3")]
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: {"results": [
        {"id": postings[0].fingerprint, "decision": "drop", "why": "different field"},
        {"id": postings[1].fingerprint, "decision": "keep", "why": ""},
        {"id": postings[2].fingerprint, "decision": "unsure", "why": ""},
    ]})

    triage.triage_postings(postings, config(), persist=True)

    with store.connect(db) as conn:
        rows = conn.execute("SELECT id FROM drops").fetchall()
    assert [r["id"] for r in rows] == [postings[0].fingerprint]


def test_known_drops_suppression_issues_zero_calls_on_repeat(db, monkeypatch):
    p = posting("1")
    calls = {"n": 0}

    def fake_run_json(*a, **k):
        calls["n"] += 1
        return {"results": [{"id": p.fingerprint, "decision": "drop", "why": "x"}]}

    monkeypatch.setattr(triage, "run_json", fake_run_json)
    triage.triage_postings([p], config(), persist=True)
    assert calls["n"] == 1

    results, report = triage.triage_postings([p], config(), persist=True)

    assert calls["n"] == 1, "second triage must not call the model at all"
    assert report.suppressed == 1
    assert report.judged == 0
    assert results[p.fingerprint]["decision"] == "drop"
    assert results[p.fingerprint]["suppressed"]


def test_changed_digest_invalidates_suppression(db, monkeypatch):
    p = posting("1")
    calls = {"n": 0}

    def fake_run_json(*a, **k):
        calls["n"] += 1
        return {"results": [{"id": p.fingerprint, "decision": "drop", "why": "x"}]}

    monkeypatch.setattr(triage, "run_json", fake_run_json)
    monkeypatch.setattr(triage.profile, "get_digest", lambda *a, **k: "DIGEST-OLD")
    triage.triage_postings([p], config(), persist=True)
    assert calls["n"] == 1

    monkeypatch.setattr(triage.profile, "get_digest", lambda *a, **k: "DIGEST-NEW")
    results, report = triage.triage_postings([p], config(), persist=True)

    assert calls["n"] == 2, "a changed digest must force a real re-judgement"
    assert report.suppressed == 0
    assert report.judged == 1


def test_changed_kb_invalidates_suppression(db, monkeypatch):
    p = posting("1")
    calls = {"n": 0}

    def fake_run_json(*a, **k):
        calls["n"] += 1
        return {"results": [{"id": p.fingerprint, "decision": "drop", "why": "x"}]}

    monkeypatch.setattr(triage, "run_json", fake_run_json)
    monkeypatch.setattr(triage.profile, "get_kb", lambda: "KB-OLD")
    triage.triage_postings([p], config(), persist=True)
    assert calls["n"] == 1

    monkeypatch.setattr(triage.profile, "get_kb", lambda: "KB-NEW")
    results, report = triage.triage_postings([p], config(), persist=True)

    assert calls["n"] == 2, "a changed kb must force a real re-judgement"
    assert report.suppressed == 0
    assert report.judged == 1


def test_dry_run_writes_no_drop_rows(db, monkeypatch):
    postings = [posting("1"), posting("2")]
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: {"results": [
        {"id": p.fingerprint, "decision": "drop", "why": "x"} for p in postings]})

    results, report = triage.triage_postings(postings, config(), persist=False)

    assert report.dropped == 0
    assert results[postings[0].fingerprint]["decision"] == "drop"
    with store.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM drops").fetchone()["c"] == 0


def test_cap_defers_rather_than_storing(db, monkeypatch):
    postings = [posting(str(i)) for i in range(5)]
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: {"results": [
        {"id": p.fingerprint, "decision": "drop", "why": "x"} for p in postings]})

    results, report = triage.triage_postings(postings, config(max_per_cycle=3),
                                              persist=True)

    assert report.judged == 3
    assert report.deferred == 2
    for p in postings[:3]:
        assert p.fingerprint in results
    for p in postings[3:]:
        assert p.fingerprint not in results
    with store.connect(db) as conn:
        rows = conn.execute("SELECT id FROM drops").fetchall()
    assert len(rows) == 3, "only the judged postings may ever reach the table"


def test_triage_all_covers_a_backlog_larger_than_one_cap(db, monkeypatch):
    postings = [posting(str(i)) for i in range(7)]

    def fake_run_json(prompt, **k):
        ids = [line.split("id: ", 1)[1] for line in prompt.splitlines()
               if line.startswith("### id: ")]
        return {"results": [{"id": i, "decision": "keep", "why": ""} for i in ids]}

    monkeypatch.setattr(triage, "run_json", fake_run_json)

    results, total = triage._triage_all(postings, config(max_per_cycle=3), persist=False)

    assert len(results) == 7
    assert total.deferred == 0
    assert total.judged == 7


def test_empty_input_returns_empty_without_touching_the_database(db, monkeypatch):
    monkeypatch.setattr(triage, "run_json", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call the model for an empty batch")))

    results, report = triage.triage_postings([], config(), persist=True)

    assert results == {}
    assert report.judged == 0
