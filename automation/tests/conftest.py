"""Test setup: put `automation/` on the path so `watcher` imports as a package.

The watcher is run as `python -m watcher.poll` from `automation/`, not installed,
so there is no package metadata to lean on here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from watcher import logsetup  # noqa: E402  (needs the path set above)


@pytest.fixture(scope="session", autouse=True)
def quarantine_the_log(tmp_path_factory):
    """Keep the suite out of the running watcher's log file.

    Every CLI entry point calls `logsetup.setup()`, which attaches a rotating
    handler on `logs/watcher.log` — the real one, the one the owner reads and
    `watcherctl tail` prints. Any test that exercises a `main()` therefore
    attaches it, and `_CONFIGURED` is a module global, so it stays attached for
    the rest of the session: from that point on every test's log records go to
    the operator's log, not just the ones from the test that opened it.

    It looks exactly like production traffic, because it is production
    formatting. A full run left a couple of hundred lines about builds failing
    for a company that does not exist, timestamped in the middle of a real
    afternoon. Anyone reading back through an incident would have to work out
    which of those were real.

    Session-scoped because the damage is session-scoped: one `main()` early on
    poisons everything after it, whatever order the files run in.
    """
    patch = pytest.MonkeyPatch()
    patch.setattr(logsetup, "LOG_DIR", tmp_path_factory.mktemp("logs"))
    yield
    patch.undo()
