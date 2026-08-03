"""Logging configuration shared by the CLIs and the long-running service."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, ensure_dirs

_CONFIGURED = False


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
