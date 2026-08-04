"""Tidy a finished application folder down to its deliverables.

CLAUDE.md step 09 requires the folder to hold only the final `.tex` + PDF pairs
and one archived posting `.html` before final QA can pass. The obvious way to do
that is `rm`, and the headless guard blocks every `rm -f`/`rm -rf` outright — so
builds spent turns inventing workarounds instead. This script is the sanctioned
way, and the guard stays exactly as strict as it was.

Not to be confused with `cleanup_application.py`, which erases a *failed* run
entirely — folder, tracker row and all. This one runs on a run that *succeeded*
and only clears the scaffolding out of the way.

Three categories, and nothing outside them is touched:

* **kept**    — `.tex`, `.pdf`, `.html`. The deliverables.
* **moved**   — payload JSON, match brief, research note, extracted posting text.
  These go to `_tmp/payloads/<Company> <date> <Role>/` rather than the bin: a
  later change to the rules or templates can then be re-rendered from the
  payloads instead of regenerated from scratch.
* **removed** — LaTeX build artifacts and rasterised page images. Regenerable by
  definition, and the only things this script actually deletes.

Anything it does not recognise is left alone and reported, so an unexpected file
is a line of output rather than a loss.

Idempotent: running it twice, or on an already-clean folder, is a no-op.

Usage:
    python scripts/clean_deliverable.py --folder "<workspace>/2026/ExampleCo/2026-08-02 - ML Engineer"
    python scripts/clean_deliverable.py --folder "<...>" --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_application import ROOT, TMP, check_safe, describe_folder, verb  # noqa: E402

# Deliverables. Everything the folder exists to hold.
KEEP_SUFFIXES = {".tex", ".pdf", ".html"}

# Scaffolding worth keeping, just not here. Payloads especially: re-rendering
# from them is cheap, regenerating them costs a full pipeline run.
MOVE_SUFFIXES = {".json"}
MOVE_NAMES = {"match_brief.md", "research_note.md", "posting.txt", "posting.md"}
MOVE_STEM_SUFFIXES = ("_extracted.txt",)

# Build artifacts. Regenerable from the .tex, so these are simply deleted.
DROP_SUFFIXES = {
    ".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".xdv", ".toc", ".lof",
    ".lot", ".bbl", ".blg", ".nav", ".snm", ".vrb", ".idx", ".ilg", ".ind",
}
DROP_NAMES = {"texput.log"}
DROP_DIR_NAMES = {"_texbuild", "texbuild", "_build", "build", "__pycache__"}


def _is_page_image(path: Path) -> bool:
    """A rasterised PDF page, e.g. `<file_prefix> - CV_p1.png`.

    Only PNG/JPG whose stem ends `_p<number>`; a logo or a screenshot the user
    put in the folder deliberately does not match and is left alone.
    """
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return False
    stem = path.stem
    marker = stem.rfind("_p")
    return marker != -1 and stem[marker + 2:].isdigit()


def _classify(entry: Path) -> str:
    """One of: keep, move, drop, unknown."""
    if entry.is_dir():
        return "drop" if entry.name in DROP_DIR_NAMES else "unknown"

    suffix = entry.suffix.lower()
    name = entry.name.lower()

    if name in DROP_NAMES or suffix in DROP_SUFFIXES or _is_page_image(entry):
        return "drop"
    # `.synctex.gz` and `.run.xml` are two suffixes deep, so `Path.suffix` alone
    # sees only `.gz`/`.xml` and would call them unknown.
    if name.endswith(".synctex.gz") or name.endswith(".run.xml"):
        return "drop"
    if suffix in KEEP_SUFFIXES:
        return "keep"
    if name in MOVE_NAMES or suffix in MOVE_SUFFIXES:
        return "move"
    if any(name.endswith(tail) for tail in MOVE_STEM_SUFFIXES):
        return "move"
    return "unknown"


def payload_dir(folder: Path) -> Path | None:
    """`_tmp/payloads/<Company> <date> <Role>` for this folder.

    The same name `cleanup_application.remove_tmp` looks for, so a run cleaned
    up later still finds and removes what this script moved aside.
    """
    described = describe_folder(folder)
    if described is None:
        return None
    company, role, applied = described
    return TMP / "payloads" / f"{company} {applied.isoformat()} {role}"


def clean(folder: Path, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Tidy one folder. Returns (what happened, what was left unrecognised)."""
    done: list[str] = []
    unknown: list[str] = []
    target = payload_dir(folder)

    for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        verdict = _classify(entry)
        if verdict == "keep":
            continue
        if verdict == "unknown":
            unknown.append(entry.name)
            continue
        if verdict == "drop":
            if not dry_run:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            done.append(f"{verb(dry_run)} {entry.name}")
            continue

        # move
        if target is None:
            unknown.append(entry.name)
            continue
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            destination = target / entry.name
            # The payload dir may hold an older copy from a re-run. It is
            # scratch either way, and the newer one is the one worth keeping.
            if destination.exists():
                destination.unlink()
            shutil.move(str(entry), str(destination))
        done.append(f"{'would move' if dry_run else 'moved'} {entry.name} "
                    f"-> {target.relative_to(ROOT)}")

    return done, unknown


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--folder", required=True,
                    help="the dated deliverable folder to tidy")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    problem = check_safe(folder)
    if problem:
        print(f"Refusing to clean: {problem}", file=sys.stderr)
        return 1
    folder = folder.resolve()

    done, unknown = clean(folder, dry_run=args.dry_run)

    label = folder.relative_to(ROOT.resolve())
    if not done:
        print(f"{label} is already clean.")
    else:
        print(f"Cleaned {label}:")
        for line in done:
            print(f"  {line}")

    if unknown:
        # Not a failure. The point is that a file this script has no rule for is
        # visible rather than quietly deleted or quietly left.
        print("\nLeft in place (not recognised — check these belong here):")
        for name in unknown:
            print(f"  {name}")

    remaining = sorted(p.name for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in KEEP_SUFFIXES)
    print(f"\n{len(remaining)} deliverable(s) remain:")
    for name in remaining:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
