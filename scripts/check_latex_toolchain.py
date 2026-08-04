"""Check whether the LaTeX-first pipeline can compile PDFs."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# scripts/ is this file's own directory, so this resolves however the script
# is invoked. Suppresses the console window each child would otherwise open
# when the parent has none — see scripts/no_console.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import no_console  # noqa: E402


COMMON_WINDOWS_PATHS = [
    r"C:\Program Files\MiKTeX\miktex\bin\x64\latexmk.exe",
    r"C:\Program Files\MiKTeX\miktex\bin\x64\xelatex.exe",
    r"C:\texlive\2026\bin\windows\latexmk.exe",
    r"C:\texlive\2026\bin\windows\xelatex.exe",
    r"C:\texlive\2025\bin\windows\latexmk.exe",
    r"C:\texlive\2025\bin\windows\xelatex.exe",
]


def version(cmd: str) -> str:
    try:
        result = subprocess.run([cmd, "--version"], text=True, capture_output=True,
                                timeout=10, check=False, **no_console.kwargs())
        output = (result.stdout or result.stderr).strip()
        first = output.splitlines()[0] if output else "no version output"
        status = "OK" if result.returncode == 0 else f"NOT USABLE (exit {result.returncode})"
        return f"{status}: {first.strip()}"
    except Exception as exc:  # noqa: BLE001
        return f"found but cannot run: {exc}"


def main() -> int:
    candidates: list[str] = []
    configured = os.environ.get("LATEX_ENGINE")
    if configured:
        candidates.append(configured)
    for name in ("latexmk", "xelatex"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for path in COMMON_WINDOWS_PATHS:
        if Path(path).exists():
            candidates.append(path)

    unique = []
    for item in candidates:
        if item not in unique:
            unique.append(item)

    usable = False
    if unique:
        print("LaTeX engine candidates:")
        for item in unique:
            status = version(item)
            print(f"- {item}: {status}")
            if status.startswith("OK"):
                usable = True
        if usable:
            return 0
        print("ERROR: TeX binaries were found, but none ran successfully.", file=sys.stderr)
        return 1

    print("ERROR: no latexmk or xelatex found.", file=sys.stderr)
    print("Install MiKTeX or TeX Live, or set LATEX_ENGINE to the full path of xelatex/latexmk.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
