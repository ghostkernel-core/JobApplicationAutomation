"""Configuration loading for the watcher.

Three inputs, deliberately separated:

    config.toml   behaviour knobs (intervals, thresholds, timeouts)
    sources.toml  what to scrape — the file the user edits most often
    .env          secrets, so neither of the above ever holds a token

Paths are derived from this file's location, so the watcher works regardless of
how the drive is mounted or which directory it was launched from.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# automation/watcher/config.py -> automation/ -> repo root
AUTOMATION_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = AUTOMATION_DIR.parent

# Who this workspace belongs to. The loader lives in scripts/ because the
# rendering pipeline is its primary consumer; the watcher needs the same answer
# (deliverable filenames) and must not keep a second copy of it that can drift.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from workspace_identity import load as load_identity  # noqa: E402

STATE_DIR = AUTOMATION_DIR / "state"
LOG_DIR = AUTOMATION_DIR / "logs"
BUILD_LOG_DIR = LOG_DIR / "builds"

CONFIG_PATH = AUTOMATION_DIR / "config.toml"
SOURCES_PATH = AUTOMATION_DIR / "sources.toml"
ENV_PATH = AUTOMATION_DIR / ".env"

DB_PATH = STATE_DIR / "watch.db"
PROFILE_DIGEST_PATH = STATE_DIR / "profile_digest.md"
BROWSER_PROFILE_DIR = STATE_DIR / "browser"
# A consolidation proposal waiting for a yes/no in Telegram. On disk rather
# than in memory so a restart between "sent" and "answered" does not turn the
# reply into an orphan the handler cannot resolve.
KB_PROPOSAL_PATH = STATE_DIR / "kb_proposal.json"

PROFILE_KB_PATH = AUTOMATION_DIR / "profile_kb.md"
DECISIONS_PATH = AUTOMATION_DIR / "decisions.jsonl"
BUILD_SETTINGS_PATH = AUTOMATION_DIR / "build_settings.json"

CANONICAL_PROFILE_PATH = REPO_ROOT / "rules" / "00-canonical-profile.md"


def ensure_dirs() -> None:
    """Create the runtime directories. Safe to call repeatedly."""
    for path in (STATE_DIR, LOG_DIR, BUILD_LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Read automation/.env into os.environ without clobbering real env vars.

    Deliberately minimal — no python-dotenv dependency for four lines of parsing.
    Existing environment always wins, so a shell export can override the file.
    """
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


