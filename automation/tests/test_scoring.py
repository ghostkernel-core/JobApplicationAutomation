"""The judgement half: the CLI wrapper, the profile digest, scoring, and the kb.

`test_degraded_scoring.py` already pins down what happens to a posting the model
could not judge. This covers everything around that — how the prompt is built,
how a well-formed but sloppy response is normalised, and how the two files that
grow over time (the digest cache and profile_kb.md) are written.

The common thread is that a model is on the other end of all of it. Every
response is treated as untrusted input: a score outside 0-100, a verdict label
that disagrees with its own number, a missing result, a section name the
proposal was told not to touch. None of those may reach the database or the kb
file as given, and none of them may raise either — a scoring cycle that throws
stops the watcher for that poll, and the postings it was judging go quiet.
"""

from __future__ import annotations

import datetime as dt
import json
import runpy
import subprocess

import pytest

from watcher import claude_cli, kb, matcher, profile, store
from watcher.claude_cli import ClaudeError


# ===========================================================================
# claude_cli
# ===========================================================================

class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture()
def cli(monkeypatch):
    """Capture what would have been run, and hand back a scripted result."""
    calls: list[dict] = []
    scripted: list = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        result = scripted.pop(0) if scripted else Completed(stdout="ok")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(claude_cli, "resolve_bin", lambda name: f"/bin/{name}")
    return type("CLI", (), {"calls": calls, "scripted": scripted})()


def test_the_prompt_goes_on_stdin_never_on_argv(cli) -> None:
    """Windows caps a command line at ~32k and a batch of postings exceeds it."""
    claude_cli.run("x" * 50_000)
    call = cli.calls[0]
    assert len(call["input"]) == 50_000
    assert not any(len(str(part)) > 100 for part in call["cmd"])


def test_a_call_runs_outside_the_workspace_by_default(cli, tmp_path) -> None:
    """Claude Code loads CLAUDE.md from the cwd, and the matcher does not want
    the whole application pipeline prepended to every scoring batch."""
    claude_cli.run("hello")
    assert "JobApplicationAutomation" not in cli.calls[0]["cwd"]

    claude_cli.run("hello", cwd=tmp_path)
    assert cli.calls[1]["cwd"] == str(tmp_path)


def test_tools_are_forbidden_unless_asked_for(cli) -> None:
    claude_cli.run("hello")
    assert cli.calls[0]["cmd"][-2:] == ["--allowed-tools", ""]

    claude_cli.run("hello", allowed_tools="Read")
    assert cli.calls[1]["cmd"][-2:] == ["--allowed-tools", "Read"]

    claude_cli.run("hello", allowed_tools=None)
    assert "--allowed-tools" not in cli.calls[2]["cmd"]


def test_the_model_is_passed_through_with_a_cheap_default(cli) -> None:
    claude_cli.run("hello")
    assert "haiku" in cli.calls[0]["cmd"]

    claude_cli.run("hello", model="sonnet")
    assert "sonnet" in cli.calls[1]["cmd"]


def test_the_answer_comes_back_stripped(cli) -> None:
    cli.scripted.append(Completed(stdout="  the answer  \n"))
    assert claude_cli.run("hello") == "the answer"


def test_a_non_zero_exit_names_the_real_error(cli) -> None:
    cli.scripted.append(Completed(returncode=1, stdout="",
                                  stderr="claude.ai connectors are disabled\n"
                                         "Error: credit balance too low"))
    with pytest.raises(ClaudeError, match="credit balance"):
        claude_cli.run("hello")


def test_a_timeout_says_how_long_it_waited(cli) -> None:
    cli.scripted.append(subprocess.TimeoutExpired("claude", 180))
    with pytest.raises(ClaudeError, match="timed out after 180s"):
        claude_cli.run("hello", timeout=180)


def test_a_missing_cli_says_what_is_missing(cli) -> None:
    cli.scripted.append(FileNotFoundError("no such file"))
    with pytest.raises(ClaudeError, match="Claude Code CLI"):
        claude_cli.run("hello")


def test_resolving_the_binary_applies_pathext(monkeypatch, tmp_path) -> None:
    """On Windows the npm shim is `claude.CMD` and bare "claude" cannot be spawned."""
    claude_cli.resolve_bin.cache_clear()
    monkeypatch.setattr(claude_cli.shutil, "which",
                        lambda name: f"C:/npm/{name}.CMD" if name == "claude" else None)
    assert claude_cli.resolve_bin("claude").endswith(".CMD")

    claude_cli.resolve_bin.cache_clear()
    with pytest.raises(ClaudeError, match="not found on PATH"):
        claude_cli.resolve_bin("nope")

    claude_cli.resolve_bin.cache_clear()
    real = tmp_path / "claude.exe"
    real.write_text("", encoding="utf-8")
    assert claude_cli.resolve_bin(str(real)) == str(real.resolve())
    claude_cli.resolve_bin.cache_clear()


