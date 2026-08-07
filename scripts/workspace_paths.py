r"""How this workspace writes a path down: relative to its own root, forward slashes.

Every path this system persists — the tracker's `Application Folder` column, the
watcher's `builds.folder`, `builds.log_path` and `questions.folder` — names
something *inside* the workspace. Storing the absolute form of such a path
records two facts where there is only one: where the thing is relative to the
workspace, and where the workspace happened to be that day. The second one goes
stale the moment anyone moves the folder, and it takes the first down with it.

That is not hypothetical. This workspace used to live at `D:\Job Applications`.
After the move, 39 of 43 tracker rows and every path in the watcher database
still pointed there, which made a completed application look like an empty
folder to `dedupe.is_complete` and a build log look like a file that was never
written.

So: absolute paths exist in memory, where they are used. On disk, everything is
relative to the root, and the root is supplied by whoever is reading. Move the
workspace to another drive, clone it onto another machine, or run it on another
OS, and every stored path still resolves.

Forward slashes are not a style choice. `Path(r"2026\\Acme\\x")` is a *single*
segment on POSIX, so a backslash-separated value cannot be split anywhere but
Windows; `Path("2026/Acme/x")` is three segments on both. A stored value has to
outlive the platform that wrote it.

The watcher reaches this module the same way it reaches `workspace_identity`:
`watcher/config.py` puts `scripts/` on `sys.path` and re-exports, so there is
one definition rather than two that can drift.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent

# "2026-07-24 - AI Engineer" and "2026". The shape of a deliverable folder,
# defined here because three modules need to agree on it: this one,
# `cleanup_application.py`, and `watcher/dedupe.py`.
FOLDER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-\s*(.+)$")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# Splits on either separator, so a value written on Windows is still readable
# on POSIX and vice versa. `Path` cannot do this — it only knows the separator
# of the machine it is running on, which is exactly the assumption that broke.
_SEPARATORS = re.compile(r"[\\/]+")


def _segments(value: str) -> list[str]:
    """Path segments, ignoring a drive letter, a UNC prefix, and empties."""
    text = str(value).strip().strip('"')
    if len(text) > 1 and text[1] == ":":
        text = text[2:]  # "D:\Job Applications\..." -> "\Job Applications\..."
    return [part for part in _SEPARATORS.split(text) if part not in ("", ".")]


def looks_absolute(value: str | Path) -> bool:
    r"""Whether this value is absolute on *some* platform.

    `Path.is_absolute()` answers only for the platform asking, and the whole
    point of a stored value is that it outlives the one that wrote it: on POSIX,
    `Path(r"D:\Job Applications\x").is_absolute()` is `False`, so treating it as
    relative would graft the workspace root onto a path from another machine.
    """
    text = str(value).strip().strip('"')
    return (len(text) > 1 and text[1] == ":") or text[:1] in ("\\", "/")


def _looks_like_deliverable(parts: list[str]) -> bool:
    """<YYYY>/<Company>/<YYYY-MM-DD> - <Role> — a shape this system defines."""
    return (len(parts) == 3
            and bool(YEAR_RE.match(parts[0]))
            and bool(FOLDER_RE.match(parts[2])))


def to_relative(path: str | Path, root: str | Path | None = None) -> str:
    """How this system writes a path down. Always forward slashes.

    Four rules, most certain first:

    1. Under `root` — `relative_to(root)`, at any depth. Everything written from
       now on takes this branch, because every path this system produces is
       inside the workspace.
    2. Not under it, but some trailing run of its segments *exists* under `root`
       — take the longest such run. Self-verifying, and it needs no hardcoded
       knowledge of the layout: it recovers `2026/Acme/2026-08-02 - Role` and
       `automation/logs/builds/x.log` out of a stale `D:\\...` value alike.
    3. Nothing exists, but the last three segments are shaped like a deliverable
       folder — take those three. A folder that has since been cleaned up is
       still worth recording correctly, and this shape is one we define, so it
       can be recognised without the folder being there to confirm it.
    4. None of the above — returned unchanged. A value we cannot read is not one
       to overwrite; leaving it visibly wrong is better than silently mangling
       it into something that looks right.

    An empty value stays empty: the columns this feeds all treat "" as "none
    recorded", which is a different thing from a path.
    """
    if not path:
        return ""
    base = Path(root) if root is not None else ROOT

    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return PurePosixPath(candidate.resolve().relative_to(base.resolve())).as_posix()
        except (OSError, ValueError):
            pass  # a foreign root, or an unresolvable one — fall through

    parts = _segments(path)
    if not parts:
        return str(path)

    # Longest tail first, and never a single segment: every path this system
    # stores is at least two deep, so a lone segment that happens to exist
    # under the root — `scripts`, `automation` — is a coincidence, not the
    # value's meaning.
    for start in range(len(parts) - 1):
        tail = parts[start:]
        if (base / PurePosixPath(*tail)).exists():
            return PurePosixPath(*tail).as_posix()

    if _looks_like_deliverable(parts[-3:]):
        return PurePosixPath(*parts[-3:]).as_posix()

    return str(path)


def to_absolute(path: str | Path, root: str | Path | None = None) -> Path:
    r"""A stored value resolved against *this* workspace, wherever it now is.

    Idempotent on a path that is already absolute and already inside the root,
    so it is safe to apply at a boundary that sees both — which every one of
    them does while the stored rows are being migrated.

    A value `to_relative` could not read comes back untouched rather than hung
    off the root: `<workspace>/D:\somewhere\else.txt` is not an improvement on
    `D:\somewhere\else.txt`, and only the second one is recognisably wrong.
    """
    base = Path(root) if root is not None else ROOT
    relative = to_relative(path, base)
    return Path(relative) if looks_absolute(relative) else base / relative
