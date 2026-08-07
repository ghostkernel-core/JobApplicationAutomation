"""Keyword coverage of a built CV, and the ways it must refuse to guess.

`ats_report.py` answers one question — would a machine reading this CV find the
words this job asks for — and the whole design risk is in the answers it must
*not* give. A missing Match Brief is not zero per cent. A saved posting page that
turned out to hold no posting is not zero per cent. A word the PDF text
extractor split down the middle is not a missing skill. Each of those, reported
as a number, would be worse than reporting nothing: the number looks like
measurement and reads like fact.

So most of what is pinned here is silence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ats_report  # noqa: E402

# A CV the way pypdf hands it over: mostly right, with the kerning splits this
# extractor produces on every document it has ever read. The name is invented —
# no fixture in this repo carries a real person, and CI scans for it.
CV_TEXT = """
Alex Morgan — Machine Learning Engineer

Skills: Python, PyT orch, scikit-learn, SQL, Docker, Kubernetes, AWS,
Kafka, Spark, model deployment, feature engineering.

Built and shipped production ML services, owned data pipelines end to end,
and ran experiment tracking across a team of four.
"""

# A job ad long enough to clear MIN_POSTING_CHARS, because a page shorter than
# that is a cookie banner rather than a posting and is deliberately not scored.
JOB_DESCRIPTION = """About the role

We are hiring a Machine Learning Engineer to own the models that rank and
recommend across our marketplace. You will work with the product teams to take
machine learning models from a notebook to a service that carries real traffic.

Your responsibilities:
- Build and deploy machine learning models to production
- Own data pipelines and the experiment tracking that keeps them honest
- Work with Python, PyTorch and SQL day to day
- Monitor model deployment and retrain when the data shifts
- Partner with product on what the machine learning models should optimise for

Your profile:
- 3+ years of experience with machine learning models in production
- Strong Python and SQL, comfortable owning data pipelines
- Experience with Kubernetes, Docker and model deployment
- You have run experiment tracking on a real project, not just a tutorial
- Comfortable with the statistics behind the models you ship

Nice to have:
- MLflow or a comparable experiment tracking tool
- Kafka, Spark or another streaming stack