@pytest.mark.parametrize("response", [
    '{"results": []}',
    'Here you go:\n```json\n{"results": []}\n```',
    'Sure!\n{"results": []}\nHope that helps.',
    '```\n{"results": []}\n```',
])
def test_json_is_recovered_from_whatever_the_model_wrapped_it_in(response) -> None:
    assert claude_cli.extract_json(response) == {"results": []}


def test_a_response_with_no_json_raises_so_the_caller_can_retry() -> None:
    with pytest.raises(ClaudeError, match="no JSON in response"):
        claude_cli.extract_json("I cannot help with that.")
    with pytest.raises(ClaudeError):
        claude_cli.extract_json("")


def test_run_json_retries_once_on_a_malformed_response(cli, caplog) -> None:
    cli.scripted += [Completed(stdout="not json at all"),
                     Completed(stdout='{"results": [1]}')]
    assert claude_cli.run_json("hello") == {"results": [1]}
    assert len(cli.calls) == 2


def test_run_json_gives_up_and_raises_the_last_error(cli) -> None:
    cli.scripted += [Completed(stdout="nope"), Completed(stdout="still nope")]
    with pytest.raises(ClaudeError, match="no JSON"):
        claude_cli.run_json("hello")
    assert len(cli.calls) == 2

    cli.scripted.append(Completed(stdout="nope"))
    with pytest.raises(ClaudeError):
        claude_cli.run_json("hello", retries=0)


