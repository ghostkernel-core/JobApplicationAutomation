"""Which identity.toml gets loaded, and what the override is not allowed to do.

The regression: `identity.toml` is untracked by design, so a CI checkout has
none — and several modules build their document-name constants from it at
*import* time. That is not a skipped test. It is a collection error that takes
down every unrelated test sharing the file, which is how two new test modules
arrived red for a reason that had nothing to do with either of them.

`JOBAPP_IDENTITY` is the way out, and the danger in it is obvious: a mechanism
for pointing the loader at a different person is one bad default away from
printing that person onto a real application. So the tests below spend more
effort on what the override must *not* relax than on the redirection itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workspace_identity as wi  # noqa: E402

FIXTURE = (Path(__file__).resolve().parents[2]
           / "automation" / "tests" / "data" / "identity.fixture.toml")

COMPLETE = """
[person]
full_name     = "Alex Morgan"
file_prefix   = "Morgan, Alex"
email         = "alex.morgan@example.invalid"
phone         = "+49 30 000000"
date_of_birth = "01.01.1990"
linkedin      = "linkedin.com/in/example"
street        = "Beispielstrasse 1"
city          = "Berlin"

[en]
city_line   = "10115 Berlin, Germany"
nationality = "German"

[de]
city_line   = "10115 Berlin, Deutschland"
nationality = "Deutsch"
"""


def _write(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "identity.toml"
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# where it looks
# --------------------------------------------------------------------------

def test_the_repo_root_is_the_default() -> None:
    assert wi.default_path({}) == wi.ROOT / "identity.toml"


def test_the_environment_redirects_it(tmp_path: Path) -> None:
    elsewhere = tmp_path / "other-person.toml"
    assert wi.default_path({"JOBAPP_IDENTITY": str(elsewhere)}) == elsewhere


def test_an_empty_override_is_not_an_override() -> None:
    """A shell that exports the variable unset must not send the loader to `.`.

    `Path("")` is the current directory, so the loader would look for an
    identity beside wherever the process happened to be started.
    """
    assert wi.default_path({"JOBAPP_IDENTITY": ""}) == wi.ROOT / "identity.toml"


# --------------------------------------------------------------------------
# what it refuses, wherever it was pointed
# --------------------------------------------------------------------------

def test_a_missing_file_names_itself(tmp_path: Path) -> None:
    """The error has to carry the path, or an override failure is unreadable:
    the file it names is not the one the reader expects to be looking at.
    """
    absent = tmp_path / "gone.toml"
    with pytest.raises(FileNotFoundError, match="gone.toml"):
        wi.load(absent)


@pytest.mark.parametrize("field, line", [
    ("full_name", 'full_name     = "FILL IN — as it should appear"'),
    ("email", 'email         = "FILL IN"'),
    ("city", 'city          = ""'),
], ids=["name", "email", "blank"])
def test_a_stub_is_rejected_through_the_override_too(tmp_path: Path, field: str,
                                                     line: str) -> None:
    """The whole risk of an override is that it becomes a way to run without a
    real identity. It is not: validation happens after the path is resolved, so
    a placeholder fails identically wherever it was found.
    """
    stub = "\n".join(l for l in COMPLETE.splitlines()
                     if not l.startswith(field.ljust(13)))
    with pytest.raises(ValueError, match=field):
        wi.load(_write(tmp_path, stub + "\n" + line))


def test_a_complete_file_loads(tmp_path: Path) -> None:
    ident = wi.load(_write(tmp_path, COMPLETE))
    assert ident.doc_name("CV", ".pdf") == "Morgan, Alex - CV.pdf"


# --------------------------------------------------------------------------
# the fixture CI actually points at
# --------------------------------------------------------------------------

def test_the_ci_fixture_is_loadable() -> None:
    """If this file ever stops satisfying the loader, CI fails at collection
    across two test modules with an error that names neither of them.
    """
    assert wi.load(FIXTURE).file_prefix == "Morgan, Alex"


def test_the_ci_fixture_is_not_a_real_person() -> None:
    """A guard against the obvious drift: someone fills the fixture with their
    own details to reproduce a rendering bug, and commits it. The values below
    are the ones that would end up printed on a document.
    """
    ident = wi.load(FIXTURE)
    assert ident.email.endswith(".invalid")
    assert "example" in ident.linkedin
    assert set(ident.phone.split()[-1]) == {"0"}
