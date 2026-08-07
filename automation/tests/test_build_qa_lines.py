"""The ATS and style lines on the build-report path, and how they fail.

These two lines are decoration on the message that tells the user their
application is ready. The message is not decoration. Everything here exists to
pin one property: when the scorer produces nothing, or nonsense, or its module
will not even import, the notification is byte-for-byte the one that was sent
before any of this existed.

That is not a hypothetical. `read_qa_summary` imports `qa_application` and
`clean_deliverable`, both of which read `identity.toml` at import time — so the
build most likely to hit the failure path is a build that failed *because* the
workspace is incomplete, which is exactly the build the user most needs to hear
about.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from watcher.builder import DONE, Outcome, qa_lines, read_qa_summary, \
    ready_message, result_message  # noqa: E402

SUMMARY = {
    "folder": "2026-08-06 - Machine Learning Engineer",
    "ats": {
        "brief": {"coverage": 0.7391, "matched": [], "missing": [],
                  "total": 23, "source": "match_brief.md"},
        "posting": {"coverage": 0.66, "matched": [], "total": 25,
                    "missing": ["MLflow", "feature engineering", "statistics",
                                "model deployment"],
                    "source": "posting.html"},
    },
    "style": {
        "hits": [{"document": "CV.pdf", "category": "cliche", "match": "robust",
                  "context": "…robust services…"}],
        "metrics": [], "bullet_runs": [],
    },
}


def _folder(tmp_path: Path, payload: object | str | None = None) -> str:
    """A build folder, optionally holding a summary file.

    Named so `describe_folder` cannot parse it, which sends `payload_dir` to
    `None` and `read_qa_summary` to its second candidate — the folder itself.
    That keeps the test off the real `_tmp/payloads/` tree, which is shared with
    the live workspace.
    """
    folder = tmp_path / "build"
    folder.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (folder / "qa_summary.json").write_text(text, encoding="utf-8")
    return str(folder)


def _outcome(folder: str, **kwargs) -> Outcome:
    return Outcome(status=DONE, folder=folder,
                   documents=("CV.pdf", "Cover Letter.pdf"), **kwargs)


def _log() -> Path:
    return Path("watcher-2026-08-06.log")


# --------------------------------------------------------------------------
# the lines, when there is something to say
# --------------------------------------------------------------------------

def test_both_numbers_are_reported_separately(tmp_path) -> None:
    """Two numbers, never averaged into one.

    The brief number is coverage of keywords step 01A judged truthfully usable;
    the posting number is a proxy over the raw ad. Merging them would hide which
    one moved, and only one of the two is trustworthy enough to act on.
    """
    line = qa_lines(_folder(tmp_path, SUMMARY))[0]
    assert line.startswith("ATS 74% brief · 66% posting")
    assert "missing: MLflow, feature engineering, statistics +1 more" in line


def test_the_style_line_names_the_tell_rather_than_counting_it(tmp_path) -> None:
    """A bare count sends the user hunting through two PDFs."""
    assert qa_lines(_folder(tmp_path, SUMMARY))[1] == \
        '⚠ 1 style tell: "robust" (CV)'


def test_a_summary_with_nothing_to_report_adds_no_lines(tmp_path) -> None:
    quiet = {"folder": "x",
             "ats": {"brief": None, "posting": None, "skipped": {}},
             "style": {"hits": [], "metrics": [], "bullet_runs": []}}
    assert qa_lines(_folder(tmp_path, quiet)) == []


def test_the_ready_message_carries_them(tmp_path) -> None:
    text = ready_message("<b>Roche</b>", _outcome(_folder(tmp_path, SUMMARY)))
    assert "ATS 74% brief · 66% posting" in text
    assert '"robust" (CV)' in text
    # …without displacing what the message is actually for.
    assert "Application ready" in text
    assert "still rendering" in text


def test_the_completion_message_carries_them_only_when_nothing_announced(
        tmp_path) -> None:
    """Repeating both numbers a minute later is noise: neither can have moved.

    Both are measured off the CV, which was final before the ready message went
    out.
    """
    folder = _folder(tmp_path, SUMMARY)
    announced = result_message("<b>Roche</b>",
                               _outcome(folder, announced=True), _log())
    assert "ATS" not in announced

    silent = result_message("<b>Roche</b>", _outcome(folder), _log())
    assert "ATS 74% brief · 66% posting" in silent


# --------------------------------------------------------------------------
# every way it can fail, and the one thing that must not change
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    None,                                   # no summary written at all
    "{ this is not json",                   # half-flushed or truncated
    '["not", "an", "object"]',              # valid JSON, wrong shape
    '{"ats": "66%", "style": 3}',           # right keys, wrong types
    '{"ats": {"brief": {}}, "style": {"hits": ["robust"]}}',  # right types, wrong depth
], ids=["absent", "malformed", "wrong-shape", "wrong-types", "wrong-depth"])
def test_a_broken_summary_leaves_the_message_untouched(tmp_path, payload) -> None:
    """The load-bearing test. Compared against a folder with no summary at all,
    which is what every one of these must degrade to.
    """
    baseline = ready_message("<b>Roche</b>", _outcome(_folder(tmp_path / "a")))
    text = ready_message("<b>Roche</b>",
                         _outcome(_folder(tmp_path / "b", payload)))
    assert text.replace(str(tmp_path / "b"), str(tmp_path / "a")) == baseline


def test_a_scorer_that_will_not_import_costs_nothing(tmp_path, monkeypatch) -> None:
    """The failure mode the local imports exist for.

    `ats_report` reads `identity.toml` at import time, so on an incomplete
    workspace this raises — during the report of the build that failed for that
    same reason.
    """
    monkeypatch.setitem(sys.modules, "ats_report", None)  # import → TypeError
    folder = _folder(tmp_path, SUMMARY)
    assert qa_lines(folder) == ['⚠ 1 style tell: "robust" (CV)']
    assert "Application ready" in ready_message("<b>Roche</b>", _outcome(folder))


def test_an_empty_folder_string_reads_nothing(tmp_path) -> None:
    """A failed build has no folder, and `to_absolute("")` is the workspace root."""
    assert read_qa_summary("") == {}


# --------------------------------------------------------------------------
# where it looks
# --------------------------------------------------------------------------

def test_the_payload_directory_wins_over_the_deliverable_folder(tmp_path,
                                                                monkeypatch) -> None:
    """One lookup serves both messages, which straddle step 09's cleanup.

    The summary is written to `_tmp/payloads/…` and never moves, so the archive
    copy is the authority; a `qa_summary.json` sitting in the deliverable folder
    is a leftover from a build that predates that decision.
    """
    import clean_deliverable

    archive = tmp_path / "payloads"
    archive.mkdir()
    (archive / "qa_summary.json").write_text(
        json.dumps({"style": {"hits": [{"document": "CV.pdf",
                                        "match": "leveraged"}]}}),
        encoding="utf-8")
    monkeypatch.setattr(clean_deliverable, "payload_dir", lambda folder: archive)

    stale = {"style": {"hits": [{"document": "CV.pdf", "match": "robust"}]}}
    assert qa_lines(_folder(tmp_path, stale)) == \
        ['⚠ 1 style tell: "leveraged" (CV)']