# ===========================================================================
# profile digest
# ===========================================================================

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A canonical profile, a digest path, and a database, all disposable."""
    canonical = tmp_path / "00-canonical-profile.md"
    canonical.write_text("# Profile\n" + "fact. " * 200, encoding="utf-8")
    digest_path = tmp_path / "profile_digest.md"
    kb_path = tmp_path / "profile_kb.md"
    db = tmp_path / "watch.db"

    monkeypatch.setattr(profile, "CANONICAL_PROFILE_PATH", canonical)
    monkeypatch.setattr(profile, "PROFILE_DIGEST_PATH", digest_path)
    monkeypatch.setattr(profile, "PROFILE_KB_PATH", kb_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    store.init_db(db)
    return type("WS", (), {"canonical": canonical, "digest": digest_path,
                           "kb": kb_path, "db": db})()


def test_the_digest_is_generated_once_and_then_cached(workspace, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(profile, "run",
                        lambda prompt, **kw: calls.append(prompt) or "d" * 300)

    assert profile.get_digest() == "d" * 300
    assert profile.get_digest() == "d" * 300
    assert len(calls) == 1
    assert "fact." in calls[0], "the canonical profile is what gets condensed"


def test_editing_the_canonical_profile_regenerates_the_digest(
        workspace, monkeypatch) -> None:
    """Keyed on size and mtime, so nothing else has to remember to invalidate."""
    calls = []
    monkeypatch.setattr(profile, "run",
                        lambda prompt, **kw: calls.append(1) or "d" * 300)
    profile.get_digest()

    workspace.canonical.write_text("# Profile\n" + "new fact. " * 200,
                                   encoding="utf-8")
    profile.get_digest()
    assert len(calls) == 2


def test_the_digest_can_be_forced(workspace, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(profile, "run",
                        lambda prompt, **kw: calls.append(1) or "d" * 300)
    profile.get_digest()
    profile.get_digest(force=True)
    assert len(calls) == 2


def test_a_suspiciously_short_digest_is_refused(workspace, monkeypatch) -> None:
    """Silently caching a one-line digest would quietly break every later batch."""
    monkeypatch.setattr(profile, "run", lambda prompt, **kw: "n/a")
    with pytest.raises(RuntimeError, match="too short"):
        profile.get_digest()
    assert not workspace.digest.exists()


def test_a_missing_canonical_profile_says_where_it_should_be(
        workspace, monkeypatch) -> None:
    workspace.canonical.unlink()
    with pytest.raises(FileNotFoundError, match="canonical profile not found"):
        profile.get_digest()


def test_the_learned_preferences_are_optional(workspace) -> None:
    assert profile.get_kb() == ""
    workspace.kb.write_text("## Prefer\n- remote\n", encoding="utf-8")
    assert "remote" in profile.get_kb()


# ===========================================================================
# matcher — prompt building
# ===========================================================================

def row(**over) -> matcher._RowLike:
    fields = {"id": "p1", "title": "Machine Learning Engineer",
              "company": "Example GmbH", "location": "Berlin, Germany",
              "country": "DE", "remote": 0, "description": "Build models."}
    fields.update(over)
    return matcher._RowLike(**fields)


def test_a_posting_is_rendered_with_the_id_the_model_must_echo() -> None:
    text = matcher._render_posting(row(), 4000)
    assert "### id: p1" in text
    assert "Machine Learning Engineer — Example GmbH" in text
    assert "Berlin, Germany" in text


def test_a_long_description_is_truncated_visibly() -> None:
    text = matcher._render_posting(row(description="x" * 9000), 4000)
    assert "…truncated]" in text
    assert len(text) < 4400


def test_html_in_a_stored_description_never_reaches_the_model() -> None:
    """Boards hand back markup; a prompt full of it wastes the batch's tokens
    on `<div class=…>` and reads worse to the model than the text alone."""
    text = matcher._render_posting(
        row(description="<p>Build <b>models</b> &amp; ship them.</p>"), 4000)
    assert "<" not in text.split("\n", 1)[1]
    assert "Build" in text and "models" in text and "& ship them." in text


def test_the_rank_is_recomputed_rather_than_read_from_the_row() -> None:
    """This batch may be re-scoring an older row whose description arrived
    later, and archived calibration postings have no such columns at all."""
    text = matcher._render_posting(
        row(title="Senior Data Scientist",
            description="5+ years of experience required."), 4000)
    assert "senior · asks 5+ yrs" in text


def test_a_posting_with_nothing_to_read_still_renders() -> None:
    text = matcher._render_posting(
        row(description="", location=None, country=None), 4000)
    assert "unknown" in text
    assert "(no description available)" in text


def test_remote_is_marked_in_the_rendered_posting() -> None:
    assert "· remote" in matcher._render_posting(row(remote=1), 4000)


def test_the_profile_context_is_sent_once_per_batch_not_per_posting() -> None:
    prompt = matcher.build_prompt([row(id="a"), row(id="b")], "DIGEST", "KB", 4000)
    assert prompt.count("DIGEST") == 1
    assert prompt.count("### id:") == 2


def test_an_empty_kb_is_labelled_rather_than_left_blank() -> None:
    assert "(none yet)" in matcher.build_prompt([row()], "DIGEST", "", 4000)


# ===========================================================================
# matcher — normalising the response
# ===========================================================================

def test_a_well_formed_result_passes_through() -> None:
    assert matcher._coerce({
        "id": "p1", "score": 82, "verdict": "strong",
        "why": ["python", "nlp"], "gaps": ["no k8s"],
        "stop_and_ask": False, "stop_reason": None,
    }) == {"score": 82, "verdict": "strong", "why": ["python", "nlp"],
           "gaps": ["no k8s"], "stop_and_ask": False, "stop_reason": None}


@pytest.mark.parametrize("score,expected", [
    (150, 100), (-10, 0), ("82", 82), ("high", 0), (None, 0), (82.7, 82),
])
def test_a_score_is_clamped_into_range(score, expected) -> None:
    assert matcher._coerce({"score": score})["score"] == expected


@pytest.mark.parametrize("score,expected", [(85, "strong"), (55, "maybe"), (10, "no")])
def test_a_label_that_disagrees_with_its_own_number_loses(score, expected) -> None:
    """They disagree occasionally; the number is what the thresholds compare."""
    assert matcher._coerce({"score": score, "verdict": "excellent"}
                           )["verdict"] == expected


def test_a_valid_label_is_kept_even_at_an_odd_score() -> None:
    assert matcher._coerce({"score": 55, "verdict": "STRONG "}
                           )["verdict"] == "strong"


def test_the_phrase_lists_are_capped_and_cleaned() -> None:
    coerced = matcher._coerce({
        "why": ["a", "  ", "b", "c", "d", "e"],
        "gaps": "one string, not a list",
    })
    assert coerced["why"] == ["a", "b", "c"], "three phrases fit a phone notification"
    assert coerced["gaps"] == ["one string, not a list"]


def test_a_missing_field_is_not_an_error() -> None:
    coerced = matcher._coerce({})
    assert coerced["why"] == [] and coerced["gaps"] == []
    assert coerced["stop_and_ask"] is False and coerced["stop_reason"] is None


def test_a_stop_reason_only_survives_alongside_something_to_say() -> None:
    assert matcher._coerce({"stop_and_ask": True, "stop_reason": " clearance "}
                           )["stop_reason"] == "clearance"
    assert matcher._coerce({"stop_and_ask": True, "stop_reason": ""}
                           )["stop_reason"] is None


# ===========================================================================
# matcher — scoring a batch
# ===========================================================================

@pytest.fixture()
def scorer(monkeypatch):
    """Stand in for the model, and record the prompts it was given.

    The digest and the knowledge base are stubbed alongside it. Both are read
    from `rules/`, which is git-ignored and absent from a fresh checkout, and
    `get_digest` regenerates through the CLI when it finds the cache stale —
    so a test that reached the real one would either raise or spend five
    minutes in a subprocess, depending on whose machine it ran on.
    """
    state = type("S", (), {"responses": [], "prompts": []})()

    def fake_run_json(prompt, **kwargs):
        state.prompts.append(prompt)
        result = state.responses.pop(0) if state.responses else {"results": []}
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(matcher, "run_json", fake_run_json)
    monkeypatch.setattr(matcher.profile, "get_digest", lambda *a, **k: "D")
    monkeypatch.setattr(matcher.profile, "get_kb", lambda *a, **k: "K")
    return state


class Cfg:
    match_model = "haiku"
    match_timeout = 180
    description_chars = 4000
    batch_size = 8
    max_score_attempts = 3


def test_a_batch_comes_back_keyed_by_posting_id(scorer) -> None:
    scorer.responses.append({"results": [
        {"id": "a", "score": 80, "verdict": "strong"},
        {"id": "b", "score": 20, "verdict": "no"},
    ]})
    out = matcher.score_batch([row(id="a"), row(id="b")], "D", "K", Cfg())
    assert out["a"]["score"] == 80 and out["b"]["verdict"] == "no"
    assert not any(v.get("degraded") for v in out.values())


def test_a_failed_call_degrades_every_posting_rather_than_raising(scorer) -> None:
    """A parsing failure should cost a nudge in the evening digest, never a
    silently missed role."""
    scorer.responses.append(ClaudeError("exit 1: overloaded"))
    out = matcher.score_batch([row(id="a"), row(id="b")], "D", "K", Cfg())
    assert set(out) == {"a", "b"}
    assert all(v["verdict"] == "maybe" and v["score"] == 45 for v in out.values())
    assert all("overloaded" in v["degraded"] for v in out.values())


@pytest.mark.parametrize("response", [
    {"results": "not a list"}, {"no_results_key": 1}, ["a list, not an object"],
])
def test_a_response_of_the_wrong_shape_degrades_the_batch(scorer, response) -> None:
    scorer.responses.append(response)
    out = matcher.score_batch([row(id="a")], "D", "K", Cfg())
    assert out["a"]["degraded"]


def test_a_posting_the_model_skipped_degrades_only_itself(scorer) -> None:
    scorer.responses.append({"results": [{"id": "a", "score": 80}]})
    out = matcher.score_batch([row(id="a"), row(id="b")], "D", "K", Cfg())
    assert not out["a"].get("degraded")
    assert "no result returned" in out["b"]["degraded"]


def test_results_for_postings_that_were_not_asked_about_are_ignored(scorer) -> None:
    scorer.responses.append({"results": [
        {"id": "a", "score": 80}, {"id": "invented", "score": 99},
        "not even an object", {"score": 50},          # no id
    ]})
    out = matcher.score_batch([row(id="a")], "D", "K", Cfg())
    assert set(out) == {"a"}


def test_a_large_run_is_split_into_batches(scorer) -> None:
    rows = [row(id=f"p{i}") for i in range(9)]
    scorer.responses += [
        {"results": [{"id": f"p{i}", "score": 50} for i in range(8)]},
        {"results": [{"id": "p8", "score": 50}]},
    ]
    verdicts, report = matcher.score_postings(rows, Cfg(), persist=False)
    assert len(scorer.prompts) == 2
    assert len(verdicts) == 9 and report.scored == 9


def test_the_report_counts_each_band_and_the_failures(scorer, monkeypatch) -> None:
    monkeypatch.setattr(profile, "get_digest", lambda: "D")
    monkeypatch.setattr(profile, "get_kb", lambda: "K")
    scorer.responses.append({"results": [
        {"id": "a", "score": 85}, {"id": "b", "score": 50}, {"id": "c", "score": 10},
    ]})
    rows = [row(id=i) for i in ("a", "b", "c", "d")]
    _, report = matcher.score_postings(rows, Cfg(), persist=False)

    assert report.by_band == {"strong": 1, "maybe": 2, "no": 1}
    assert report.scored == 4 and report.failed == 1


def test_scoring_nothing_costs_no_model_call(scorer, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(profile, "get_digest", lambda: called.append(1) or "D")
    verdicts, report = matcher.score_postings([], Cfg(), persist=False)
    assert verdicts == {} and report.scored == 0
    assert not called, "the digest is not even fetched for an empty run"


def test_a_fresh_report_starts_at_zero_in_every_band() -> None:
    assert matcher.MatchReport().by_band == {"strong": 0, "maybe": 0, "no": 0}


# ===========================================================================
# matcher — the scheduled path
# ===========================================================================

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    monkeypatch.setattr(matcher.store, "init_db", lambda *a: None)
    return path


def add(path, posting_id: str, title: str = "ML Engineer") -> str:
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO postings
               (id, loose_key, source, provider, url, canonical_url, company,
                title, description, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (posting_id, f"key-{posting_id}", "portal:test", "test",
             f"https://example.test/{posting_id}",
             f"https://example.test/{posting_id}", "ExampleCo", title,
             "Build models.", "2026-08-04T09:00:00+00:00"),
        )
    return posting_id


def test_the_scheduled_pass_scores_and_stores_what_is_pending(
        db, scorer, monkeypatch) -> None:
    monkeypatch.setattr(profile, "get_digest", lambda: "D")
    monkeypatch.setattr(profile, "get_kb", lambda: "K")
    add(db, "a")
    scorer.responses.append({"results": [{"id": "a", "score": 88,
                                          "verdict": "strong"}]})

    report = matcher.match_pending(Cfg())

    assert report.scored == 1
    with store.connect(db) as conn:
        assert store.get_verdict(conn, "a")["score"] == 88
        assert store.unscored(conn) == []


def test_nothing_pending_is_not_a_model_call(db, scorer, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(profile, "get_digest", lambda: called.append(1) or "D")
    assert matcher.match_pending(Cfg()).scored == 0
    assert not called


def test_a_degraded_posting_survives_the_scheduled_pass_unscored(
        db, scorer, monkeypatch) -> None:
    monkeypatch.setattr(profile, "get_digest", lambda: "D")
    monkeypatch.setattr(profile, "get_kb", lambda: "K")
    add(db, "a")
    scorer.responses.append(ClaudeError("overloaded"))

    report = matcher.match_pending(Cfg())

    assert report.failed == 1 and report.deferred == 1
    with store.connect(db) as conn:
        assert [r["id"] for r in store.unscored(conn)] == ["a"]


# ===========================================================================
# matcher — the calibration set
# ===========================================================================

def archive(root, company: str, folder: str, html: str | None = "<p>A posting.</p>"):
    path = root / "2026" / company / folder
    path.mkdir(parents=True, exist_ok=True)
    if html is not None:
        (path / "posting.html").write_text(html, encoding="utf-8")
    return path


def test_the_archived_postings_are_the_ground_truth_set(tmp_path) -> None:
    """Each was judged worth a full application, so the matcher scoring one
    below the notify threshold is the failure `--calibrate` exists to catch."""
    archive(tmp_path, "Example GmbH", "2026-07-01 - Data Scientist")
    rows = matcher.archived_postings(root=tmp_path)

    assert len(rows) == 1
    assert rows[0]["company"] == "Example GmbH"
    assert rows[0]["title"] == "Data Scientist"
    assert rows[0]["id"] == "applied:Example GmbH:Data Scientist"
    assert "A posting." in rows[0]["description"]


def test_the_most_recent_applications_come_first_and_the_limit_holds(
        tmp_path) -> None:
    for day in range(1, 6):
        archive(tmp_path, "Example", f"2026-07-0{day} - Role {day}")
    rows = matcher.archived_postings(limit=2, root=tmp_path)
    assert [r["title"] for r in rows] == ["Role 5", "Role 4"]


def test_a_folder_with_no_archived_posting_is_skipped(tmp_path) -> None:
    archive(tmp_path, "Example", "2026-07-01 - No Capture", html=None)
    archive(tmp_path, "Example", "not-an-application")
    (tmp_path / "2026" / "Example" / "loose.txt").write_text("x", encoding="utf-8")
    (tmp_path / "2026" / "stray.txt").write_text("x", encoding="utf-8")
    (tmp_path / "master").mkdir()
    assert matcher.archived_postings(root=tmp_path) == []


def test_the_posting_text_is_read_past_the_inlined_assets(tmp_path) -> None:
    """SingleFile inlines every image, so the text can sit past the first
    megabyte of base64 — truncating the HTML first yields an empty body."""
    padding = "<img src='data:image/png;base64," + "A" * 200_000 + "'>"
    archive(tmp_path, "Example", "2026-07-01 - Role",
            html=f"<html>{padding}<p>The actual requirements.</p></html>")
    rows = matcher.archived_postings(root=tmp_path)
    assert "The actual requirements." in rows[0]["description"]


# ===========================================================================
# matcher — CLI
# ===========================================================================

def test_replay_scores_without_saving(db, scorer, monkeypatch, capsys) -> None:
    monkeypatch.setattr(profile, "get_digest", lambda: "D")
    monkeypatch.setattr(profile, "get_kb", lambda: "K")
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    Cfg.notify_threshold, Cfg.digest_threshold = 70, 40
    add(db, "a")
    scorer.responses.append({"results": [{"id": "a", "score": 88, "verdict": "strong",
                                          "why": ["python"], "gaps": ["k8s"],
                                          "stop_and_ask": True,
                                          "stop_reason": "clearance"}]})

    assert matcher.main(["--replay", "5"]) == 0
    out = capsys.readouterr().out
    assert "88 strong" in out
    assert "+ python" in out and "- k8s" in out and "! clearance" in out
    assert "notify >= 70" in out

    with store.connect(db) as conn:
        assert store.get_verdict(conn, "a") is None, "--replay must not persist"


def test_replay_as_json(db, scorer, monkeypatch, capsys) -> None:
    monkeypatch.setattr(profile, "get_digest", lambda: "D")
    monkeypatch.setattr(profile, "get_kb", lambda: "K")
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    add(db, "a")
    scorer.responses.append({"results": [{"id": "a", "score": 60}]})

    assert matcher.main(["--replay", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["a"]["score"] == 60


def test_replay_on_an_empty_database_says_what_to_run(db, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    assert matcher.main(["--replay", "5"]) == 1
    assert "watcher.poll" in capsys.readouterr().out


def test_calibrate_names_the_applications_that_would_have_been_missed(
        db, scorer, monkeypatch, capsys) -> None:
    monkeypatch.setattr(profile, "get_digest", lambda: "D")
    monkeypatch.setattr(profile, "get_kb", lambda: "K")
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    Cfg.notify_threshold = 70
    monkeypatch.setattr(matcher, "archived_postings",
                        lambda n: [row(id="hit"), row(id="miss")])
    scorer.responses.append({"results": [{"id": "hit", "score": 88},
                                         {"id": "miss", "score": 30}]})

    assert matcher.main(["--calibrate"]) == 0
    out = capsys.readouterr().out
    assert "1 would have been notified, 1 would have been missed" in out
    assert "tune profile_kb.md" in out


def test_calibrate_with_no_archive_says_so(db, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    monkeypatch.setattr(matcher, "archived_postings", lambda n: [])
    assert matcher.main(["--calibrate"]) == 1
    assert "No archived postings" in capsys.readouterr().out


def test_rescore_degraded_lists_what_it_cleared(db, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    add(db, "a", title="Broken Posting")
    with store.connect(db) as conn:
        store.save_verdict(conn, "a", {"score": 45, "verdict": "maybe", "why": [],
                                       "gaps": [store.DEGRADED_GAP],
                                       "stop_and_ask": False, "stop_reason": None},
                           "haiku")

    assert matcher.main(["--rescore-degraded"]) == 0
    out = capsys.readouterr().out
    assert "Broken Posting" in out and "Cleared 1" in out
    with store.connect(db) as conn:
        assert [r["id"] for r in store.unscored(conn)] == ["a"]


def test_rescore_degraded_on_a_healthy_database_says_nothing_to_do(
        db, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    assert matcher.main(["--rescore-degraded"]) == 0
    assert "nothing to re-score" in capsys.readouterr().out


def test_pending_runs_the_scheduled_path(db, monkeypatch) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    called = []
    monkeypatch.setattr(matcher, "match_pending", lambda cfg: called.append(cfg))
    assert matcher.main(["--pending"]) == 0
    assert called


def test_refresh_digest_prints_and_exits(db, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    monkeypatch.setattr(profile, "get_digest", lambda force: "the digest")
    assert matcher.main(["--refresh-digest"]) == 0
    assert "the digest" in capsys.readouterr().out


def test_no_arguments_shows_the_help_rather_than_doing_something(
        db, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matcher, "load_config", lambda: Cfg())
    assert matcher.main([]) == 1
    assert "--calibrate" in capsys.readouterr().out


def test_the_verb_spelling_of_the_command_reaches_the_same_main(
        monkeypatch) -> None:
    """`python -m watcher.match` is documented in the README, so it has to
    keep working even though the logic lives under the noun."""
    monkeypatch.setattr(matcher, "main", lambda argv=None: 7)
    with pytest.raises(SystemExit) as exit_code:
        runpy.run_module("watcher.match", run_name="__main__")
    assert exit_code.value.code == 7


# ===========================================================================
# kb — the immediate, no-model half
# ===========================================================================

@pytest.fixture()
def kb_files(tmp_path, monkeypatch):
    kb_path = tmp_path / "profile_kb.md"
    kb_path.write_text(
        "# Matching knowledge\n\n"
        "## Hard filters\n\n- no unpaid work\n\n"
        "## Prefer\n\n- remote-friendly teams\n\n"
        "## Avoid\n\n- pure devops\n",
        encoding="utf-8")
    monkeypatch.setattr(kb, "PROFILE_KB_PATH", kb_path)
    monkeypatch.setattr(kb, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(kb, "KB_PROPOSAL_PATH", tmp_path / "kb_proposal.json")
    return type("KB", (), {"kb": kb_path,
                           "decisions": tmp_path / "decisions.jsonl",
                           "proposal": tmp_path / "kb_proposal.json"})()


def test_a_decision_is_appended_to_the_raw_record(kb_files) -> None:
    kb.log_decision({"id": "p1", "company": "Example", "title": "DS", "score": 80},
                    "approved", "good fit")
    kb.log_decision({"id": "p2"}, "skipped", "")

    lines = kb_files.decisions.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["action"] == "approved" and first["note"] == "good fit"
    assert first["at"]


def test_a_reasoned_decision_is_written_verbatim(kb_files) -> None:
    """The file only ever gains lines the user actually typed."""
    assert kb.append_note("Example", "DS", "skipped", " too much devops ") is True
    text = kb_files.kb.read_text(encoding="utf-8")
    assert "too much devops" in text
    assert "Example — DS" in text
    assert dt.date.today().isoformat() in text
    assert "## Prefer" in text and "- remote-friendly teams" in text


def test_a_bare_yes_teaches_nothing_and_is_not_recorded(kb_files) -> None:
    assert kb.append_note("Example", "DS", "approved", "") is False
    assert kb.append_note("Example", "DS", "approved", "   ") is False
    assert "Example" not in kb_files.kb.read_text(encoding="utf-8")


def test_the_learned_section_is_created_if_it_is_missing(kb_files) -> None:
    kb.append_note("Example", "DS", "skipped", "reason")
    assert "## Learned from decisions" in kb_files.kb.read_text(encoding="utf-8")


def test_a_missing_kb_file_is_a_warning_not_a_crash(kb_files, caplog) -> None:
    kb_files.kb.unlink()
    assert kb.append_note("Example", "DS", "skipped", "reason") is False
    assert kb.read_kb() == ""


# ===========================================================================
# kb — proposal state
# ===========================================================================

def test_a_pending_proposal_survives_a_restart(kb_files) -> None:
    """The request and the reply are on opposite sides of a process restart."""
    assert kb.pending() is None
    kb.save_pending({"proposals": [{"section": "Prefer", "text": "x"}]}, 42)

    got = kb.pending()
    assert got["message_id"] == 42
    assert got["proposals"][0]["text"] == "x"


def test_the_mark_moves_on_a_decline_as_well_as_an_approval(kb_files) -> None:
    """A rejected proposal must not be regenerated verbatim next week."""
    kb.save_pending({"consumed_through": "2026-08-01T00:00:00"}, 1)
    kb.clear_pending("2026-08-01T00:00:00")

    assert kb.pending() is None
    kb_files.decisions.write_text(
        json.dumps({"at": "2026-07-30T00:00:00", "note": "old"}) + "\n"
        + json.dumps({"at": "2026-08-02T00:00:00", "note": "new"}) + "\n",
        encoding="utf-8")
    assert [d["note"] for d in kb.unconsumed()] == ["new"]


def test_clearing_without_a_mark_leaves_the_high_water_mark_alone(kb_files) -> None:
    kb.clear_pending("2026-08-01T00:00:00")
    kb.save_pending({"x": 1}, 2)
    kb.clear_pending()
    assert json.loads(kb_files.proposal.read_text(encoding="utf-8")
                      )["consumed_through"] == "2026-08-01T00:00:00"


def test_an_unreadable_state_file_starts_from_empty(kb_files, caplog) -> None:
    kb_files.proposal.write_text("{not json", encoding="utf-8")
    assert kb.pending() is None
    kb_files.proposal.write_text('"a string, not an object"', encoding="utf-8")
    assert kb.pending() is None


def test_a_state_file_holding_a_junk_proposal_reports_none(kb_files) -> None:
    kb_files.proposal.write_text('{"pending": "not an object"}', encoding="utf-8")
    assert kb.pending() is None


def test_decisions_are_read_newest_last_and_bad_lines_skipped(kb_files) -> None:
    kb_files.decisions.write_text(
        json.dumps({"at": "1", "note": "a"}) + "\n"
        + "{ broken\n"
        + json.dumps({"at": "2", "note": "b"}) + "\n",
        encoding="utf-8")
    assert [d["note"] for d in kb.recent_decisions()] == ["a", "b"]
    assert kb.recent_decisions(limit=1)[0]["note"] == "b"


def test_no_decisions_file_yet_is_an_empty_list(kb_files) -> None:
    assert kb.recent_decisions() == []
    assert kb.unconsumed() == []


# ===========================================================================
# kb — the weekly proposal
# ===========================================================================

class KbCfg:
    kb_model = "sonnet"
    kb_timeout = 180
    kb_lookback = 60
    kb_min_decisions = 3


def write_decisions(path, count: int, note: str = "too much devops") -> None:
    path.write_text("".join(
        json.dumps({"at": f"2026-08-0{i + 1}T00:00:00", "action": "skipped",
                    "company": "Example", "title": "DS", "score": 50,
                    "note": note}) + "\n"
        for i in range(count)), encoding="utf-8")


@pytest.fixture()
def proposer(monkeypatch):
    state = type("S", (), {"response": {"proposals": [], "summary": ""},
                           "prompts": []})()

    def fake_run_json(prompt, **kwargs):
        state.prompts.append(prompt)
        return state.response

    monkeypatch.setattr(claude_cli, "run_json", fake_run_json)
    return state


def test_a_quiet_week_proposes_nothing_without_a_model_call(
        kb_files, proposer) -> None:
    write_decisions(kb_files.decisions, 2)
    assert kb.propose(KbCfg()) is None
    assert not proposer.prompts


def test_decisions_with_no_reason_are_not_evidence_of_anything(
        kb_files, proposer) -> None:
    write_decisions(kb_files.decisions, 5, note="")
    assert kb.propose(KbCfg()) is None
    assert not proposer.prompts


def test_a_proposal_carries_the_mark_it_consumed_up_to(kb_files, proposer) -> None:
    write_decisions(kb_files.decisions, 4)
    proposer.response = {
        "proposals": [{"section": "avoid", "text": "Skip pure devops roles",
                       "because": "three skips said so"}],
        "summary": "one rule",
    }

    got = kb.propose(KbCfg())

    assert got["proposals"] == [{"section": "Avoid",
                                 "text": "Skip pure devops roles",
                                 "because": "three skips said so"}]
    assert got["consumed_through"] == "2026-08-04T00:00:00"
    assert got["reviewed"] == 4
    assert "current profile_kb.md" in proposer.prompts[0]
    assert "too much devops" in proposer.prompts[0]


def test_a_proposal_for_a_section_it_may_not_touch_is_dropped(
        kb_files, proposer) -> None:
    """The model wanting to edit `Hard filters` is exactly the case this must
    not quietly satisfy — those are calibrated by hand."""
    write_decisions(kb_files.decisions, 4)
    proposer.response = {"proposals": [
        {"section": "Hard filters", "text": "reject everything"},
        {"section": "Prefer", "text": ""},
        {"section": "Prefer", "text": "Remote-first teams"},
        "not even an object",
    ], "summary": "s"}

    assert [p["text"] for p in kb.propose(KbCfg())["proposals"]
            ] == ["Remote-first teams"]


def test_a_response_with_no_proposals_is_still_a_result(kb_files, proposer) -> None:
    write_decisions(kb_files.decisions, 4)
    proposer.response = {"summary": "nothing worth generalising this week"}
    got = kb.propose(KbCfg())
    assert got["proposals"] == []
    assert "nothing worth" in got["summary"]


# ===========================================================================
# kb — applying an approved proposal
# ===========================================================================

def test_approved_bullets_land_in_their_own_sections_and_are_marked(
        kb_files) -> None:
    written = kb.apply_proposal({"proposals": [
        {"section": "Prefer", "text": "Remote-first teams"},
        {"section": "Avoid", "text": "Pure devops"},
    ]})

    text = kb_files.kb.read_text(encoding="utf-8")
    assert written == 2
    prefer = text.index("## Prefer")
    avoid = text.index("## Avoid")
    assert prefer < text.index("Remote-first teams") < avoid
    assert avoid < text.index("Pure devops")
    assert f"<!-- proposed {dt.date.today().isoformat()} -->" in text


def test_the_hand_written_bullets_above_are_untouched(kb_files) -> None:
    kb.apply_proposal({"proposals": [{"section": "Prefer", "text": "New rule"}]})
    text = kb_files.kb.read_text(encoding="utf-8")
    assert text.index("- remote-friendly teams") < text.index("New rule")
    assert "## Hard filters" in text and "- no unpaid work" in text


def test_a_bullet_lands_in_the_last_section_too(kb_files) -> None:
    """`Avoid` runs to end of file, which is the case with no closing heading."""
    kb.apply_proposal({"proposals": [{"section": "Avoid", "text": "Last rule"}]})
    lines = kb_files.kb.read_text(encoding="utf-8").rstrip().splitlines()
    assert lines[-1].startswith("- Last rule")
    assert lines[-2] == "- pure devops"


def test_an_empty_proposal_writes_nothing(kb_files) -> None:
    before = kb_files.kb.read_text(encoding="utf-8")
    assert kb.apply_proposal({"proposals": []}) == 0
    assert kb.apply_proposal({}) == 0
    assert kb_files.kb.read_text(encoding="utf-8") == before


def test_applying_to_a_kb_that_is_not_there_is_a_no_op(kb_files) -> None:
    kb_files.kb.unlink()
    assert kb.apply_proposal({"proposals": [{"section": "Prefer", "text": "x"}]}) == 0


def test_a_kb_missing_the_section_refuses_rather_than_guessing(kb_files) -> None:
    kb_files.kb.write_text("# Matching knowledge\n\n## Prefer\n\n- a\n",
                           encoding="utf-8")
    with pytest.raises(ValueError, match="no '## Avoid' section"):
        kb.apply_proposal({"proposals": [{"section": "Avoid", "text": "x"}]})


# ===========================================================================
# kb — CLI
# ===========================================================================

def test_the_cli_dry_run_writes_nothing(kb_files, proposer, monkeypatch,
                                        capsys) -> None:
    monkeypatch.setattr("watcher.config.load_config", lambda: KbCfg())
    write_decisions(kb_files.decisions, 4)
    proposer.response = {"proposals": [{"section": "Prefer", "text": "x"}],
                         "summary": "s"}
    before = kb_files.kb.read_text(encoding="utf-8")

    assert kb.main(["--propose"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and '"Prefer"' in out
    assert kb_files.kb.read_text(encoding="utf-8") == before


def test_the_cli_reports_a_quiet_week(kb_files, proposer, monkeypatch,
                                      capsys) -> None:
    monkeypatch.setattr("watcher.config.load_config", lambda: KbCfg())
    assert kb.main(["--propose"]) == 0
    assert "nothing to review" in capsys.readouterr().out


def test_the_cli_shows_what_is_awaiting_a_reply(kb_files, capsys) -> None:
    kb.save_pending({"summary": "waiting on you"}, 7)
    assert kb.main(["--pending"]) == 0
    assert "waiting on you" in capsys.readouterr().out
