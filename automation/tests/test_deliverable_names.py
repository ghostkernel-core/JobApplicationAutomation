"""A handoff note is recognised however its filename was capitalised.

`clean_deliverable.py` sorts a finished folder into keep / move / drop, and
anything it does not recognise is left in place and reported. That report is not
cosmetic: step 09 fails the run on an unexpected file, so one unrecognised name
is a failed final QA on an application that is otherwise complete.

Which is what happened. The move list held `research_note.md`, the comparison
lowercased the filename but kept its separators, and the pipeline had written
`Research Note.md` — the title CLAUDE.md uses for it. The file stayed in the
deliverable folder and QA reported it as unexpected.

Agents name these files the way a person titles a document. The classifier now
folds spaces and hyphens to underscores before matching, so the same note is the
same note whether it was written by a script or by a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import clean_deliverable as cleaner  # noqa: E402


def _classify(name: str) -> str:
    """Classify a filename. `_classify` only touches the path for `is_dir`."""
    return cleaner._classify(Path("nowhere") / name)


# --------------------------------------------------------------------------
# the regression
# --------------------------------------------------------------------------

def test_the_note_that_was_left_behind_is_moved() -> None:
    """The exact filename that failed QA, byte for byte."""
    assert _classify("Research Note.md") == "move"


@pytest.mark.parametrize("name", [
    "Research Note.md", "research_note.md", "research-note.md",
    "RESEARCH NOTE.md",
    "Match Brief.md", "match_brief.md", "match-brief.md",
])
def test_a_handoff_note_is_recognised_however_it_was_titled(name: str) -> None:
    assert _classify(name) == "move"


def test_payloads_are_still_moved_by_suffix() -> None:
    """The JSON rule is separate from the name rule and must stay untouched."""
    assert _classify("cv_payload_en.json") == "move"
    assert _classify("interview_prep_payload.json") == "move"


# --------------------------------------------------------------------------
# folding separators must not swallow anything else
# --------------------------------------------------------------------------

def test_the_deliverables_are_still_kept() -> None:
    """These carry spaces and a hyphen in every real folder."""
    for name in ("Someone - CV.pdf", "Someone - CV.tex",
                 "Someone - Cover Letter.pdf", "Someone - Interview Prep.pdf",
                 "Acme GmbH - Data Scientist.html"):
        assert _classify(name) == "keep", name


def test_an_unrelated_markdown_file_is_still_reported() -> None:
    """The report is the point. Widening the match must not silence it.

    Anything the script cannot place stays where it is and gets named, so a file
    the user put there deliberately is never moved out from under them.
    """
    assert _classify("Notes to self.md") == "unknown"
    assert _classify("salary research.md") == "unknown"


def test_build_artifacts_are_still_dropped() -> None:
    assert _classify("Someone - CV.aux") == "drop"
    assert _classify("Someone - CV.synctex.gz") == "drop"
    assert _classify("Someone - CV_p1.png") == "drop"


def test_normalising_is_confined_to_separators() -> None:
    """A different name is still a different name."""
    assert cleaner._normalised("Research Note.md") == "research_note.md"
    assert cleaner._normalised("Research-Note.MD") == "research_note.md"
    assert cleaner._normalised("researchnote.md") != "research_note.md"
