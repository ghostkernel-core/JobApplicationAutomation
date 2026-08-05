"""Start, stop and inspect the job watcher from a plain command prompt.

    python watcherctl.py status
    python watcherctl.py start
    python watcherctl.py stop
    python watcherctl.py restart
    python watcherctl.py poll            # one fetch/score/notify cycle, in the foreground
    python watcherctl.py health
    python watcherctl.py reset portal:stepstone
    python watcherctl.py rescore        # re-queue postings the scorer couldn't judge
    python watcherctl.py digest
    python watcherctl.py logs -n 50 -f

`start_watcher.py` in the repository root forwards every one of these
sub-commands here unchanged, defaulting to `start` when given none, so either
spelling works and there is only one implementation behind them.

Unlike everything else under `automation/`, this file imports nothing from
`watcher` and needs no third-party package, so it runs under *any* interpreter
on the machine -- `py watcherctl.py status` works even though the global Python
cannot run the watcher itself. Sub-commands that need the real dependencies are
handed to `automation/.venv` as child processes rather than imported.

Process discovery is by command line, not by a pid file. A pid file goes stale
the moment someone starts the watcher by hand (which is exactly how the running
instance was started), and a recycled pid would then point at an unrelated
process. Asking the OS which python processes mention `run_watcher` costs a few
hundred milliseconds and is always right.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import no_console  # noqa: E402

AUTOMATION_DIR = Path(__file__).resolve().parent
VENV_DIR = AUTOMATION_DIR / ".venv"
LOG_DIR = AUTOMATION_DIR / "logs"
WATCHER_LOG = LOG_DIR / "watcher.log"
STARTUP_LOG = LOG_DIR / "watcher.out"
ENTRYPOINT = AUTOMATION_DIR / "run_watcher.py"

IS_WINDOWS = os.name == "nt"
# How to invoke this control surface, for help text and hints. The root
# `start_watcher.py` forwards every sub-command here, so it overwrites this with
# its own name and the advice quotes the command the user actually typed.
PROG = "python automation/watcherctl.py"
# The marker that identifies a watcher process in a command line. Deliberately
# the module name without extension so it matches both `run_watcher.py` and
# `python -m run_watcher`.
PROCESS_MARKER = "run_watcher"


# --------------------------------------------------------------------------
# interpreters
# --------------------------------------------------------------------------

def venv_python(windowed: bool = False) -> Path:
    """The interpreter that can actually import `watcher`.

    `windowed` picks pythonw.exe, which detaches from the console -- the point
    of a background start. There is no pythonw on POSIX; the caller gets the
    normal interpreter and relies on the fork instead.
    """
    if IS_WINDOWS:
        name = "pythonw.exe" if windowed else "python.exe"
        candidate = VENV_DIR / "Scripts" / name
    else:
        candidate = VENV_DIR / "bin" / "python"
    if candidate.exists():
        return candidate
    # No venv: fall back to whatever is running this file and let the child
    # produce the import error, which says more than anything invented here.
    return Path(sys.executable)


def require_venv() -> Path:
    python = venv_python()
    if not (VENV_DIR / ("Scripts" if IS_WINDOWS else "bin")).exists():
        print(f"warning: no virtualenv at {VENV_DIR}; using {python}", file=sys.stderr)
    return python


# --------------------------------------------------------------------------
# process discovery
# --------------------------------------------------------------------------

@dataclass
class Proc:
    pid: int
    ppid: int
    started: str
    cmdline: str


def _windows_python_processes() -> list[Proc]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "ForEach-Object { $_.ProcessId.ToString() + '|~|' + "
        "$_.ParentProcessId.ToString() + '|~|' + "
        "$_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss') + '|~|' + $_.CommandLine }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30, **no_console.kwargs(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines():
        parts = line.split("|~|", 3)
        if len(parts) == 4 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            procs.append(Proc(int(parts[0]), int(parts[1]),
                              parts[2].strip(), parts[3].strip()))
    return procs


def _posix_python_processes() -> list[Proc]:
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,lstart=,args="],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        # lstart is a fixed 24-character date ("Mon Aug  3 08:42:11 2026").
        rest = parts[2]
        procs.append(Proc(int(parts[0]), int(parts[1]),
                          rest[:24].strip(), rest[24:].strip()))
    return procs


def find_watchers() -> list[Proc]:
    """Live watcher instances, whoever started them and from wherever.

    A venv's pythonw.exe is a small launcher stub that re-executes the real
    interpreter as a child, so one watcher shows up as two matching processes.
    Only the roots are instances -- a match whose parent also matches is that
    stub's child, and killing the root's tree takes it with it.
    """
    procs = _windows_python_processes() if IS_WINDOWS else _posix_python_processes()
    mine = os.getpid()
    matched = [p for p in procs
               if PROCESS_MARKER in p.cmdline.lower() and p.pid != mine
               # `watcherctl stop` must not match itself.
               and "watcherctl" not in p.cmdline.lower()]
    matched_pids = {p.pid for p in matched}
    return [p for p in matched if p.ppid not in matched_pids]


def is_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True,
                             **no_console.kwargs()).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate(pid: int, grace: float = 8.0) -> str:
    """Ask nicely, then insist. Returns what it took."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                       capture_output=True, text=True, **no_console.kwargs())
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return "already gone"

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return "stopped"
        time.sleep(0.4)

    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, **no_console.kwargs())
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.5)
    return "killed" if not is_alive(pid) else "WOULD NOT DIE"


