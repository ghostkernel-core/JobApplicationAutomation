"""Control the job watcher from the repository root.

    python start_watcher.py                 # start it detached (no-op if already running)
    python start_watcher.py status
    python start_watcher.py stop
    python start_watcher.py restart
    python start_watcher.py logs -n 50 -f
    python start_watcher.py --help          # every sub-command

Every sub-command of `automation/watcherctl.py` is available here verbatim --
this file adds a default (`start`, so the bare launcher shortcuts and scheduler
tasks keep working) and translates the historical `--status` flag into the
`status` sub-command. It holds no logic of its own: preflighting the venv,
`.env` token and `build_settings.json`, refusing to start a second instance, and
finding a watcher that was started some other way all live in `watcherctl`, so
there is one implementation rather than two that can drift.

`watcherctl` is imported rather than launched as a child process, which keeps
exit codes and Ctrl-C behaviour (`logs -f`) intact and avoids a second
interpreter. It imports nothing from `watcher` and needs no third-party package,
so this works under any interpreter on the machine -- the venv is only involved
for the sub-commands that genuinely need it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUTOMATION_DIR = ROOT / "automation"


def main(argv: list[str]) -> int:
    if not (AUTOMATION_DIR / "watcherctl.py").exists():
        print(f"missing {AUTOMATION_DIR / 'watcherctl.py'}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(AUTOMATION_DIR))
    import watcherctl

    # So the hints watcherctl prints name the command the user actually typed.
    watcherctl.PROG = "python start_watcher.py"

    # `--status` predates the sub-commands and is still in shortcuts and docs.
    argv = ["status" if arg == "--status" else arg for arg in argv]
    # Nothing, or options only (`--force`), still means the historical `start`.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["start", *argv]
    return watcherctl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
