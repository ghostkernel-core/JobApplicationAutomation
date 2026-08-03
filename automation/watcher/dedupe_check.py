"""Ask whether a company+role has already been applied to.

    python -m watcher.dedupe_check --company Deluxe --role "AI Engineer"

Exit status is 1 on a hit and 0 on a miss, so this doubles as a shell guard.
"""

from __future__ import annotations

import argparse

from .config import load_config
from .dedupe import find_existing
from .logsetup import force_utf8


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--lookback-days", type=int,
                        default=config.duplicate_lookback_days)
    parser.add_argument("--ratio", type=float, default=config.duplicate_title_ratio)
    args = parser.parse_args(argv)

    hit = find_existing(args.company, args.role, args.lookback_days, args.ratio)
    if hit:
        print(f"DUPLICATE ({hit.similarity:.2f} via {hit.origin}) — {hit.describe()}")
        return 1
    print(f"new — no prior application to {args.company} for {args.role!r} "
          f"in the last {args.lookback_days} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