# --------------------------------------------------------------------------
# delegation to the venv
# --------------------------------------------------------------------------

def run_in_venv(args: list[str]) -> int:
    """Run a watcher command in the foreground and stream its output."""
    python = require_venv()
    # The child writes straight to the same console, so anything still sitting
    # in this process's buffer would surface *after* it and read as out of order.
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run([str(python), *args], cwd=str(AUTOMATION_DIR)).returncode


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    procs = find_watchers()
    if not procs:
        print("watcher: NOT RUNNING")
    else:
        label = "watcher" if len(procs) == 1 else f"watcher ({len(procs)} PROCESSES -- see note)"
        print(f"{label}: RUNNING")
        for proc in procs:
            print(f"  pid {proc.pid}  started {proc.started}")
            print(f"    {proc.cmdline}")
        if len(procs) > 1:
            print("\n  Two instances long-polling one bot token produce a stream of")
            print("  telegram.error.Conflict. Stop them all and start one.")

    print()
    if WATCHER_LOG.exists():
        age = (time.time() - WATCHER_LOG.stat().st_mtime) / 60
        print(f"log: {WATCHER_LOG}  (last written {age:.0f} min ago)")
    else:
        print(f"log: {WATCHER_LOG} (does not exist yet)")

    print()
    if args.brief:
        # The old shape: process state plus the per-source health table, and
        # nothing that needs the config files parsed.
        return run_in_venv(["-m", "watcher.health"])
    extra = ["-m", "watcher.status"]
    if args.verbose:
        extra.append("--verbose")
    if args.sources_only:
        extra.append("--sources-only")
    return run_in_venv(extra)


