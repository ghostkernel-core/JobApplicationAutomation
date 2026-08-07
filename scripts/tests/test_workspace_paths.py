r"""How a path is written down, and read back somewhere else entirely.

The property under test is portability: a value stored by one checkout has to
resolve in another, on another drive, on another OS. Every test here therefore
uses a fake root under `tmp_path` and passes it explicitly, because a test that
leans on the real workspace root proves only that the machine it ran on has not
moved yet — which is exactly the assumption that broke.

The regression behind it: the workspace moved off `D:\Job Applications` and 39
of 43 tracker rows plus every path in the watcher database kept pointing there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workspace_paths as wp  # noqa: E402


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """A workspace with one deliverable folder and one build log in it.

    A level below `tmp_path`, so a test can stand a second workspace beside it
    and read the same stored value from both.
    """
    base = tmp_path / "workspace"
    (base / "2026" / "Acme" / "2026-08-02 - ML Engineer").mkdir(parents=True)
    (base / "automation" / "logs" / "builds").mkdir(parents=True)
    (base / "automation" / "logs" / "builds" / "acme.log").write_text("x")
    return base


# --------------------------------------------------------------------------
# to_relative
# --------------------------------------------------------------------------

def test_a_path_under_the_root_is_stored_relative_to_it(root: Path) -> None:
    folder = root / "2026" / "Acme" / "2026-08-02 - ML Engineer"
    assert wp.to_relative(folder, root) == "2026/Acme/2026-08-02 - ML Engineer"


def test_a_path_from_another_machine_is_recovered_by_its_tail(root: Path) -> None:
    """Rule 2, which is what repairs the 39 stale rows.

    Nothing about `D:\\Job Applications` is known here. The tail is found by
    asking the filesystem whether it exists under *this* root.
    """
    stale = r"D:\Job Applications\2026\Acme\2026-08-02 - ML Engineer"
    assert wp.to_relative(stale, root) == "2026/Acme/2026-08-02 - ML Engineer"


def test_the_tail_rule_is_not_limited_to_deliverable_folders(root: Path) -> None:
    stale = r"D:\Job Applications\automation\logs\builds\acme.log"
    assert wp.to_relative(stale, root) == "automation/logs/builds/acme.log"


def test_the_longest_matching_tail_wins(root: Path) -> None:
    """A shorter tail that also exists must not shadow the full one."""
    (root / "builds").mkdir()
    stale = r"D:\Elsewhere\automation\logs\builds"
    assert wp.to_relative(stale, root) == "automation/logs/builds"


def test_a_single_matching_segment_is_not_a_match(root: Path) -> None:
    """`D:\\Somewhere\\automation` is not this workspace's automation folder.

    One segment that happens to exist under the root is a coincidence; every
    path this system stores is at least two deep.
    """
    stale = r"D:\Somewhere\Unrelated\automation"
    assert wp.to_relative(stale, root) == stale


def test_a_deliverable_folder_that_is_gone_is_still_recognised(root: Path) -> None:
    """Rule 3: the shape is one this system defines, so it needs no folder."""
    stale = r"D:\Job Applications\2026\Cleaned Up\2026-01-05 - Data Scientist"
    assert wp.to_relative(stale, root) == "2026/Cleaned Up/2026-01-05 - Data Scientist"


def test_a_value_that_cannot_be_read_is_returned_unchanged(root: Path) -> None:
    """Rule 4. Visibly wrong beats silently mangled."""
    foreign = r"D:\somewhere\else\notes.txt"
    assert wp.to_relative(foreign, root) == foreign


def test_backslashes_are_split_without_help_from_the_platform(root: Path) -> None:
    """The one that fails on Linux if `Path` is used to split a stored value.

    `Path(r"2026\\Acme\\x")` is a *single* segment on POSIX, so this cannot go
    through `pathlib` at all.
    """
    assert wp._segments(r"2026\Acme\2026-08-02 - ML Engineer") == [
        "2026", "Acme", "2026-08-02 - ML Engineer"]
    assert wp.to_relative(r"2026\Acme\2026-08-02 - ML Engineer", root) == (
        "2026/Acme/2026-08-02 - ML Engineer")


def test_an_already_relative_value_is_left_as_it_is(root: Path) -> None:
    """Idempotence, which is what makes the repair pass safe to re-run."""
    stored = "2026/Acme/2026-08-02 - ML Engineer"
    assert wp.to_relative(stored, root) == stored
    assert wp.to_relative(wp.to_relative(stored, root), root) == stored


def test_output_never_contains_a_backslash(root: Path) -> None:
    values = [
        root / "2026" / "Acme" / "2026-08-02 - ML Engineer",
        r"D:\Job Applications\2026\Acme\2026-08-02 - ML Engineer",
        r"D:\Job Applications\automation\logs\builds\acme.log",
        r"2026\Gone\2026-01-05 - Analyst",
    ]
    for value in values:
        assert "\\" not in wp.to_relative(value, root)


def test_an_empty_value_stays_empty(root: Path) -> None:
    """An empty cell means "none recorded" — not a path, and not one to invent."""
    assert wp.to_relative("", root) == ""
    assert wp.to_relative(None, root) == ""


def test_a_quoted_value_is_unquoted(root: Path) -> None:
    quoted = '"D:\\Job Applications\\2026\\Acme\\2026-08-02 - ML Engineer"'
    assert wp.to_relative(quoted, root) == "2026/Acme/2026-08-02 - ML Engineer"


# --------------------------------------------------------------------------
# to_absolute
# --------------------------------------------------------------------------

def test_a_stored_value_resolves_under_whichever_root_reads_it(
        root: Path, tmp_path: Path) -> None:
    """The whole point, stated directly.

    One checkout writes the value; a second checkout somewhere else entirely
    reads it and gets its own copy of the same thing.
    """
    other = tmp_path / "another-machine"
    (other / "2026" / "Acme" / "2026-08-02 - ML Engineer").mkdir(parents=True)

    stored = wp.to_relative(root / "2026" / "Acme" / "2026-08-02 - ML Engineer", root)

    assert wp.to_absolute(stored, root) == root / "2026/Acme/2026-08-02 - ML Engineer"
    assert wp.to_absolute(stored, other) == other / "2026/Acme/2026-08-02 - ML Engineer"
    assert wp.to_absolute(stored, other).is_dir()


def test_to_absolute_is_idempotent_on_an_absolute_path(root: Path) -> None:
    """Boundaries see both forms while the stored rows are being migrated."""
    folder = root / "2026" / "Acme" / "2026-08-02 - ML Engineer"
    assert wp.to_absolute(folder, root) == folder
    assert wp.to_absolute(wp.to_absolute(folder, root), root) == folder


def test_to_absolute_rehomes_a_stale_absolute_path(root: Path) -> None:
    stale = r"D:\Job Applications\2026\Acme\2026-08-02 - ML Engineer"
    assert wp.to_absolute(stale, root) == root / "2026/Acme/2026-08-02 - ML Engineer"


def test_to_absolute_leaves_an_unreadable_absolute_path_alone(root: Path) -> None:
    """Rule 4 all the way through: no root is grafted onto a foreign path.

    This is the case that used to differ by platform. `Path.is_absolute()` calls
    a Windows path relative when asked on POSIX, so the same stored value came
    back as `<root>/D:\\somewhere\\else\\notes.txt` in CI and unchanged locally.
    """
    foreign = r"D:\somewhere\else\notes.txt"
    assert str(wp.to_absolute(foreign, root)) == foreign


@pytest.mark.parametrize("value", [
    r"D:\Job Applications\2026\Acme\x",   # a Windows drive
    r"\\server\share\2026\Acme\x",        # a UNC share
    "/home/user/workspace/2026/Acme/x",   # POSIX
])
def test_absolute_is_recognised_whoever_wrote_it(value: str) -> None:
    assert wp.looks_absolute(value)


@pytest.mark.parametrize("value", [
    "2026/Acme/x", r"2026\Acme\x", "automation/logs/builds/acme.log", "", "x",
])
def test_a_relative_value_is_not_mistaken_for_an_absolute_one(value: str) -> None:
    assert not wp.looks_absolute(value)
