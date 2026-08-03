"""Job posting watcher.

Polls job sources, scores new postings against the canonical profile, notifies
over Telegram, and — on approval — invokes the existing Claude Code application
pipeline headlessly.

This package only ever *calls* the application pipeline. It does not duplicate
any of it: builds go through `claude -p` with the same URL+note prompt a human
would type, so the CLAUDE.md orchestration stays the single implementation.
"""

__all__ = ["config", "normalize", "store", "prefilter"]