def preflight() -> int:
    """Check the things that fail silently once the process is detached.

    A detached watcher has no console, so a missing token or an unbuilt venv
    produces nothing at all -- it simply never polls. Catching those here means
    the failure lands in the terminal that asked for the start.
    """
    python = venv_python(windowed=True)
    if not python.exists():
        print(f"{python} is missing. Set up the watcher's venv first:\n"
              "  python -m venv automation/.venv\n"
              "  automation/.venv/Scripts/pip install -r automation/requirements.txt\n"
              "  automation/.venv/Scripts/playwright install chromium", file=sys.stderr)
        return 1

    env_path = AUTOMATION_DIR / ".env"
    if not env_path.exists():
        print(f"{env_path} is missing. Copy automation/.env.example to automation/.env "
              "and fill in a Telegram bot token (never a shared one).", file=sys.stderr)
        return 1
    has_token = any(
        line.strip().startswith("TELEGRAM_BOT_TOKEN=") and line.split("=", 1)[1].strip()
        for line in env_path.read_text(encoding="utf-8").splitlines()
    )
    if not has_token:
        print(f"{env_path} has no non-empty TELEGRAM_BOT_TOKEN line. "
              "Add one from BotFather.", file=sys.stderr)
        return 1

    build_settings = AUTOMATION_DIR / "build_settings.json"
    if not build_settings.exists():
        print(f"{build_settings} is missing. Run: python scripts/init_workspace.py",
              file=sys.stderr)
        return 1
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    failed = preflight()
    if failed:
        return failed

    running = find_watchers()
    if running and not args.force:
        # Exit 0, not 1: starting something that is already started is a no-op,
        # not a failure, and an "at logon" scheduler task must not be able to
        # report a red run just because the watcher survived the last session.
        print("Already running -- not starting a second instance:")
        for proc in running:
            print(f"  pid {proc.pid}  started {proc.started}")
        print(f"\nUse `{PROG} restart` to replace it, "
              "or add `--force` if you really mean two.")
        return 0

    python = venv_python(windowed=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # pythonw discards stdio, but a crash before logging is configured still has
    # to land somewhere or the failure is invisible.
    handle = STARTUP_LOG.open("a", encoding="utf-8")
    handle.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    handle.flush()

    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([str(python), str(ENTRYPOINT)], cwd=str(AUTOMATION_DIR),
                     stdout=handle, stderr=handle, stdin=subprocess.DEVNULL, **kwargs)

    # Confirm it survived startup rather than reporting success on a process
    # that died on a missing token two seconds later.
    for _ in range(12):
        time.sleep(0.5)
        found = find_watchers()
        if found:
            print(f"started: pid {found[0].pid}")
            print(f"logs:    {WATCHER_LOG}")
            return 0
    print("Process did not come up. Last lines of the startup log:", file=sys.stderr)
    print(tail(STARTUP_LOG, 20), file=sys.stderr)
    return 1


def cmd_stop(_args: argparse.Namespace) -> int:
    procs = find_watchers()
    if not procs:
        print("watcher: not running -- nothing to stop")
        return 0
    for proc in procs:
        print(f"stopping pid {proc.pid} ... {terminate(proc.pid)}")
    return 0 if not find_watchers() else 1


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    time.sleep(1.0)
    return cmd_start(args)


def cmd_poll(args: argparse.Namespace) -> int:
    if args.dry_run:
        extra = ["-m", "watcher.poll", "--dry-run"]
        if args.source:
            extra += ["--source", args.source]
        return run_in_venv(extra)
    if find_watchers():
        print("note: the background watcher is running; this is an extra cycle "
              "on top of its schedule.\n")
    return run_in_venv([str(ENTRYPOINT), "--once"])


def cmd_health(_args: argparse.Namespace) -> int:
    return run_in_venv(["-m", "watcher.health"])


def cmd_reset(args: argparse.Namespace) -> int:
    return run_in_venv(["-m", "watcher.health", "--reset", args.source])


def cmd_rescore(_args: argparse.Namespace) -> int:
    return run_in_venv(["-m", "watcher.matcher", "--rescore-degraded"])


def cmd_digest(_args: argparse.Namespace) -> int:
    return run_in_venv([str(ENTRYPOINT), "--digest"])


def cmd_resend(args: argparse.Namespace) -> int:
    argv = ["-m", "watcher.notifier", "--forget-vanished",
            "--min-score", str(args.min_score)]
    if args.apply:
        argv.append("--apply")
    return run_in_venv(argv)


def cmd_rehydrate(args: argparse.Namespace) -> int:
    argv = ["-m", "watcher.rehydrate"]
    if args.dry_run:
        argv.append("--dry-run")
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.retry_failed:
        argv.append("--retry-failed")
    return run_in_venv(argv)


def tail(path: Path, lines: int) -> str:
    if not path.exists():
        return f"(no such file: {path})"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def cmd_logs(args: argparse.Namespace) -> int:
    print(tail(WATCHER_LOG, args.number))
    if not args.follow:
        return 0
    if not WATCHER_LOG.exists():
        return 1
    print("\n-- following, Ctrl-C to stop --")
    try:
        with WATCHER_LOG.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog=PROG,
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser(
        "status", help="is it running, what is it watching, with which settings")
    status.add_argument("--brief", action="store_true",
                        help="process state and the per-source health table only")
    status.add_argument("--sources-only", action="store_true",
                        help="skip the settings sections")
    status.add_argument("--verbose", "-v", action="store_true",
                        help="also show the endpoints actually being polled")

    start = sub.add_parser("start", help="start it in the background")
    start.add_argument("--force", action="store_true",
                       help="start even if one is already running (rarely correct)")

    sub.add_parser("stop", help="stop every running instance")

    restart = sub.add_parser("restart", help="stop, then start")
    restart.add_argument("--force", action="store_true", help=argparse.SUPPRESS)

    poll = sub.add_parser("poll", help="run one cycle now, in the foreground")
    poll.add_argument("--dry-run", action="store_true",
                      help="fetch and normalize only; store nothing, notify nobody")
    poll.add_argument("--source", help="limit a dry run to one source key")

    sub.add_parser("health", help="per-source status table")

    reset = sub.add_parser(
        "reset",
        help="re-enable a disabled source now, without waiting for its auto-retry")
    reset.add_argument("source", help='source key, or "all"')

    sub.add_parser(
        "rescore",
        help="re-queue postings the scorer could not judge (a failed batch "
             "degrades to 45/maybe, below the notify threshold, and never "
             "gets looked at again)")

    sub.add_parser("digest", help="send the digest to Telegram now")

    resend = sub.add_parser(
        "resend",
        help="re-enable pings whose Telegram message is gone (a cleared chat "
             "deletes the messages but not the record of them, so the postings "
             "count as notified forever and never resurface)")
    resend.add_argument("--min-score", type=int, default=75,
                        help="only pings at or above this score (default 75)")
    resend.add_argument("--apply", action="store_true",
                        help="without this it only reports what it would do")

    rehydrate = sub.add_parser(
        "rehydrate",
        help="re-fetch postings stored as one-line teasers (stepstone and "
             "hiring.cafe bodies were never fetched, so seniority, the years "
             "bar and the language line were read off the opening sentence)")
    rehydrate.add_argument("--dry-run", action="store_true",
                           help="list what would be fetched, touch nothing")
    rehydrate.add_argument("--limit", type=int, help="stop after this many")
    rehydrate.add_argument("--retry-failed", action="store_true",
                           help="try the ones that failed on an earlier run again")

    logs = sub.add_parser("logs", help="show the tail of watcher.log")
    logs.add_argument("-n", "--number", type=int, default=40)
    logs.add_argument("-f", "--follow", action="store_true")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "status": cmd_status, "start": cmd_start, "stop": cmd_stop,
        "restart": cmd_restart, "poll": cmd_poll, "health": cmd_health,
        "reset": cmd_reset, "rescore": cmd_rescore, "digest": cmd_digest,
        "logs": cmd_logs, "resend": cmd_resend, "rehydrate": cmd_rehydrate,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
