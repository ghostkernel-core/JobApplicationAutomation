"""The whole configured watcher on one screen: sources, URLs, and settings.

    python -m watcher.status            # everything a person needs to see
    python -m watcher.status --verbose  # plus the raw endpoints being polled

`watcherctl status` prints the process state and then calls this, so the answer
to "what is it actually watching, and with which settings" is one command and
never a tour of three TOML files.

Everything here is read-only and derived from the same loaders and URL builders
the poller uses -- no second copy of an endpoint that can drift from the one
being fetched. `watcher.health` remains the narrow per-source table.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import store
from .config import (BUILD_SETTINGS_PATH, CONFIG_PATH, DB_PATH, ENV_PATH,
                     LOG_DIR, PROFILE_KB_PATH, PROFILE_DIGEST_PATH, REPO_ROOT,
                     SOURCES_PATH, Config, Sources, env_name, load_config,
                     load_env, load_sources, source_key)
from .fetchers import source_urls
from .logsetup import force_utf8

WIDTH = 78
# Lines under a source line up with its name, which starts after the state
# column (two spaces of indent plus a ten-wide state field, plus a space).
INDENT = " " * 13


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

def rule(title: str) -> None:
    print(f"\n{title} " + "─" * max(3, WIDTH - len(title) - 1))


def banner(title: str) -> None:
    print("═" * WIDTH)
    print(f" {title}")
    print("═" * WIDTH)


def field(label: str, value: Any, note: str = "") -> None:
    print(f"  {label:<20} {value}{note}")


def wrapped(label: str, items: Iterable[str], empty: str = "(none)") -> None:
    """A long list under a label, wrapped to the terminal width."""
    items = list(items)
    if not items:
        field(label, empty)
        return
    line, first = "", True
    for item in items:
        candidate = f"{line}, {item}" if line else item
        if len(candidate) > WIDTH - 24:
            field(label if first else "", line + ",")
            first, line = False, item
        else:
            line = candidate
    if line:
        field(label if first else "", line)


def env_note(section: str, key: str) -> str:
    """Mark a value that an environment variable is overriding config.toml with.

    Without this a knob read from `.env` looks like it came from the tracked
    file, and the two disagreeing is exactly the thing a status report exists
    to surface.
    """
    name = env_name(section, key)
    return f"   [{name}]" if os.environ.get(name, "").strip() else ""


def state_of(row: sqlite3.Row | None) -> str:
    if row is None:
        return "unpolled"
    if row["disabled"]:
        return "DISABLED"
    if row["consecutive_failures"]:
        return f"fail x{row['consecutive_failures']}"
    return "ok"


def health_detail(row: sqlite3.Row | None) -> str:
    if row is None:
        return "no poll recorded yet"
    return f"last ok {row['last_ok_at'] or 'never'}"


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def _print_source_head(state: str, name: str, kind: str,
                       row: sqlite3.Row | None, parked: bool) -> None:
    if parked:
        print(f"  {'off':<10} {name:<24} {kind}   (enabled = false in sources.toml)")
        return
    print(f"  {state:<10} {name:<24} {kind:<16} {health_detail(row)}")
    if row is not None and row["last_error"] and (row["disabled"]
                                                  or row["consecutive_failures"]):
        # Long enough to carry the failing URL, which is usually the whole
        # diagnosis; `watcherctl logs` has the untruncated version.
        print(f"{INDENT}! {row['last_error'][:160]}")


def print_ats(sources: Sources, health: dict[str, sqlite3.Row],
              verbose: bool) -> None:
    entries = sources.ats
    live = sum(1 for e in entries if e.get("enabled", True))
    rule(f"COMPANY BOARDS  ({live} watched"
         + (f", {len(entries) - live} parked" if len(entries) > live else "") + ")")
    for entry in entries:
        tagged = {**entry, "kind": "ats"}
        key = source_key(tagged)
        row = health.get(key)
        parked = not entry.get("enabled", True)
        _print_source_head(state_of(row), entry.get("company", "?"),
                           entry.get("provider", "?"), row, parked)
        urls = source_urls(tagged)
        if urls.board:
            print(f"{INDENT}{urls.board}")
        if verbose and urls.feed:
            print(f"{INDENT}polled: {urls.feed}")


def print_portals(sources: Sources, health: dict[str, sqlite3.Row],
                  verbose: bool) -> None:
    entries = sources.portals
    live = sum(1 for e in entries if e.get("enabled", True))
    rule(f"PORTALS  ({live} watched"
         + (f", {len(entries) - live} parked" if len(entries) > live else "") + ")")
    for entry in entries:
        tagged = {**entry, "kind": "portal"}
        key = source_key(tagged)
        row = health.get(key)
        parked = not entry.get("enabled", True)
        kind = "fragile" if entry.get("fragile") else "stable"
        _print_source_head(state_of(row), entry.get("name", "?"), kind, row, parked)

        where = entry.get("location") or "(anywhere)"
        radius = entry.get("radius_km")
        scope = f"{where} +{radius}km" if radius else where
        print(f"{INDENT}location: {scope}")
        for query, url in source_urls(tagged).searches:
            print(f"{INDENT}{query}")
            print(f"{INDENT}  {url}")
        if verbose:
            print(f"{INDENT}(the fetcher reads this portal's data endpoint "
                  "behind those pages)")


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def print_filters(sources: Sources) -> None:
    loc = sources.filters.location
    rule("FILTERS — where")
    named = list(loc.continents) + list(loc.regions) + list(loc.countries)
    allowed = sorted(loc.allowed)
    wrapped("accept from", named or ["(anywhere)"])
    if not allowed:
        field("", "→ no geographic restriction")
    elif len(allowed) <= 20:
        wrapped("", allowed)
    else:
        field("", f"→ {len(allowed)} countries "
                  "(`python -m watcher.geo --expand …` lists them)")
    if loc.exclude_countries:
        wrapped("except", loc.exclude_countries)
    if loc.cities:
        wrapped("only in cities", loc.cities)
    if loc.exclude_cities:
        wrapped("not in cities", loc.exclude_cities)
    field("remote", ("counts as a match anywhere in the allowed countries"
                     if loc.remote_ok else "no special treatment"))
    if loc.remote_ok:
        field("remote_anywhere", "yes — remote roles outside those countries too"
              if loc.remote_anywhere else "no — employer must sit in an allowed country")

    rule("FILTERS — what")
    sen = sources.filters.seniority
    wrapped("seniority allow", sen.allow or ["(every band)"])
    wrapped("seniority deny", sen.deny)
    field("", "unknown rank is never filtered")
    exp = sources.filters.experience
    field("experience", f"{exp.mode}"
          + (f" — drop above {exp.max_years} years" if exp.filtering
             else " — shown in the ping, never acted on"))
    wrapped("title must match", sources.defaults.title_allow)
    wrapped("title must not", sources.defaults.title_deny)


def print_behaviour(config: Config) -> None:
    rule("POLLING")
    field("every", f"{config.interval_minutes} min", env_note("poll", "interval_minutes"))
    field("max posting age", f"{config.max_age_days} days", env_note("poll", "max_age_days"))
    field("http timeout", f"{config.http_timeout}s", env_note("poll", "timeout_seconds"))
    field("disable a source", f"after {config.failures_before_disable} consecutive failures",
          env_note("poll", "failures_before_disable"))

    rule("MATCHING")
    field("model", config.match_model)
    field("batch size", config.batch_size, env_note("match", "batch_size"))
    field("notify at", f"score ≥ {config.notify_threshold}  → instant Telegram ping",
          env_note("match", "notify_threshold"))
    field("digest at", f"score ≥ {config.digest_threshold}  → evening digest line",
          env_note("match", "digest_threshold"))
    field("below that", "stored silently, visible via `--replay`")
    field("description sent", f"{config.description_chars} chars",
          env_note("match", "description_chars"))
    field("timeout", f"{config.match_timeout}s", env_note("match", "timeout_seconds"))

    rule("NOTIFICATIONS")
    field("digest", f"{config.digest_hour:02d}:00 local", env_note("notify", "digest_hour"))
    field("heartbeat", f"{config.heartbeat_hour:02d}:00 local",
          env_note("notify", "heartbeat_hour"))
    field("snooze (\"later\")", f"{config.snooze_days} days", env_note("notify", "snooze_days"))

    rule("HEADLESS BUILDS")
    field("enabled", "yes — an approval reply spawns a run"
          if config.build_enabled else "no — approvals are recorded, nothing is built")
    field("cli", f"{config.claude_bin}  (orchestrator model: {config.build_model})")
    field("timeout", f"{config.build_timeout_minutes} min",
          env_note("build", "timeout_minutes"))
    field("duplicate role", f"title similarity ≥ {config.duplicate_title_ratio:.2f} "
          f"within {config.duplicate_lookback_days} days",
          env_note("build", "duplicate_title_ratio"))

    rule("WEEKLY KB PASS")
    if not config.kb_enabled:
        field("enabled", "no")
        return
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
    weekday = days[config.kb_weekday % 7]
    field("runs", f"{weekday} {config.kb_hour:02d}:00 local, model {config.kb_model}",
          env_note("kb", "weekday"))
    field("needs", f"≥ {config.kb_min_decisions} new decisions, "
          f"looks back over {config.kb_lookback}", env_note("kb", "min_decisions"))
    field("applies changes", "only on an explicit yes in Telegram")


# --------------------------------------------------------------------------
# files and stored state
# --------------------------------------------------------------------------

def _short(path: Path) -> str:
    """Repo-relative where possible — the absolute paths are all one prefix."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _size(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "ok"
    kb = path.stat().st_size / 1024
    return f"{kb / 1024:.1f} MB" if kb > 1024 else f"{kb:.0f} KB"


