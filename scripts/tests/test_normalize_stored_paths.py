r"""The one-off repair pass over paths that were stored before the convention.

Everything here builds a whole fake workspace under `tmp_path` — a tracker
workbook and a watcher database holding `D:\Job Applications\...` values — and
checks that a single pass leaves both readable from wherever the workspace now
sits. The properties that matter are that it changes only what it can read, and
that running it twice changes nothing the second time.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import normalize_stored_paths as nsp  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")

STALE = r"D:\Job Applications\2026\Acme\2026-08-02 - ML Engineer"
GOOD = "2026/Acme/2026-08-02 - ML Engineer"
STALE_LOG = r"D:\Job Applications\automation\logs\builds\acme.log"
GOOD_LOG = "automation/logs/builds/acme.log"
UNREADABLE = r"D:\somewhere\else\notes.txt"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _workbook(root: Path, folders: list[str]) -> Path:
    """A tracker workbook whose Application Folder column holds `folders`."""
    path = root / "2026" / "Job Applications Tracker 2026.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Applications"
    sheet.append(["#", "Company", "Position Applied", "Date Applied",
                  "Application Folder", "Days Since Applied"])
    for index, folder in enumerate(folders, start=1):
        sheet.append([index, "Acme", "ML Engineer", "2026-08-02", folder,
                      f"=TODAY()-D{index + 1}"])
    book.save(path)
    return path


def _database(root: Path, folder: str = STALE, log_path: str = STALE_LOG,
              question: str = STALE) -> Path:
    path = root / "automation" / "state" / "watch.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE builds (id INTEGER PRIMARY KEY, folder TEXT, log_path TEXT);"
        "CREATE TABLE questions (id INTEGER PRIMARY KEY, folder TEXT);")
    conn.execute("INSERT INTO builds (folder, log_path) VALUES (?, ?)",
                 (folder, log_path))
    conn.execute("INSERT INTO questions (folder) VALUES (?)", (question,))
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """A workspace holding the things the stale values used to name."""
    base = tmp_path / "workspace"
    (base / "2026" / "Acme" / "2026-08-02 - ML Engineer").mkdir(parents=True)
    (base / "automation" / "logs" / "builds").mkdir(parents=True)
    (base / "automation" / "logs" / "builds" / "acme.log").write_text("x")
    return base


@pytest.fixture()
def workspace(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`root`, plus both stores, plus `main()` pointed at it and no watcher up."""
    _workbook(root, [STALE])
    _database(root)
    monkeypatch.setattr(nsp, "ROOT", root)
    monkeypatch.setattr(nsp, "running_watchers", lambda: [])
    return root


def _folders(path: Path) -> list[str]:
    book = openpyxl.load_workbook(path)
    sheet = book["Applications"]
    try:
        return [str(sheet.cell(row=r, column=5).value or "")
                for r in range(2, sheet.max_row + 1)]
    finally:
        book.close()