@dataclass(frozen=True)
class Config:
    """Behaviour knobs from config.toml, with defaults for anything omitted."""

    poll: dict[str, Any] = field(default_factory=dict)
    match: dict[str, Any] = field(default_factory=dict)
    notify: dict[str, Any] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)
    kb: dict[str, Any] = field(default_factory=dict)

    # --- poll -------------------------------------------------------------
    @property
    def interval_minutes(self) -> int:
        return int(self.poll.get("interval_minutes", 30))

    @property
    def max_age_days(self) -> int:
        return int(self.poll.get("max_age_days", 14))

    @property
    def http_timeout(self) -> int:
        return int(self.poll.get("timeout_seconds", 30))

    @property
    def failures_before_disable(self) -> int:
        return int(self.poll.get("failures_before_disable", 3))

    # --- match ------------------------------------------------------------
    @property
    def match_model(self) -> str:
        return str(self.match.get("model", "haiku"))

    @property
    def batch_size(self) -> int:
        return int(self.match.get("batch_size", 8))

    @property
    def notify_threshold(self) -> int:
        return int(self.match.get("notify_threshold", 70))

    @property
    def digest_threshold(self) -> int:
        return int(self.match.get("digest_threshold", 40))

    @property
    def description_chars(self) -> int:
        return int(self.match.get("description_chars", 4000))

    @property
    def match_timeout(self) -> int:
        return int(self.match.get("timeout_seconds", 180))

    # --- notify -----------------------------------------------------------
    @property
    def digest_hour(self) -> int:
        return int(self.notify.get("digest_hour", 19))

    @property
    def heartbeat_hour(self) -> int:
        return int(self.notify.get("heartbeat_hour", 9))

    @property
    def snooze_days(self) -> int:
        return int(self.notify.get("snooze_days", 7))

    # --- build ------------------------------------------------------------
    @property
    def build_enabled(self) -> bool:
        return bool(self.build.get("enabled", False))

    @property
    def claude_bin(self) -> str:
        return str(self.build.get("claude_bin", "claude"))

    @property
    def build_model(self) -> str:
        """Model for the orchestrator of a headless build.

        Sonnet on purpose, per CLAUDE.md: the orchestrator carries the whole
        run's growing context and only does coordination, while each subagent
        pins its own model in frontmatter — so the two writing steps still get
        Opus regardless of what this says. Without the flag the CLI default
        (Opus) applies to the most expensive, least useful seat in the run.
        """
        return str(self.build.get("model", "sonnet"))

    @property
    def build_timeout_minutes(self) -> int:
        return int(self.build.get("timeout_minutes", 45))

    @property
    def duplicate_title_ratio(self) -> float:
        return float(self.build.get("duplicate_title_ratio", 0.8))

    @property
    def duplicate_lookback_days(self) -> int:
        return int(self.build.get("duplicate_lookback_days", 365))

    # --- kb consolidation --------------------------------------------------
    @property
    def kb_enabled(self) -> bool:
        return bool(self.kb.get("enabled", True))

    @property
    def kb_model(self) -> str:
        """Sonnet: condensing a fortnight of reply notes into two or three
        matching rules is a judgement call, and Haiku's version of it is not
        worth reviewing. It runs once a week, so the cost is negligible."""
        return str(self.kb.get("model", "sonnet"))

    @property
    def kb_weekday(self) -> int:
        """0 = Monday, per `datetime.date.weekday()`."""
        return int(self.kb.get("weekday", 6))

    @property
    def kb_hour(self) -> int:
        return int(self.kb.get("hour", 18))

    @property
    def kb_min_decisions(self) -> int:
        """Below this many new decisions there is nothing to generalise from,
        and the run is skipped rather than producing a confident rule out of
        two data points."""
        return int(self.kb.get("min_decisions", 5))

    @property
    def kb_lookback(self) -> int:
        return int(self.kb.get("lookback", 60))

    @property
    def kb_timeout(self) -> int:
        return int(self.kb.get("timeout_seconds", 180))


@dataclass(frozen=True)
class SourceDefaults:
    countries: tuple[str, ...]
    title_allow: tuple[str, ...]
    title_deny: tuple[str, ...]


@dataclass(frozen=True)
class LocationFilter:
    """Where a job may be, expressed the way a person thinks about it.

    Continents, regions, and countries all resolve down to ISO codes before
    anything is compared, so they are additive and interchangeable — `regions =
    ["DACH"]` and `countries = ["DE", "AT", "CH"]` mean the same thing.

    Every list being empty means "no geographic restriction", which is why the
    legacy `[defaults] countries` is still honoured as a fallback: an untouched
    sources.toml keeps behaving exactly as it did.
    """

    continents: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    exclude_countries: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    exclude_cities: tuple[str, ...] = ()
    remote_ok: bool = True
    remote_anywhere: bool = False

    @property
    def allowed(self) -> frozenset[str]:
        from .geo import expand

        return frozenset(expand(
            list(self.continents) + list(self.regions) + list(self.countries)
        ))

    @property
    def excluded(self) -> frozenset[str]:
        from .geo import expand

        return frozenset(expand(self.exclude_countries))

    @property
    def allowed_cities(self) -> frozenset[str]:
        from .geo import expand_cities

        return frozenset(expand_cities(self.cities))

    @property
    def excluded_cities(self) -> frozenset[str]:
        from .geo import expand_cities

        return frozenset(expand_cities(self.exclude_cities))


@dataclass(frozen=True)
class SeniorityFilter:
    """Which rank bands survive. Empty `allow` means every band.

    `unknown` is never filtered — see `roles.level_allowed`. Most good ML
    titles state no level at all, so treating unknown as a rejection would
    discard the bulk of the feed.
    """

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperienceFilter:
    """What to do with a stated years-of-experience bar.

    Default `annotate`: read the number, show it, never act on it. Postings
    overstate the bar routinely and `profile_kb.md` already treats being one or
    two years short as a minor gap, so filtering here would cost real matches
    for a number the employer half-invented.
    """

    mode: str = "annotate"
    max_years: int = 0

    @property
    def filtering(self) -> bool:
        return self.mode.strip().casefold() == "filter" and self.max_years > 0