We offer a friendly team, real ownership of what you ship, and a budget for the
conferences you actually want to attend.
"""


def _folder(tmp_path: Path, brief: str | None = None) -> Path:
    """An application folder with a CV file and optionally a Match Brief.

    Named `<Company>/<date> - <Role>` because `describe_folder` reads the
    company off the parent directory — a bare tmp_path would hand the report a
    pytest fixture name as the employer.

    The PDF is a stub. Every test that needs its text patches the extractor,
    because what is under test here is the parsing, not pypdf.
    """
    folder = tmp_path / "Testco" / "2026-08-06 - Machine Learning Engineer"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Someone - CV.pdf").write_bytes(b"%PDF-1.7 stub")
    if brief is not None:
        (folder / "match_brief.md").write_text(brief, encoding="utf-8")
    return folder


@pytest.fixture()
def extracted(monkeypatch):
    """Patch the PDF text extractor. Returns a setter for the text."""
    def _set(text: str) -> None:
        monkeypatch.setattr(ats_report, "pdf_text_and_pages",
                            lambda path: (2, text, []))
    _set(CV_TEXT)
    return _set


# --------------------------------------------------------------------------
# what "not measured" has to look like
# --------------------------------------------------------------------------

def test_no_brief_reports_nothing_rather_than_zero(tmp_path, extracted) -> None:
    """Four of nine archived runs have no brief. None of them scored 0%."""
    data = ats_report.report(_folder(tmp_path))
    assert data["brief"] is None
    assert "no Match Brief" in data["skipped"]["brief"]
    assert ats_report.summary_line(data) == ""


def test_a_brief_with_no_keyword_section_says_so(tmp_path, extracted) -> None:
    folder = _folder(tmp_path, "# Match Brief\n\n## Fit\n\nStrong.\n")
    data = ats_report.report(folder)
    assert data["brief"] is None
    assert "Top ATS keywords" in data["skipped"]["brief"]


def test_a_page_that_holds_no_posting_is_not_scored(tmp_path, extracted) -> None:
    """Four Stepstone captures saved the site chrome and none of the ad.

    The posting body is rendered client-side, so it is genuinely absent from the
    archive. There is no parsing fix for that — only the refusal to publish a
    percentage derived from a navigation bar.
    """
    folder = _folder(tmp_path)
    (folder / "posting.html").write_text(
        "<html><body>Job finden Job posten Login</body></html>", encoding="utf-8")
    data = ats_report.report(folder)
    assert data["posting"] is None
    assert "--posting-text" in data["skipped"]["posting"]


def test_a_cv_with_no_text_layer_stops_before_scoring(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ats_report, "pdf_text_and_pages", lambda path: (2, "", []))
    data = ats_report.report(_folder(tmp_path, "## Top ATS keywords\n\nPython, SQL\n"))
    assert data["brief"] is None and data["posting"] is None
    assert "no extractable text" in data["skipped"]["cv"]


def test_a_missing_folder_returns_a_report_not_an_exception(tmp_path) -> None:
    data = ats_report.report(tmp_path / "does not exist")
    assert data["cv"] is None
    assert data["skipped"]["cv"]


# --------------------------------------------------------------------------
# finding the CV
# --------------------------------------------------------------------------

def test_the_name_from_identity_wins(tmp_path, extracted) -> None:
    """Both files end in `CV.pdf`, and only one belongs to this workspace."""
    folder = _folder(tmp_path)
    (folder / f"{ats_report.load_identity().file_prefix} - CV.pdf").write_bytes(
        b"%PDF-1.7 stub")
    assert ats_report.report(folder)["cv"].startswith(
        ats_report.load_identity().file_prefix)


@pytest.mark.parametrize("boom", [
    FileNotFoundError("identity.toml is missing"),
    ValueError("identity.toml: [person] is missing or unfilled: email"),
], ids=["absent", "unfilled"])
def test_an_unusable_identity_falls_back_to_the_shape(tmp_path, extracted,
                                                      boom) -> None:
    """Coverage is an optional report; the identity file is not optional at all.

    Reading it at import time is what made this module unimportable wherever
    there is no `identity.toml` — a fresh clone, and every CI checkout, the file
    being untracked by design. It took an unrelated test file down with it. The
    name is a convenience here: what makes a PDF the CV is that it is the CV.
    """
    def refuse(*_args, **_kwargs):
        raise boom
    ats_report.load_identity.cache_clear()
    try:
        monkey = pytest.MonkeyPatch()
        monkey.setattr(ats_report, "load_identity", refuse)
        assert ats_report.report(_folder(tmp_path))["cv"] == "Someone - CV.pdf"
        monkey.undo()
    finally:
        ats_report.load_identity.cache_clear()


def test_a_german_run_is_found_by_its_lebenslauf(tmp_path, extracted) -> None:
    """`doc_name("CV")` is the English name; a German run renders neither."""
    folder = _folder(tmp_path)
    (folder / "Someone - CV.pdf").unlink()
    (folder / "Someone - Lebenslauf.pdf").write_bytes(b"%PDF-1.7 stub")
    assert ats_report.report(folder)["cv"] == "Someone - Lebenslauf.pdf"


# --------------------------------------------------------------------------
# the brief, in every shape the archive holds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("heading", [
    "## Top ATS keywords (truthfully usable)",
    "## Top ATS keywords (truthful)",
    "## Top ATS keywords (truthful, from approved stack)",
    "## Top ATS Keywords",
])
def test_every_observed_heading_suffix_is_found(tmp_path, extracted, heading) -> None:
    """The suffix drifts run to run; anchoring on the whole line found nothing."""
    folder = _folder(tmp_path, f"{heading}\n\nPython, SQL, Docker\n")
    data = ats_report.report(folder)
    assert data["brief"] is not None
    assert data["brief"]["total"] == 3


def test_a_bracketed_comma_stays_inside_its_term(tmp_path, extracted) -> None:
    """`production ML delivery (Docker, CI/CD)` is one keyword, not two.

    Split naively it became `production ML delivery (Docker` and `CI/CD)`,
    neither of which can match any CV ever written — two guaranteed misses
    manufactured by the parser.
    """
    folder = _folder(tmp_path,
                     "## Top ATS keywords\n\nPython; production ML delivery "
                     "(Docker, CI/CD); SQL\n")
    data = ats_report.report(folder)
    assert data["brief"]["total"] == 3
    assert "production ML delivery (Docker, CI/CD)" in (
        data["brief"]["matched"] + data["brief"]["missing"])


def test_the_substitution_note_is_not_read_as_skills(tmp_path, extracted) -> None:
    """One brief welds the vendor-substitution note onto the keyword paragraph.

    Filtering by paragraph threw away all twenty of that run's keywords;
    filtering by sentence keeps them and drops the note.
    """
    folder = _folder(tmp_path,
                     "## Top ATS keywords\n\nPython, SQL, Docker. No AI-vendor "
                     "name appears anywhere in this posting, so no substitution "
                     "was needed.\n")
    keywords = ats_report.brief_keywords(folder / "match_brief.md")
    assert keywords == ["Python", "SQL", "Docker"]


def test_only_the_first_paragraph_is_the_list(tmp_path, extracted) -> None:
    """Prose under the heading is commentary, and comma-splitting prose is noise."""
    folder = _folder(tmp_path,
                     "## Top ATS keywords\n\nPython, SQL\n\n"
                     "Use vendor-neutral phrasing, since the posting names a "
                     "framework the profile does not carry.\n")
    assert ats_report.brief_keywords(folder / "match_brief.md") == ["Python", "SQL"]


# --------------------------------------------------------------------------
# the extractor's own damage
# --------------------------------------------------------------------------

def test_a_kerning_split_is_rescued_and_declared(tmp_path, extracted) -> None:
    """pypdf reads `PyTorch` as `PyT orch` in every CV this workspace has built.

    poppler reads it correctly, so it is an artifact of the tool doing the
    scanning, not a defect in the document. Reporting PyTorch missing would be a
    false alarm about the single most load-bearing word on the CV — so it counts
    as matched, and the rescue is stated out loud rather than hidden.
    """
    folder = _folder(tmp_path, "## Top ATS keywords\n\nPyTorch, SQL\n")
    data = ats_report.report(folder)
    assert "PyTorch" in data["brief"]["matched"]
    assert "PyTorch" not in data["brief"]["missing"]
    assert "PyTorch" in data["parse_warnings"]


def test_a_genuinely_absent_word_is_still_missing(tmp_path, extracted) -> None:
    """The rescue must not become a machine for finding words that are not there."""
    folder = _folder(tmp_path, "## Top ATS keywords\n\nTensorFlow, MLflow, SQL\n")
    data = ats_report.report(folder)
    assert set(data["brief"]["missing"]) == {"TensorFlow", "MLflow"}
    assert data["parse_warnings"] == []


def test_a_short_term_is_never_rescued_loosely(tmp_path, extracted) -> None:
    """The rescue has a length floor, and the floor is the whole safety margin.

    Loose matching allows a space between every character, so on a short term it
    stops being a repair and becomes a coin toss — `Rust` would match any `r
    u s t` the extractor happened to lay down. A term wrongly counted as present
    is the one failure this report cannot afford, so below the floor the strict
    pattern is the only pattern and the term is simply reported missing.
    """
    extracted("Skills: R ust, K afka, data pipelines.")
    folder = _folder(tmp_path, "## Top ATS keywords\n\nRust, Kafka\n")
    data = ats_report.report(folder)
    # Kafka is 5 characters, at the floor, so its split is repaired and declared.
    assert data["brief"]["matched"] == ["Kafka"]
    assert data["parse_warnings"] == ["Kafka"]
    # Rust is 4, below it, and stays missing rather than being guessed present.
    assert data["brief"]["missing"] == ["Rust"]


# --------------------------------------------------------------------------
# the posting side
# --------------------------------------------------------------------------

def test_posting_text_beats_the_archive(tmp_path, extracted) -> None:
    """The headless path hands over the stored description, already clean text.

    It is also the only path that works at all for a client-rendered board, and
    the two routes have to agree where both are available.
    """
    folder = _folder(tmp_path)
    jd = tmp_path / "jd.txt"
    jd.write_text(JOB_DESCRIPTION, encoding="utf-8")
    data = ats_report.report(folder, jd)
    assert data["posting"] is not None
    assert 0.0 < data["posting"]["coverage"] <= 1.0
    assert data["posting"]["source"] == jd.name
    # Extracted from the ad, not from a hand-written list: the terms the ad
    # repeats are the ones it is built around.
    assert any("machine learning" in term for term in
               data["posting"]["matched"] + data["posting"]["missing"])


def test_the_company_name_is_not_a_skill(tmp_path, extracted) -> None:
    """A posting says its own name forty times. That is not a requirement."""
    words = ats_report._company_words(Path("2026/Roche/2026-08-06 - ML Engineer"))
    assert "roche" in words
    # The legal-form noise words are excluded, or every German posting would
    # report `gmbh` as its top keyword.
    assert "gmbh" not in ats_report._company_words(
        Path("2026/kausable GmbH/2026-08-05 - ML Product Engineer"))


# --------------------------------------------------------------------------
# the one line that reaches a human
# --------------------------------------------------------------------------

def test_summary_line_names_both_numbers_separately() -> None:
    data = {"brief": {"coverage": 0.71, "missing": [], "matched": [], "total": 7},
            "posting": {"coverage": 0.66, "missing": [], "matched": [], "total": 9}}
    assert ats_report.summary_line(data) == "ATS 71% brief · 66% posting"


def test_summary_line_caps_the_missing_list() -> None:
    data = {"brief": {"coverage": 0.5,
                      "missing": ["MLflow", "statistics", "Airflow", "dbt", "Spark"],
                      "matched": [], "total": 10}}
    line = ats_report.summary_line(data)
    assert line == ("ATS 50% brief — missing: MLflow, statistics, Airflow +2 more")


def test_summary_line_is_empty_when_nothing_was_measured() -> None:
    assert ats_report.summary_line({"brief": None, "posting": None}) == ""
    assert ats_report.summary_line({}) == ""


def test_the_report_always_carries_its_caveat(tmp_path, extracted) -> None:
    """It is keyword coverage. No vendor publishes the number a recruiter sees."""
    data = ats_report.report(_folder(tmp_path))
    assert "not a recruiter" in data["note"]


def test_the_cli_never_fails_a_run(tmp_path, capsys) -> None:
    """Exit 0 on a folder with nothing in it. This measures; it never judges."""
    assert ats_report.main([str(tmp_path)]) == 0
    assert "n/a" in capsys.readouterr().out