def _column(path: Path, table: str, column: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [str(v) for (v,) in conn.execute(f"SELECT {column} FROM {table}")]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the workbook half
# --------------------------------------------------------------------------

def test_a_stale_tracker_row_is_rewritten(root: Path) -> None:
    path = _workbook(root, [STALE])
    result = nsp.normalize_workbook(path, root, dry_run=False)

    assert _folders(path) == [GOOD]
    assert result.changed == [f"row 2: {STALE} -> {GOOD}"]


def test_a_relative_backslash_row_is_rewritten(root: Path) -> None:
    """The other broken shape: relative already, but unsplittable off Windows."""
    path = _workbook(root, [r"2026\Acme\2026-08-02 - ML Engineer"])
    nsp.normalize_workbook(path, root, dry_run=False)
    assert _folders(path) == [GOOD]


def test_a_value_that_cannot_be_read_is_left_alone(root: Path) -> None:
    path = _workbook(root, [UNREADABLE])
    result = nsp.normalize_workbook(path, root, dry_run=False)

    assert _folders(path) == [UNREADABLE]
    assert not result.changed
    assert result.left_alone == [f"row 2: {UNREADABLE} -- could not be read; left alone"]


def test_a_rewritten_path_that_names_nothing_is_flagged_not_refused(
        root: Path) -> None:
    gone = r"D:\Job Applications\2026\Gone\2026-01-05 - Analyst"
    path = _workbook(root, [gone])
    result = nsp.normalize_workbook(path, root, dry_run=False)

    assert _folders(path) == ["2026/Gone/2026-01-05 - Analyst"]
    assert "names nothing on disk" in result.changed[0]


def test_an_already_relative_row_is_counted_and_not_rewritten(root: Path) -> None:
    path = _workbook(root, [GOOD])
    result = nsp.normalize_workbook(path, root, dry_run=False)

    assert not result.changed
    assert result.already_relative == 1


def test_the_computed_column_survives_the_rewrite(root: Path) -> None:
    """Cell-by-cell, so unlike a row delete the per-row formulas never move."""
    path = _workbook(root, [STALE, STALE])
    nsp.normalize_workbook(path, root, dry_run=False)

    book = openpyxl.load_workbook(path)
    sheet = book["Applications"]
    try:
        assert [sheet.cell(row=r, column=6).value for r in (2, 3)] == [
            "=TODAY()-D2", "=TODAY()-D3"]
    finally:
        book.close()


def test_a_workbook_without_the_column_is_skipped(root: Path) -> None:
    path = root / "2026" / "Job Applications Tracker 2026.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    book.active.title = "Applications"
    book.active.append(["Company", "Position Applied"])
    book.save(path)

    result = nsp.normalize_workbook(path, root, dry_run=False)
    assert not result.changed
    assert "no 'Application Folder' column" in result.left_alone[0]


# --------------------------------------------------------------------------
# the database half
# --------------------------------------------------------------------------

def test_all_three_database_columns_are_rewritten(root: Path) -> None:
    path = _database(root)
    result = nsp.normalize_database(path, root, dry_run=False)

    assert _column(path, "builds", "folder") == [GOOD]
    assert _column(path, "builds", "log_path") == [GOOD_LOG]
    assert _column(path, "questions", "folder") == [GOOD]
    assert len(result.changed) == 3


def test_an_unreadable_database_value_is_left_alone(root: Path) -> None:
    path = _database(root, log_path=UNREADABLE)
    result = nsp.normalize_database(path, root, dry_run=False)

    assert _column(path, "builds", "log_path") == [UNREADABLE]
    assert any("could not be read" in line for line in result.left_alone)


def test_a_missing_table_is_reported_not_fatal(root: Path) -> None:
    path = root / "automation" / "state" / "watch.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE builds (id INTEGER PRIMARY KEY, folder TEXT, log_path TEXT);")
    conn.execute("INSERT INTO builds (folder, log_path) VALUES (?, ?)",
                 (STALE, STALE_LOG))
    conn.commit()
    conn.close()

    result = nsp.normalize_database(path, root, dry_run=False)
    assert _column(path, "builds", "folder") == [GOOD]
    assert result.left_alone == ["no questions table; skipped"]


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing(workspace: Path, capsys) -> None:
    workbook = workspace / "2026" / "Job Applications Tracker 2026.xlsx"
    database = workspace / "automation" / "state" / "watch.db"

    assert nsp.main(["--dry-run"]) == 0

    assert _folders(workbook) == [STALE]
    assert _column(database, "builds", "folder") == [STALE]
    out = capsys.readouterr().out
    assert "Would rewrite 4 stored path(s)" in out
    assert "Re-run without --dry-run to apply." in out


def test_one_pass_repairs_both_stores(workspace: Path, capsys) -> None:
    workbook = workspace / "2026" / "Job Applications Tracker 2026.xlsx"
    database = workspace / "automation" / "state" / "watch.db"

    assert nsp.main([]) == 0

    assert _folders(workbook) == [GOOD]
    assert _column(database, "builds", "folder") == [GOOD]
    assert _column(database, "builds", "log_path") == [GOOD_LOG]
    assert _column(database, "questions", "folder") == [GOOD]
    assert "Rewrote 4 stored path(s)" in capsys.readouterr().out


def test_a_second_pass_changes_nothing(workspace: Path, capsys) -> None:
    assert nsp.main([]) == 0
    capsys.readouterr()

    assert nsp.main([]) == 0
    assert "Rewrote 0 stored path(s)" in capsys.readouterr().out


def test_year_limits_the_run_to_one_workbook(workspace: Path, capsys) -> None:
    database = workspace / "automation" / "state" / "watch.db"

    assert nsp.main(["--year", "2026"]) == 0

    assert _folders(workspace / "2026" / "Job Applications Tracker 2026.xlsx") == [GOOD]
    assert _column(database, "builds", "folder") == [STALE]
    assert "Rewrote 1 stored path(s)" in capsys.readouterr().out


def test_a_running_watcher_blocks_the_database_half(
        workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """It would write the old absolute form straight back over the repair."""
    monkeypatch.setattr(nsp, "running_watchers", lambda: [4321])
    database = workspace / "automation" / "state" / "watch.db"

    assert nsp.main([]) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out and "4321" in out
    assert _column(database, "builds", "folder") == [STALE]
    # The workbook half is safe either way, so it still runs.
    assert _folders(workspace / "2026" / "Job Applications Tracker 2026.xlsx") == [GOOD]


def test_a_workspace_with_no_database_yet_is_not_an_error(
        root: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _workbook(root, [STALE])
    monkeypatch.setattr(nsp, "ROOT", root)
    monkeypatch.setattr(nsp, "running_watchers", lambda: [])

    assert nsp.main([]) == 0
    assert "not created yet; skipped" in capsys.readouterr().out
