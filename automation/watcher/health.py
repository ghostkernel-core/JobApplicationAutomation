"""Source health: what is working, what disabled itself, and how to revive it.

    python -m watcher.health                 # status table
    python -m watcher.health --reset all     # clear every disable
    python -m watcher.health --reset "portal:stepstone"

A fragile source that fails repeatedly switches itself off rather than
retry-spamming, and says so once in Telegram. It then retries itself once every
`[poll] retry_after_minutes` and announces its own recovery, so `--reset` is
only for forcing that retry early (or for `retry_after_minutes = 0`).
"""

from __future__ import annotations

import argparse
import datetime as dt

from . import store
from .config import load_config, load_sources, source_key
from .logsetup import force_utf8


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", metavar="SOURCE",
                        help='source key to re-enable, or "all"')
    args = parser.parse_args(argv)

    store.init_db()

    if args.reset:
        with store.connect() as conn:
            if args.reset.lower() == "all":
                targets = [row["source"] for row in store.source_health(conn)]
            else:
                targets = [args.reset]
            for target in targets:
                store.reset_source(conn, target)
        print(f"re-enabled: {', '.join(targets) or '(nothing)'}")
        return 0

    configured = {source_key(entry) for entry in load_sources().all_enabled()}
    cooldown = load_config().retry_after_minutes
    with store.connect() as conn:
        rows = store.source_health(conn)

    if not rows:
        print("No polls recorded yet. Run `python -m watcher.poll`.")
        return 0

    print(f"{'source':<34} {'state':<10} {'fails':>5}  last ok")
    for row in rows:
        state = "DISABLED" if row["disabled"] else "ok"
        print(f"{row['source']:<34} {state:<10} {row['consecutive_failures']:>5}  "
              f"{row['last_ok_at'] or 'never'}")
        due = store.retry_due_at(row, cooldown)
        if due is not None:
            left = round((due - dt.datetime.now()).total_seconds() / 60)
            print("    retry due now" if left <= 0 else f"    retry in {left}m")
        elif row["disabled"]:
            print("    no auto-retry — use --reset")
        if row["last_error"]:
            print(f"    {row['last_error'][:110]}")

    seen = {row["source"] for row in rows}
    for missing in sorted(configured - seen):
        print(f"{missing:<34} {'not polled':<10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
