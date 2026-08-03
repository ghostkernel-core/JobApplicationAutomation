"""Open an interactive Claude Code session in this workspace.

    python start_claude.py [claude args...]

Paste a job posting URL (or the full posting text) into the session that
opens, optionally with a note like "add German". CLAUDE.md defines the
11-step pipeline that runs from there — this script's only job is to land in
the right directory with the right binary, the same way on Windows and POSIX.

Preflight checks the things a broken pipeline would otherwise fail on deep
into a run: identity.toml + rules/00-canonical-profile.md are real (not the
freshly cloned stub) via scripts/workspace_identity.py, and `claude` is on
PATH. automation/build_settings.json only matters for headless builds, so its
absence is a warning here, not a blocker.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

STUB_MARKER = "NOT YET WRITTEN"


def preflight() -> None:
    """Verify the workspace is buildable before handing control to `claude`.

    Exits 1 with a clear message on any blocking failure. The missing
    build_settings.json case is headless-only and only warns.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import workspace_identity  # noqa: E402  (path must be set first)

    try:
        workspace_identity.load()
    except Exception as exc:  # noqa: BLE001 — report verbatim, do not narrow
        print(str(exc))
        sys.exit(1)

    profile_path = ROOT / "rules" / "00-canonical-profile.md"
    if not profile_path.exists():
        print(f"{profile_path} is missing. Run scripts/init_workspace.py, "
              "then write the real profile (or run the 12-workspace-init agent).")
        sys.exit(1)
    if STUB_MARKER in profile_path.read_text(encoding="utf-8"):
        print(f"{profile_path} is still the stub ({STUB_MARKER!r} found). "
              "Write the real profile, or run the 12-workspace-init agent.")
        sys.exit(1)

    settings_path = ROOT / "automation" / "build_settings.json"
    if not settings_path.exists():
        print("WARN: automation/build_settings.json is missing — headless "
              "builds won't run until you do: python scripts/init_workspace.py")


def main(argv: list[str]) -> int:
    preflight()

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        print("claude CLI not found on PATH. Install it: "
              "npm install -g @anthropic-ai/claude-code")
        return 1

    print("Paste a job posting URL or the posting text to start a run.")
    print('Add "add German" (or "EN+DE") to also produce Lebenslauf + Anschreiben.')
    print("Pipeline: CLAUDE.md in this workspace.")

    if sys.platform == "win32":
        # which() may resolve to a .cmd shim on Windows (npm installs claude
        # this way), and CreateProcess cannot exec a .cmd directly. shell=True
        # with a quoted command line handles both the .cmd and .exe cases
        # without the caller needing to know which one it got.
        cmd = " ".join([f'"{claude_bin}"', *(f'"{a}"' for a in argv)])
        return subprocess.call(cmd, shell=True)
    return subprocess.call([claude_bin, *argv])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
