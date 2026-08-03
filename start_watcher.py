"""Start the job watcher detached, so it keeps polling after the terminal closes.

    python start_watcher.py            # start it (no-op if already running)
    python start_watcher.py --status   # report running/not-running, start nothing

automation/run_watcher.py has no single-instance guard of its own (no lockfile,
no pidfile check) — checked by reading it and automation/watcher/config.py
before writing this. So the guard lives here instead: a pidfile at
automation/state/watcher.launcher.pid, with a portable liveness check (Windows:
`tasklist /FI "PID eq N"`; POSIX: `os.kill(pid, 0)`) so a stale pidfile left
behind by a crash or reboot does not block a restart.

The pidfile only covers instances this launcher itself started. A watcher
started some other way (a manual `python run_watcher.py`, a scheduler task,
an earlier install of this same launcher) leaves no pidfile behind, so
`running_pid()` falls back to a process-table scan for a live `run_watcher.py`
process before concluding nothing is running — the same portable split,
`tasklist`/PowerShell on Windows and `ps` on POSIX.

Preflight checks the things that would otherwise fail silently in a detached
process with no attached console: the venv interpreter exists, .env has a
token, build_settings.json exists.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

AUTOMATION_DIR = ROOT / "automation"
STATE_DIR = AUTOMATION_DIR / "state"
LOG_DIR = AUTOMATION_DIR / "logs"
PIDFILE = STATE_DIR / "watcher.launcher.pid"
ENV_PATH = AUTOMATION_DIR / ".env"
BUILD_SETTINGS_PATH = AUTOMATION_DIR / "build_settings.json"


def _venv_python() -> Path:
    if sys.platform == "win32":
        return AUTOMATION_DIR / ".venv" / "Scripts" / "pythonw.exe"
    return AUTOMATION_DIR / ".venv" / "bin" / "python"


def _is_alive(pid: int) -> bool:
    """Portable liveness check for a PID recorded in the pidfile."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _scan_for_watcher_windows() -> int | None:
    """Look for a live run_watcher.py process via PowerShell (tasklist alone
    does not expose command lines, and wmic is no longer present everywhere)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter "
             "\"Name='python.exe' or Name='pythonw.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        pid_str, _, cmdline = line.partition("|")
        if "run_watcher.py" in cmdline:
            try:
                return int(pid_str.strip())
            except ValueError:
                continue
    return None


def _scan_for_watcher_posix() -> int | None:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if "run_watcher.py" not in line:
            continue
        pid_str, _, _ = line.partition(" ")
        try:
            return int(pid_str)
        except ValueError:
            continue
    return None


def running_pid() -> int | None:
    """A live watcher PID: from the pidfile if this launcher started it,
    otherwise from a process-table scan (see module docstring)."""
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
        if pid is not None and _is_alive(pid):
            return pid
    if sys.platform == "win32":
        return _scan_for_watcher_windows()
    return _scan_for_watcher_posix()


def preflight() -> None:
    """Exit 1 with a clear message on anything that would make the watcher
    fail silently once it is detached and has no console to report to."""
    venv_python = _venv_python()
    if not venv_python.exists():
        print(f"{venv_python} is missing. Set up the watcher's venv first:\n"
              "  python -m venv automation/.venv\n"
              "  automation/.venv/Scripts/pip install -r automation/requirements.txt\n"
              "  automation/.venv/Scripts/playwright install chromium")
        sys.exit(1)

    if not ENV_PATH.exists():
        print(f"{ENV_PATH} is missing. Copy automation/.env.example to "
              "automation/.env and fill in a Telegram bot token (never a shared one).")
        sys.exit(1)
    has_token = False
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN=") and line.split("=", 1)[1].strip():
            has_token = True
            break
    if not has_token:
        print(f"{ENV_PATH} has no non-empty TELEGRAM_BOT_TOKEN line. "
              "Add one from BotFather.")
        sys.exit(1)

    if not BUILD_SETTINGS_PATH.exists():
        print(f"{BUILD_SETTINGS_PATH} is missing. Run: python scripts/init_workspace.py")
        sys.exit(1)


def start() -> int:
    preflight()

    pid = running_pid()
    if pid is not None:
        print(f"watcher already running (PID {pid})")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    venv_python = _venv_python()

    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        process = subprocess.Popen(
            [str(venv_python), "run_watcher.py"],
            cwd=AUTOMATION_DIR,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        live_log = LOG_DIR / "live.out"
        handle = open(live_log, "a", encoding="utf-8")
        process = subprocess.Popen(
            [str(venv_python), "run_watcher.py"],
            cwd=AUTOMATION_DIR,
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
        )

    PIDFILE.write_text(str(process.pid), encoding="utf-8")
    print(f"watcher started (PID {process.pid})")
    print("tail automation/logs/watcher.log")
    return 0


def status() -> int:
    pid = running_pid()
    if pid is not None:
        print(f"watcher running (PID {pid})")
    else:
        print("watcher not running")
    return 0


def main(argv: list[str]) -> int:
    if "--status" in argv:
        return status()
    return start()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
