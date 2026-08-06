"""Logging configuration shared by the CLIs and the long-running service."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LOG_DIR, ensure_dirs

_CONFIGURED = False

#: What survives a trim of `logs/watcher.out`: enough for a full traceback and
#: the tail of whatever led to it, and nowhere near enough to be worth reading
#: as a log. `watcher.log` is the log — it is formatted, it rotates, and
#: `watcherctl logs` reads it. This file only ever has to catch what dies
#: before that handler is attached, so its whole value is in its last pages.
STARTUP_LOG_KEEP_BYTES = 200_000

#: When a *running* watcher trims its own startup log. Every restart trims down
#: to KEEP anyway, so this only fires on a process that has stayed up long
#: enough to write a megabyte by itself.
STARTUP_LOG_MAX_BYTES = 1_000_000

#: How far into the kept block to look for a line break before giving up on a
#: clean cut. The block can open in the middle of something enormous — a
#: traceback carrying a huge repr, or a run of NUL padding — and skipping "the
#: partial first line" through one of those throws away nearly everything the
#: trim was meant to keep. A ragged first line is the lesser problem.
_LINE_SEARCH_BYTES = 8_192


def force_utf8() -> None:
    """Make stdout/stderr UTF-8.

    The Windows console defaults to cp1252 here, which turns every em dash and
    umlaut in a company name into a `?`. Job postings are full of both.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup(level: int = logging.INFO, to_file: bool = True) -> logging.Logger:
    """Configure root logging once. Repeat calls are no-ops."""
    global _CONFIGURED
    logger = logging.getLogger("watcher")
    if _CONFIGURED:
        return logger

    force_utf8()
    ensure_dirs()
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if to_file:
        handler = RotatingFileHandler(
            LOG_DIR / "watcher.log", maxBytes=5_000_000, backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)

    # The HTTP libraries are chatty at INFO and drown out anything useful.
    for noisy in ("urllib3", "httpx", "httpcore", "apscheduler", "telegram"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return logger


def _resync_streams(target: Path, streams) -> None:
    """Point the inherited stdout/stderr handles at the new end of the file.

    `watcherctl start` opens `watcher.out`, writes its marker, and hands the
    *handle* to the watcher as fd 1 and fd 2. The append flag is a C-runtime
    idea and does not survive process creation on Windows, so the child does not
    seek to the end before each write — it writes wherever the shared file
    pointer happens to be.

    Truncate underneath that and the pointer is left far past the new end of
    file. The next line lands back at the old offset, Windows zero-fills
    everything in between, and the file is instantly the size it was again with
    the difference made up in NUL padding: 613,888 of them, the first time this
    ran against the live watcher. Seeking the descriptors forward is what makes
    the truncation stick.

    Only descriptors that are genuinely this file are touched. In a foreground
    run fd 1 is a console and fd 2 may be a pipe, and neither wants a seek.
    """
    try:
        marker = target.stat()
    except OSError:
        return
    for fd in streams:
        try:
            info = os.fstat(fd)
        except OSError:
            continue        # closed, or not a descriptor at all
        if (info.st_dev, info.st_ino) != (marker.st_dev, marker.st_ino):
            continue        # a console, a pipe, or somebody else's file
        try:
            os.lseek(fd, 0, os.SEEK_END)
        except OSError:
            pass


def trim_startup_log(path: Path | None = None, *, over: int = 0,
                     keep: int = STARTUP_LOG_KEEP_BYTES,
                     streams=(1, 2)) -> bool:
    """Drop all but the last `keep` bytes of the startup log, in place.

    `logs/watcher.out` is where `pythonw`'s stdout and stderr land, so it holds
    a second copy of every line the logger writes, plus anything that never
    reached the logger at all. Unlike `watcher.log` it had no rotation and no
    truncation between runs: `watcherctl start` opens it in append mode, and it
    grew across every restart, forever.

    Trimming in place rather than renaming is deliberate. The running process
    inherited a handle on *this* file and cannot be told about a new one, so
    moving it aside would send the rest of the run to a path nobody looks at.
    What in-place costs is that the descriptors still pointing into the old file
    have to be moved along with it — see `_resync_streams`, which is the whole
    reason this is not simply a truncate.

    `over` is the size that triggers a trim, for the periodic caller that should
    leave a normal-sized file alone; the trim itself always cuts back to `keep`.

    Returns True if anything was removed. Never raises: failing to tidy a log is
    not worth taking the watcher down for, and the untrimmed file still works.
    """
    target = path if path is not None else LOG_DIR / "watcher.out"
    try:
        size = target.stat().st_size
    except OSError:
        return False        # no file yet, which is every first run
    if size <= max(over, keep):
        return False

    try:
        with open(target, "r+b") as handle:
            handle.seek(size - keep)
            tail = handle.read()
            # Cut on a line break so the file does not open mid-timestamp, but
            # only if one turns up promptly — see `_LINE_SEARCH_BYTES`.
            cut = tail.find(b"\n", 0, _LINE_SEARCH_BYTES)
            if cut != -1:
                tail = tail[cut + 1:]
            handle.seek(0)
            handle.write(tail)
            handle.truncate()
    except OSError:
        logging.getLogger("watcher").warning(
            "could not trim %s, leaving it as it is", target, exc_info=True)
        return False

    _resync_streams(target, streams)
    return True