@dataclass(frozen=True)
class Filters:
    location: LocationFilter = field(default_factory=LocationFilter)
    seniority: SeniorityFilter = field(default_factory=SeniorityFilter)
    experience: ExperienceFilter = field(default_factory=ExperienceFilter)


@dataclass(frozen=True)
class Sources:
    defaults: SourceDefaults
    ats: tuple[dict[str, Any], ...]
    portals: tuple[dict[str, Any], ...]
    filters: Filters = field(default_factory=Filters)

    def enabled_ats(self) -> list[dict[str, Any]]:
        return [entry for entry in self.ats if entry.get("enabled", True)]

    def enabled_portals(self) -> list[dict[str, Any]]:
        return [entry for entry in self.portals if entry.get("enabled", True)]

    def all_enabled(self) -> list[dict[str, Any]]:
        """Every enabled source, tagged with `kind` so callers can dispatch."""
        out: list[dict[str, Any]] = []
        for entry in self.enabled_ats():
            out.append({**entry, "kind": "ats"})
        for entry in self.enabled_portals():
            out.append({**entry, "kind": "portal"})
        return out


def source_key(entry: dict[str, Any]) -> str:
    """Stable identifier for a source, used for health tracking and --source."""
    if entry.get("kind") == "ats" or "provider" in entry:
        return f"ats:{entry.get('company', entry.get('token', '?'))}"
    return f"portal:{entry.get('name', '?')}"


@lru_cache(maxsize=1)
def load_config() -> Config:
    raw = _read_toml(CONFIG_PATH)
    return Config(
        poll=raw.get("poll", {}),
        match=raw.get("match", {}),
        notify=raw.get("notify", {}),
        build=raw.get("build", {}),
        kb=raw.get("kb", {}),
    )


def _strs(section: dict[str, Any], key: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in value if str(v).strip())


def _parse_filters(raw: dict[str, Any], defaults: dict[str, Any]) -> Filters:
    section = raw.get("filters", {}) or {}
    loc = section.get("location", {}) or {}
    sen = section.get("seniority", {}) or {}
    exp = section.get("experience", {}) or {}

    # `[defaults] countries` predates this section and is still the only
    # geography setting in an un-migrated config. It is folded in as an
    # additional source of allowed countries rather than being replaced, so
    # upgrading the watcher never silently widens where jobs may come from.
    countries = _strs(loc, "countries") or _strs(defaults, "countries")

    return Filters(
        location=LocationFilter(
            continents=_strs(loc, "continents"),
            regions=_strs(loc, "regions"),
            countries=countries,
            exclude_countries=_strs(loc, "exclude_countries"),
            cities=_strs(loc, "cities"),
            exclude_cities=_strs(loc, "exclude_cities"),
            remote_ok=bool(loc.get("remote_ok", True)),
            remote_anywhere=bool(loc.get("remote_anywhere", False)),
        ),
        seniority=SeniorityFilter(
            allow=tuple(s.casefold() for s in _strs(sen, "allow")),
            deny=tuple(s.casefold() for s in _strs(sen, "deny")),
        ),
        experience=ExperienceFilter(
            mode=str(exp.get("mode", "annotate")),
            max_years=int(exp.get("max_years", 0) or 0),
        ),
    )


@lru_cache(maxsize=1)
def load_sources() -> Sources:
    raw = _read_toml(SOURCES_PATH)
    defaults = raw.get("defaults", {})
    return Sources(
        defaults=SourceDefaults(
            countries=tuple(defaults.get("countries", [])),
            title_allow=tuple(s.lower() for s in defaults.get("title_allow", [])),
            title_deny=tuple(s.lower() for s in defaults.get("title_deny", [])),
        ),
        ats=tuple(raw.get("ats", [])),
        portals=tuple(raw.get("portal", [])),
        filters=_parse_filters(raw, defaults),
    )


def require_env(name: str) -> str:
    load_env()
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {ENV_PATH} (see automation/README.md)."
        )
    return value