def print_files() -> None:
    rule("FILES")
    for label, path in (("config.toml", CONFIG_PATH), ("sources.toml", SOURCES_PATH),
                        (".env", ENV_PATH), ("profile_kb.md", PROFILE_KB_PATH),
                        ("profile digest", PROFILE_DIGEST_PATH),
                        ("build settings", BUILD_SETTINGS_PATH),
                        ("database", DB_PATH), ("logs", LOG_DIR)):
        field(label, f"{_short(path):<44} {_size(path)}")
    field("", f"(all under {REPO_ROOT})")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    # Never echo any part of the token: this output gets pasted into chats.
    field("telegram token", "set" if token else "MISSING — the watcher cannot notify")
    field("telegram chat", f"pinned (…{chat[-4:]})" if chat
          else "not pinned (first chat to talk to the bot wins)")


def print_state() -> None:
    rule("STORED STATE")
    counts = {
        "postings seen": "SELECT COUNT(*) FROM postings",
        "scored": "SELECT COUNT(*) FROM verdicts",
        "notifications sent": "SELECT COUNT(*) FROM notifications",
        "replies recorded": "SELECT COUNT(*) FROM decisions",
        "builds": "SELECT COUNT(*) FROM builds",
    }
    with store.connect() as conn:
        for label, sql in counts.items():
            field(label, conn.execute(sql).fetchone()[0])
        newest = conn.execute(
            "SELECT first_seen_at, company, title FROM postings "
            "ORDER BY first_seen_at DESC LIMIT 1").fetchone()
    if newest:
        field("newest posting", f"{newest['first_seen_at']}  "
                                f"{newest['company']} — {newest['title']}"[:WIDTH - 24])


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="also show the endpoints actually being polled")
    parser.add_argument("--sources-only", action="store_true",
                        help="skip the settings sections")
    args = parser.parse_args(argv)

    load_env()
    config, sources = load_config(), load_sources()
    store.init_db()
    with store.connect() as conn:
        health = {row["source"]: row for row in store.source_health(conn)}

    banner("WATCHING")
    print_ats(sources, health, args.verbose)
    print_portals(sources, health, args.verbose)

    if not args.sources_only:
        print()
        banner("SETTINGS")
        print_filters(sources)
        print_behaviour(config)
        print_files()
        print_state()

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
