#!/usr/bin/env python3
"""
Scaffold the company/role folder for one job application.

Usage:
    python3 scaffold.py "Company Name" "Role Title" [--date YYYY-MM-DD]

Creates:  <repo-root>/<YYYY>/<Company>/<YYYY-MM-DD> - <Role>/
and prints the absolute path of that folder (so the pipeline knows where to write).

The role folder is always dated (Role - YYYY-MM-DD), so multiple applications to
the same company with different roles or dates never require renaming previous folders.

The repo root is auto-detected as the parent of this scripts/ folder, so it works
no matter how the drive is mounted in a given session.
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ILLEGAL = r'[\\/:*?"<>|]'  # Windows-illegal filename chars (keep spaces and &)

def clean(name: str) -> str:
    name = re.sub(ILLEGAL, "", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name.rstrip(". ")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("role")
    ap.add_argument("--date", default=dt.date.today().isoformat(),
                    help="YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    try:
        date = dt.date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent          # <repo>/scripts/.. = repo root
    company_name = clean(args.company)
    role_name = clean(args.role)

    target = root / str(date.year) / company_name / f"{date.isoformat()} - {role_name}"
    target.mkdir(parents=True, exist_ok=True)
    print(str(target))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
