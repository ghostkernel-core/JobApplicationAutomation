"""`python -m watcher.match` — alias for the matcher CLI.

The logic lives in `matcher.py` alongside the other nouns (poll, store,
notifier); this exists so the command reads as a verb.
"""

from __future__ import annotations

from .matcher import main

if __name__ == "__main__":
    raise SystemExit(main())
