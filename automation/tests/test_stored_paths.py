r"""What the watcher database keeps in its three path columns, and why.

`builds.folder`, `builds.log_path` and `questions.folder` all hold a path
*relative to the workspace root*. Callers pass and receive absolute paths; the
relative form exists only on disk, so a workspace that moves drive, machine or
OS leaves nothing dangling behind it.

The bug: after this workspace moved off `D:\Job Applications`, all 20 folders,
all 22 log paths and the one open question still named that drive. A build log
that does not resolve is a progress message that can never be replayed, and a
question folder that does not resolve is a `/cancel` that silently cleans up
nothing.

The tests below check the boundary in both directions and then the two callers
that read a raw row rather than going through an accessor, because those are
the places where a stored value can escape unconverted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from watcher import store

# `watcher.config` is what puts `scripts/` on `sys.path`, and `store` imports it.
import workspace_paths  # noqa: E402

FOLDER = Path("2026") / "Acme" / "2026-08-02 - ML Engineer"
LOG = Path("automation") / "logs" / "builds" / "acme.log"
#: What the same two paths looked like on the drive this workspace used to be on.
STALE_FOLDER = r"D:\Job Applications\2026\Acme\2026-08-02 - ML Engineer"
STALE_LOG = r"D:\Job Applications\automation\logs\builds\acme.log"


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    """A workspace holding the folder and the log, with `ROOT` pointed at it.

    Patching the module global rather than passing `root=` everywhere is what
    the production call sites do — they take the default — so this exercises
    the same code path they will.
    """
    base = tmp_path / "workspace"
    (base / FOLDER).mkdir(parents=True)
    (base / LOG).parent.mkdir(parents=True)
    (base / LOG).write_text("event\n", encoding="utf-8")
    monkeypatch.setattr(workspace_paths, "ROOT", base)
    return base


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    with store.connect(path) as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
                                     canonical_url, company, title, first_seen_at)
               VALUES ('p1','k1','portal:test','test','https://example.test/1',
                       'https://example.test/1','Acme','ML Engineer',
                       '2026-08-02T09:00:00+00:00')""")
    return path


def _stored(path: Path, table: str, column: str) -> str:
    """The raw cell, straight out of SQLite, with no conversion in the way."""
    with store.connect(path) as conn:
        row = conn.execute(f"SELECT {column} AS v FROM {table} ORDER BY id DESC "
                           f"LIMIT 1").fetchone()
    return str(row["v"] or "")


# --------------------------------------------------------------------------
# builds.log_path
# --------------------------------------------------------------------------

def test_a_log_path_goes_in_absolute_and_is_stored_relative(root, db) -> None:
    with store.connect(db) as conn:
        store.start_build(conn, "p1", str(root / LOG))

    assert _stored(db, "builds", "log_path") == "automation/logs/builds/acme.log"


def test_a_log_path_comes_back_absolute(root, db) -> None:
    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", str(root / LOG))
        assert store.build_log_path(conn, build_id) == str(root / LOG)


def test_a_log_path_stored_before_the_move_still_resolves(root, db) -> None:
    """The 22 rows this change exists for. Nothing had to be migrated first."""
    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", "")
        conn.execute("UPDATE builds SET log_path = ? WHERE id = ?",
                     (STALE_LOG, build_id))
        assert store.build_log_path(conn, build_id) == str(root / LOG)
        assert Path(store.build_log_path(conn, build_id)).is_file()


def test_a_build_with_no_log_reports_no_log(root, db) -> None:
    """A duplicate decline never spawns a process, so "" is an ordinary answer."""
    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", "")
        assert store.build_log_path(conn, build_id) == ""


def test_marking_a_build_running_relativises_too(root, db) -> None:
    with store.connect(db) as conn:
        build_id = store.queue_build(conn, "p1")
        store.mark_build_running(conn, build_id, str(root / LOG))
        assert store.build_log_path(conn, build_id) == str(root / LOG)

    assert _stored(db, "builds", "log_path") == "automation/logs/builds/acme.log"


# --------------------------------------------------------------------------
# builds.folder and questions.folder
# --------------------------------------------------------------------------

def test_a_finished_build_stores_its_folder_relative(root, db) -> None:
    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", str(root / LOG))
        store.finish_build(conn, build_id, "done", folder=str(root / FOLDER))

    assert _stored(db, "builds", "folder") == "2026/Acme/2026-08-02 - ML Engineer"


def test_a_question_stores_its_folder_relative(root, db) -> None:
    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", str(root / LOG))
        store.ask_question(conn, build_id, "p1", store.QUESTION_STOP_AND_ASK,
                           chat_id="-100", message_id=701,
                           folder=str(root / FOLDER))

    assert _stored(db, "questions", "folder") == "2026/Acme/2026-08-02 - ML Engineer"


def test_a_question_without_a_folder_stores_nothing(root, db) -> None:
    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", str(root / LOG))
        store.ask_question(conn, build_id, "p1", store.QUESTION_DUPLICATE,
                           chat_id="-100", message_id=702)

    assert _stored(db, "questions", "folder") == ""


# --------------------------------------------------------------------------
# the raw-row readers
# --------------------------------------------------------------------------

def test_the_question_folder_reaches_cleanup_as_an_absolute_path(root, db) -> None:
    """`/cancel`, a decline and the expiry sweep all read the raw row.

    `clean_up` hands the value to `cleanup_application.py --folder`, which
    resolves against the process cwd — so a relative string arriving there
    would be refused as "not <YYYY>/<Company>/<date> - <Role>".
    """
    from watcher.config import to_absolute

    with store.connect(db) as conn:
        build_id = store.start_build(conn, "p1", str(root / LOG))
        store.ask_question(conn, build_id, "p1", store.QUESTION_STOP_AND_ASK,
                           chat_id="-100", message_id=701,
                           folder=str(root / FOLDER))
        question = store.open_questions(conn)[0]

    assert to_absolute(question["folder"]) == root / FOLDER
    assert to_absolute(question["folder"]).is_dir()


def test_a_progress_replay_finds_the_log_off_the_raw_row(root, db) -> None:
    """`builder._restore_progress_message` reads `row["log_path"]` directly."""
    from watcher.config import to_absolute

    with store.connect(db) as conn:
        store.start_build(conn, "p1", str(root / LOG))
        row = conn.execute("SELECT * FROM builds ORDER BY id DESC LIMIT 1").fetchone()

    assert to_absolute(row["log_path"]) == root / LOG
    assert to_absolute(row["log_path"]).read_text(encoding="utf-8") == "event\n"
