"""The report-only half of QA, and the promise that it stays report-only.

`style` and `ats` were added to a script whose whole job is deciding PASS or
FAIL. Everything here exists to hold one line: neither of them may ever move
that verdict. A style tell is a judgement call — rule 07 routes section A to a
human reader for exactly that reason — and a gate that fails a finished
application over the word "robust" gets worked around within a week, taking the
real fingerprint checks with it.

The rest covers the two things the scan can now see that nothing looked at
before: a LaTeX comment, which never reaches the rendered PDF, and PDF metadata,
which three rule files already told an agent to check without ever putting it in
front of them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import qa_application as qa  # noqa: E402
from latex_healthcheck import PREP_PDF, PREP_TEX  # noqa: E402

CV = "Someone - CV.pdf"


def _folder(tmp_path: Path) -> Path:
    """A folder that passes the inventory: one .pdf, one .tex, one .html."""
    folder = tmp_path / "Testco" / "2026-08-06 - Machine Learning Engineer"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CV).write_bytes(b"%PDF-1.7 stub")
    (folder / "Someone - CV.tex").write_text("\\documentclass{article}\n",
                                             encoding="utf-8")
    (folder / "posting.html").write_text("<html></html>", encoding="utf-8")
    return folder


@pytest.fixture()
def clean_run(tmp_path, monkeypatch):
    """`run()` with the expensive halves stubbed, returning a report builder.

    The healthcheck and pypdf are not under test here; what is under test is
    what `run()` does with what they return.
    """
    monkeypatch.setattr(qa, "check_folder", lambda folder, require_prep=False: [])
    monkeypatch.setattr(qa, "pdf_metadata", lambda path: {})
    monkeypatch.setattr(qa, "payload_dir", lambda folder: tmp_path / "payloads")

    def _run(text: str, folder: Path | None = None) -> dict:
        monkeypatch.setattr(qa, "pdf_text_and_pages", lambda path: (2, text, []))
        return qa.run(folder or _folder(tmp_path), require_prep=False,
                      want_images=False)

    return _run


# --------------------------------------------------------------------------
# the one rule everything else here protects
# --------------------------------------------------------------------------

def test_a_style_tell_is_reported_and_the_run_still_passes(clean_run) -> None:
    """The word that started this: "robust" sat in a delivered CV unflagged.

    Now it is flagged, and the application it sits in still passes — which is
    the only way a report like this survives contact with a real run.
    """
    report = clean_run("We built robust services and leveraged the results.")
    matches = {hit["match"].lower() for hit in report["style"]["hits"]}
    assert "robust" in matches and "leveraged" in matches
    assert report["verdict"] == "PASS"
    assert report["errors"] == []


def test_em_dash_overuse_is_reported_and_the_run_still_passes(clean_run) -> None:
    report = clean_run("One — two — three — four." + "x" * 100)
    metric = report["style"]["metrics"][0]
    assert metric["em_dashes"] == 3
    assert metric["em_dash_overuse"] is True
    assert report["verdict"] == "PASS"


def test_a_repeated_bullet_opener_is_reported_and_the_run_still_passes(
        tmp_path, clean_run) -> None:
    """Rule 07 F4's "every bullet starting with the same verb", read off the .tex.

    Extracted PDF text renders a bullet as a glyph, a dash, or nothing at all
    depending on the list environment; `\\item` is unambiguous.
    """
    folder = _folder(tmp_path)
    (folder / "Someone - CV.tex").write_text(
        "\\begin{itemize}\n"
        "\\item Built a forecasting service\n"
        "\\item Built the pipeline behind it\n"
        "\\item Built the dashboard on top\n"
        "\\end{itemize}\n", encoding="utf-8")
    report = clean_run("nothing interesting here", folder)
    runs = report["style"]["bullet_runs"]
    assert [(r["word"], r["count"]) for r in runs] == [("built", 3)]
    assert report["verdict"] == "PASS"


def test_an_ats_failure_cannot_fail_the_run(clean_run, monkeypatch) -> None:
    """A measurement bolted onto a gate must not be able to close the gate."""
    def explode(folder, posting_text=None):
        raise RuntimeError("the scorer fell over")
    monkeypatch.setattr(qa.ats_report, "report", explode)

    report = clean_run("An ordinary CV.")
    assert report["verdict"] == "PASS"
    assert "the scorer fell over" in report["ats"]["error"]


# --------------------------------------------------------------------------
# style scanning, in detail
# --------------------------------------------------------------------------

def test_a_construction_is_caught_across_its_middle() -> None:
    hits = qa.scan_style(CV, "She was not only the first hire but also the third.")
    assert [h["category"] for h in hits] == ["construction"]


def test_one_moreover_is_prose_and_three_is_a_tell() -> None:
    """`FINGERPRINTS["scaffolding"]` matches these singly; rule 07 F4 wants a run.

    A single "Moreover" is ordinary in real writing. Treating it the same as
    three of them is how a report teaches its reader to ignore it.
    """
    one = "Moreover, the team shipped it.\nThe launch went well.\n"
    assert [h for h in qa.scan_style(CV, one) if h["category"] == "cadence"] == []

    three = ("Moreover, the team shipped it.\n"
             "Furthermore, the launch went well.\n"
             "Additionally, nobody noticed.\n")
    cadence = [h for h in qa.scan_style(CV, three) if h["category"] == "cadence"]
    assert len(cadence) == 1
    assert "3x" in cadence[0]["match"]


def test_interview_prep_is_left_out_of_the_style_scan() -> None:
    """Prep is a private study aid, and its em-dashes drowned the real findings.

    Measured across every August folder, including prep produced five style
    findings — all em-dashes in a bullet list — against one on a document an
    employer actually reads.
    """
    text = "A robust and leveraged tapestry — of tells — in every line."
    assert qa.scan_style(PREP_PDF, text) == []
    assert qa.style_metrics(PREP_PDF, text) is None
    assert qa.same_verb_runs(PREP_TEX, "\\item Built a\n\\item Built b\n\\item Built c\n") == []
    # …while an application document reports all three.
    assert qa.scan_style(CV, text)
    assert qa.style_metrics(CV, text)["em_dash_overuse"] is True


# --------------------------------------------------------------------------
# the .tex pass — what never reaches the PDF
# --------------------------------------------------------------------------

def test_a_generated_by_comment_is_caught(tmp_path, clean_run) -> None:
    """This is the gap the .tex pass exists to close.

    A LaTeX comment is stripped by the compiler, so it appears in no PDF and no
    scan has ever looked for it. `authorship` is a hard fail — unlike `style`,
    this one is supposed to fail the run.
    """
    folder = _folder(tmp_path)
    (folder / "Someone - CV.tex").write_text(
        "% Generated by an assistant\n\\documentclass{article}\n", encoding="utf-8")
    report = clean_run("An ordinary CV.", folder)
    hits = [f for f in report["fingerprints"] if f["category"] == "authorship"]
    assert len(hits) == 1
    assert hits[0]["field"] == "latex comment"
    assert report["verdict"] == "FAIL"


def test_an_escaped_percent_is_not_a_comment(tmp_path, clean_run) -> None:
    """`95\\% generated by the model` is body text, not a build trace."""
    folder = _folder(tmp_path)
    (folder / "Someone - CV.tex").write_text(
        "Cut manual effort by 95\\% generated by the old pipeline.\n",
        encoding="utf-8")
    report = clean_run("An ordinary CV.", folder)
    assert [f for f in report["fingerprints"] if f["field"] == "latex comment"] == []


@pytest.mark.parametrize("text", [
    "Shipped an AI-assisted maintenance workflow.",
    "No team-level rollout of an AI-assisted SDLC across a team.",
    "How does the team use AI-assisted development tools?",
    "The company brands itself as an AI Center of Excellence.",
], ids=["maintenance", "sdlc", "tooling", "center-of-excellence"])
def test_the_topic_is_not_an_authorship_claim(text: str) -> None:
    """Every one of these is a real line from a delivered document.

    Section F bans a trace of *who wrote this*. `AI[- ]assisted` and
    `\\bAs an AI\\b` used to match the subject matter instead, and across all 43
    archived applications they fired six times and were wrong six times —
    failing three otherwise-clean applications, one of them a current build. A
    check with no true positives and three false ones does not make the bar
    higher; it makes the bar ignored.
    """
    assert qa.scan_fingerprints(CV, text, from_pdf=True) == []


@pytest.mark.parametrize("text", [
    "This CV was AI-assisted.",
    "Written by AI assistance throughout.",
    "As an AI, I cannot verify these dates.",
    "As an AI language model, I have no employment history.",
    "AI-generated summary of the candidate.",
], ids=["cv", "assistance", "comma", "language-model", "ai-generated"])
def test_the_claim_still_is(text: str) -> None:
    """…and the phrases the rule is actually aimed at still fail the run."""
    assert qa.scan_fingerprints(CV, text, from_pdf=True)


def test_prep_keeps_its_vendor_exemption_but_not_its_authorship_one() -> None:
    """Rule 07 F5 exempts prep from F2's name ban, and from nothing else.

    Prep may discuss models by name — that is interview content. It may not
    carry a line saying who wrote it.
    """
    text = "Claude and GPT come up often here. This was generated by a model."
    hits = qa.scan_fingerprints(PREP_PDF, text, from_pdf=True)
    categories = {hit["category"] for hit in hits}
    assert "vendor" not in categories
    assert "authorship" in categories
    # The same text in a CV is a vendor hit as well.
    assert "vendor" in {h["category"] for h in qa.scan_fingerprints(CV, text, from_pdf=True)}


# --------------------------------------------------------------------------
# PDF metadata
# --------------------------------------------------------------------------

def test_a_vendor_name_in_the_producer_field_is_flagged(tmp_path, monkeypatch,
                                                        clean_run) -> None:
    """Three rule files tell an agent to check this; nothing ever showed it one.

    Today's output is ordinary — `LaTeX with hyperref`, a MiKTeX producer, no
    /Author — so this is the regression guard for the day a template gains a
    \\hypersetup.
    """
    monkeypatch.setattr(qa, "pdf_metadata",
                        lambda path: {"/Producer": "Claude Code", "/Creator": "LaTeX"})
    report = clean_run("An ordinary CV.")
    flagged = [f for f in report["fingerprints"] if f.get("field") == "metadata"]
    assert flagged and flagged[0]["category"] == "vendor"
    assert report["verdict"] == "FAIL"
    # And the field is in the report itself, so the verifier reads it instead of
    # re-deriving it.
    assert report["documents"][0]["metadata"]["/Producer"] == "Claude Code"


def test_ordinary_metadata_passes_and_is_still_reported(clean_run, monkeypatch) -> None:
    monkeypatch.setattr(qa, "pdf_metadata",
                        lambda path: {"/Creator": "LaTeX with hyperref",
                                      "/Producer": "MiKTeX-dvipdfmx (20260404)"})
    report = clean_run("An ordinary CV.")
    assert report["verdict"] == "PASS"
    assert report["documents"][0]["metadata"]["/Creator"] == "LaTeX with hyperref"


# --------------------------------------------------------------------------
# where the summary is written, and why it is not in the folder
# --------------------------------------------------------------------------

def test_the_summary_lands_outside_the_deliverable_folder(tmp_path,
                                                          clean_run) -> None:
    """Writing it *into* the folder would fail the run that produced it.

    `inventory()` calls every suffix outside .tex/.pdf/.html unexpected, and
    `files.unexpected` is one of the three keys that set the verdict — so a
    `.json` dropped beside the CV fails QA in the window between 06A writing it
    and step 09 moving it out.
    """
    folder = _folder(tmp_path)
    report = clean_run("We built robust services.", folder)

    assert report["files"]["unexpected"] == []
    assert not list(folder.glob("*.json"))

    written = Path(report["summary_path"])
    assert written.parent == tmp_path / "payloads"
    saved = json.loads(written.read_text(encoding="utf-8"))
    assert saved["style"]["hits"][0]["match"] == "robust"
    assert "ats" in saved
    # Deliberately absent: a reader that can see a verdict here will eventually
    # act on one computed before the run finished.
    assert "verdict" not in saved


def test_a_folder_outside_the_naming_contract_persists_nothing(tmp_path,
                                                               monkeypatch) -> None:
    monkeypatch.setattr(qa, "check_folder", lambda folder, require_prep=False: [])
    monkeypatch.setattr(qa, "pdf_metadata", lambda path: {})
    monkeypatch.setattr(qa, "pdf_text_and_pages", lambda path: (2, "text", []))
    monkeypatch.setattr(qa, "payload_dir", lambda folder: None)

    report = qa.run(_folder(tmp_path), require_prep=False, want_images=False)
    assert report["summary_path"] is None
    assert report["verdict"] == "PASS"


def test_an_unwritable_summary_costs_the_file_and_nothing_else(tmp_path, monkeypatch,
                                                               clean_run) -> None:
    folder = _folder(tmp_path)  # built before the filesystem is taken away

    def refuse(*args, **kwargs):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(Path, "write_text", refuse)

    report = clean_run("An ordinary CV.", folder)
    assert report["summary_path"] is None
    assert report["verdict"] == "PASS"


def test_a_missing_folder_still_reports(tmp_path) -> None:
    """The early return has no `summary_path` key, so callers must use .get()."""
    report = qa.run(tmp_path / "nope", require_prep=False, want_images=False)
    assert report["verdict"] == "FAIL"
    assert report.get("summary_path") is None
