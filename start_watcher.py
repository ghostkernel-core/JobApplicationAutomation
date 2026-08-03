"""Start the job watcher detached, so it keeps polling after the terminal closes.

    python start_watcher.py            # start it (no-op if already running)
    python start_watcher.py --status   # report running/not-running, start nothing

This is a thin wrapper kept for the shortcuts, scheduler tasks and documentation
that already point at it. The real control surface is
`automation/watcherctl.py`, which does the same two things plus stop, restart,
poll, health, reset, digest and logs:

    py automation/watcherctl.py --help

Everything this file used to do itself -- preflighting the venv, `.env` token and
`build_settings.json`, refusing to start a second instance, and finding a watcher
that was started some other way -- now lives there, so there is one
implementation rather than two that can drift. The one behaviour that did not
survive is the pidfile at `automation/state/watcher.launcher.pid`: it only ever
covered instances this launcher started, and the process-table scan that had to
back it up is strictly better on its own. A leftover pidfile from an older
version is inert and can be deleted.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WATCHERCTL = ROOT / "automation" / "watcherctl.py"


def main(argv: list[str]) -> int:
    if not WATCHERCTL.exists():
        print(f"missing {WATCHERCTL}", file=sys.stderr)
        return 2
    command = "status" if "--status" in argv else "start"
    return subprocess.run([sys.executable, str(WATCHERCTL), command]).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
