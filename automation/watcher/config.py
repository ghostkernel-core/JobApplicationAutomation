"""Configuration loading for the watcher.

Three inputs, deliberately separated:

    config.toml   behaviour knobs (intervals, thresholds, timeouts)
    sources.toml  what to scrape — the file the user edits most often
    .env          secrets, so neither of the above ever holds a token

Every numeric knob in config.toml can also be set from the environment, which
wins over the file. The name is mechanical: `WATCHER_<SECTION>_<KEY>`, so
`[match] notify_threshold` is `WATCHER_MATCH_NOTIFY_THRESHOLD`. This is for
tuning a running deployment without editing a tracked file; config.toml stays
the place where a value lives with the comment explaining why it is that value.

config.toml and sources.toml are re-read whenever they change on disk, so a
threshold, an interval, or a new source applies from the next poll cycle without
restarting a watcher that has been running for weeks. A file that is momentarily
unparseable — an editor mid-save, a stray bracket — is logged and the last good
version stays in force until it parses again.

.env is the exception: it is read once, and existing environment variables win
over it, so a changed bot token does need a restart. That is deliberate. Swapping
the credentials of a live long-poll connection underneath itself is not a thing
to do casually between two poll cycles.

Paths are derived from this file's location, so the watcher works regardless of
how the drive is mounted or which directory it was launched from.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

log = logging.getLogger("watcher.config")

_Number = TypeVar("_Number", int, float)

# automation/watcher/config.py -> automation/ -> repo root
AUTOMATION_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = AUTOMATION_DIR.parent

# Who this workspace belongs to. The loader lives in scripts/ because the
# rendering pipeline is its primary consumer; the watcher needs the same answer
# (deliverable filenames) and must not keep a second copy of it that can drift.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from workspace_identity import load as load_identity  # noqa: E402

# Same reasoning, same source: every path this system persists is stored
# relative to the workspace root, and `scripts/workspace_paths.py` is the one
# definition of what that means. The pipeline scripts write those values; the
# watcher reads them back. Two copies of the rule would be two chances to drift.
from workspace_paths import (  # noqa: E402,F401
    FOLDER_RE, YEAR_RE, to_absolute, to_relative,
)

STATE_DIR = AUTOMATION_DIR / "state"
LOG_DIR = AUTOMATION_DIR / "logs"
BUILD_LOG_DIR = LOG_DIR / "builds"

CONFIG_PATH = AUTOMATION_DIR / "config.toml"
SOURCES_PATH = AUTOMATION_DIR / "sources.toml"
ENV_PATH = AUTOMATION_DIR / ".env"

DB_PATH = STATE_DIR / "watch.db"
PROFILE_DIGEST_PATH = STATE_DIR / "profile_digest.md"
BROWSER_PROFILE_DIR = STATE_DIR / "browser"
BROWSER_PROFILE_ENV = "WATCHER_BROWSER_PROFILE"
# A consolidation proposal waiting for a yes/no in Telegram. On disk rather
# than in memory so a restart between "sent" and "answered" does not turn the
# reply into an orphan the handler cannot resolve.
KB_PROPOSAL_PATH = STATE_DIR / "kb_proposal.json"
# Portal search terms generated from the profile, cached against the digest
# that produced them. Regenerated only when that digest changes, so a poll
# cycle never pays for them.
SEARCH_QUERIES_PATH = STATE_DIR / "search_queries.json"

PROFILE_KB_PATH = AUTOMATION_DIR / "profile_kb.md"
DECISIONS_PATH = AUTOMATION_DIR / "decisions.jsonl"
BUILD_SETTINGS_PATH = AUTOMATION_DIR / "build_settings.json"

CANONICAL_PROFILE_PATH = REPO_ROOT / "rules" / "00-canonical-profile.md"

# A restart asked for from Telegram, so the process that comes back knows who
# to report to. On disk because the request and the answer are on opposite
# sides of an `execv` — nothing in memory survives it.
RESTART_MARKER_PATH = STATE_DIR / "restart.json"

# The message kinds that can be routed to their own forum topic, and the
# `[notify.topics]` key each one reads. Order is the order they appear in the
# config file and in `watcherctl status`.
#
# Only these five. Heartbeats, source alerts, the weekly kb proposal and the
# interrupted-build notice deliberately have no topic: they are operational
# rather than about any one posting, and they belong beside `/status` and
# `/restart` in General where a reply reaches the watcher.
TOPIC_KINDS: tuple[str, ...] = (
    "new_posting",       # instant pings and the evening digest
    "targeted_build",    # an approved posting, kept as a record
    "processing_build",  # queued, building, or declined as a duplicate
    "failed_build",      # a build that failed, and any retraction
    "completed_build",   # the application is ready, and the run is complete
)

# The minute each daily job runs at, past its configured hour. Staggered so that
# three of them can never land on the same tick, and off the hour because the
# poll cycle starts ten seconds after boot and repeats from there — a digest
# sharing an instant with a sweep of four thousand postings makes one wait on
# the other.
#
# Config gives out (hour, minute) pairs built from these rather than exposing
# the hour alone, because the scheduler is not the only reader: `/status` and
# `watcherctl status` both quote these times back. When the minutes lived at the
# `run_daily` call sites, both reports advertised a flat ":00" and the morning
# heartbeat looked fifteen minutes late against a status line promising 09:00.
DIGEST_MINUTE = 5
HEARTBEAT_MINUTE = 15
KB_MINUTE = 25
RECALL_MINUTE = 35


def browser_profile_dir() -> Path:
    """Where the shared Chromium context keeps its profile.

    Read at launch rather than at import because it can be overridden, and the
    override is set by a CLI that has not parsed its arguments yet when this
    module is first imported.

    Chromium locks a persistent profile directory to a single process. The
    watcher opens the context on its first browser-backed poll and holds it for
    as long as it runs, so a *second* process that needs the browser — the
    one-off re-hydration sweep — cannot use `state/browser/` at all while the
    watcher is up. Pointing it at a seeded copy is the only way to run the two
    concurrently, which beats taking the watcher offline for an hour.
    """
    override = os.environ.get(BROWSER_PROFILE_ENV, "").strip()
    return Path(override) if override else BROWSER_PROFILE_DIR


def clock(at: tuple[int, int]) -> str:
    """One of those (hour, minute) pairs as "HH:MM", for display."""
    hour, minute = at
    return f"{hour:02d}:{minute:02d}"


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


def env_name(section: str, key: str) -> str:
    """The environment variable that overrides `[section] key` in config.toml."""
    return f"WATCHER_{section}_{key}".upper()


def _from_env(section: str, key: str,
              cast: Callable[[str], _Number]) -> _Number | None:
    """Read one knob from the environment, or None if unset or unusable.

    A malformed value warns and falls through to config.toml rather than
    raising: a typo in an env var should not stop an always-on watcher from
    starting, and the warning names the variable so it is findable in the log.
    """
    raw = os.environ.get(env_name(section, key))
    if raw is None or not raw.strip():
        return None
    try:
        return cast(raw.strip())
    except ValueError:
        log.warning("ignoring %s=%r — not a valid %s; using config.toml",
                    env_name(section, key), raw, cast.__name__)
        return None


@dataclass(frozen=True)
class Config:
    """Behaviour knobs from config.toml, with defaults for anything omitted.

    Numeric knobs resolve in three layers, first hit wins: environment variable
    (`WATCHER_<SECTION>_<KEY>`), then config.toml, then the default argument
    here. Non-numeric settings — model names, `enabled` switches, `claude_bin`
    — are file-only on purpose: they change what runs rather than how hard it
    runs, so they should be visible in a tracked file.
    """

    poll: dict[str, Any] = field(default_factory=dict)
    match: dict[str, Any] = field(default_factory=dict)
    notify: dict[str, Any] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)
    kb: dict[str, Any] = field(default_factory=dict)
    triage: dict[str, Any] = field(default_factory=dict)
    recall: dict[str, Any] = field(default_factory=dict)
    queries: dict[str, Any] = field(default_factory=dict)

    def _num(self, section_name: str, section: dict[str, Any], key: str,
             default: _Number, cast: Callable[[str], _Number] = int) -> _Number:
        override = _from_env(section_name, key, cast)
        if override is not None:
            return override
        return cast(section.get(key, default))  # type: ignore[arg-type]

    # --- poll -------------------------------------------------------------
    @property
    def interval_minutes(self) -> int:
        return self._num("poll", self.poll, "interval_minutes", 30)

    @property
    def max_age_days(self) -> int:
        return self._num("poll", self.poll, "max_age_days", 14)

    @property
    def http_timeout(self) -> int:
        return self._num("poll", self.poll, "timeout_seconds", 30)

    @property
    def failures_before_disable(self) -> int:
        """Allowance for a stable source — a public JSON API or an ATS board."""
        return self._num("poll", self.poll, "failures_before_disable", 3)

    @property
    def fragile_failures_before_disable(self) -> int:
        """Allowance for a source marked `fragile = true` in sources.toml.

        Higher on purpose. The fragile tier is browser-driven scraping against
        pages that carry anti-bot checks and change markup without notice, so an
        isolated failure there is ordinary weather rather than evidence of a
        broken source. Holding it to the same three strikes as a documented JSON
        API is what makes a scraper switch itself off over a bad afternoon.
        """
        return self._num("poll", self.poll, "fragile_failures_before_disable", 6)

    def failures_allowed(self, entry: dict[str, Any]) -> int:
        """The disable threshold for one source, by tier."""
        if entry.get("fragile"):
            return self.fragile_failures_before_disable
        return self.failures_before_disable

    @property
    def retry_after_minutes(self) -> int:
        """Cooldown before a disabled source gets its first retry probe.

        0 = never auto-retry. Each failed probe multiplies this by
        `retry_backoff_factor`, up to `retry_backoff_max_minutes`.
        """
        return self._num("poll", self.poll, "retry_after_minutes", 60)

    @property
    def retry_backoff_factor(self) -> float:
        """Growth per failed probe. 1.0 restores the old flat cooldown."""
        return max(1.0, self._num("poll", self.poll, "retry_backoff_factor",
                                  2.0, float))

    @property
    def retry_backoff_max_minutes(self) -> int:
        """Ceiling on the backoff, so a dead source is still probed daily."""
        return self._num("poll", self.poll, "retry_backoff_max_minutes", 1440)

    # --- match ------------------------------------------------------------
    @property
    def match_model(self) -> str:
        return str(self.match.get("model", "haiku"))

    @property
    def batch_size(self) -> int:
        return self._num("match", self.match, "batch_size", 8)

    @property
    def notify_threshold(self) -> int:
        return self._num("match", self.match, "notify_threshold", 70)

    @property
    def digest_threshold(self) -> int:
        return self._num("match", self.match, "digest_threshold", 40)

    @property
    def description_chars(self) -> int:
        return self._num("match", self.match, "description_chars", 4000)

    @property
    def match_timeout(self) -> int:
        return self._num("match", self.match, "timeout_seconds", 180)

    @property
    def max_score_attempts(self) -> int:
        """How often to retry a posting the scorer could not judge.

        A failed batch used to be written straight to the verdicts table as a
        low-confidence `maybe`, and `store.unscored()` only ever selects
        postings with no verdict row at all — so a sixty-second upstream blip
        buried every posting it touched, permanently. Below this many attempts
        the fallback is held back and the posting is re-scored next cycle; at
        this many it is finally persisted, so a posting the model genuinely
        cannot parse still settles instead of being retried forever.
        """
        return self._num("match", self.match, "max_score_attempts", 3)

    # --- notify -----------------------------------------------------------
    @property
    def digest_hour(self) -> int:
        return self._num("notify", self.notify, "digest_hour", 19)

    @property
    def heartbeat_hour(self) -> int:
        return self._num("notify", self.notify, "heartbeat_hour", 9)

    @property
    def digest_at(self) -> tuple[int, int]:
        """(hour, minute) the evening digest runs at, local time.

        The pairing happens here so no caller can do it differently: the
        scheduler and the two status reports all read this one property, and a
        report that quotes a different minute from the one actually registered
        is indistinguishable, from the chat, from a job running late.
        """
        return self.digest_hour, DIGEST_MINUTE

    @property
    def heartbeat_at(self) -> tuple[int, int]:
        """(hour, minute) the daily heartbeat runs at, local time."""
        return self.heartbeat_hour, HEARTBEAT_MINUTE

    @property
    def snooze_days(self) -> int:
        return self._num("notify", self.notify, "snooze_days", 7)

    # --- notify: forum topics ---------------------------------------------
    @property
    def topics(self) -> dict[str, int]:
        """Configured `[notify.topics]` thread ids, by message kind.

        Absent, blank, or zero means "no topic for this kind", and the entry is
        left out rather than stored as 0 — so `topics` being empty is exactly
        the chat-id-only case, and every routing decision downstream is a
        dictionary lookup that misses.

        A junk value warns and is dropped rather than raising. Getting one topic
        id wrong should cost that one topic, not the watcher's ability to send
        anything at all.
        """
        raw = self.notify.get("topics", {}) or {}
        out: dict[str, int] = {}
        for kind in TOPIC_KINDS:
            value: Any = _from_env("notify_topics", kind, int)
            if value is None:
                value = raw.get(kind, 0)
            try:
                thread = int(value or 0)
            except (TypeError, ValueError):
                log.warning("ignoring [notify.topics] %s = %r — not a thread id; "
                            "messages of that kind go to General", kind, value)
                continue
            if thread > 0:
                out[kind] = thread
        return out

    def topic_for(self, kind: str | None) -> int | None:
        """The thread id a message of this kind belongs in, or None for General.

        None is the answer for an unconfigured kind, an unknown kind, and a
        chat with no topics at all — which is what keeps the chat-id-only setup
        behaving exactly as it did before topics existed.
        """
        if not kind:
            return None
        return self.topics.get(kind)

    @property
    def topics_enabled(self) -> bool:
        """True once at least one topic id is set."""
        return bool(self.topics)

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
        return self._num("build", self.build, "timeout_minutes", 45)

    @property
    def build_retries(self) -> int:
        """Extra attempts after a build dies on a transient upstream failure.

        Only upstream failures qualify — a 503 from the API or the gateway in
        front of it, a rate limit, a dropped connection. A build that timed out,
        crashed, or produced the wrong thing is not retried, because repeating it
        would fail the same way and cost another timeout to find out.

        Default 1. Zero disables retrying; anything above about 2 mostly turns a
        genuine outage into three quarters of lost machine time.
        """
        return max(0, self._num("build", self.build, "retries", 1))

    @property
    def build_retry_delay_seconds(self) -> int:
        """Pause before a retry. Long enough for a brief outage to pass."""
        return max(0, self._num("build", self.build, "retry_delay_seconds", 120))

    @property
    def build_progress_updates(self) -> bool:
        """Whether a build's opener is kept up to date as a step checklist.

        Off, the message says `🛠 Building …` once and nothing more until the run
        ends — which is exactly how it behaved before this existed.
        """
        return bool(self.build.get("progress_updates", True))

    @property
    def build_progress_refresh_seconds(self) -> int:
        """How often the checklist is redrawn while nothing has changed.

        This is the clock on the step currently running, not the step list — a
        transition redraws immediately. So it trades how live that one number
        looks against how many edits a long build spends.
        """
        return max(1, self._num("build", self.build, "progress_refresh_seconds", 30))

    @property
    def build_progress_min_interval_seconds(self) -> int:
        """Floor between two edits, whatever the stream does.

        Parallel phases finish several steps within a second or two of each
        other, and each one on its own would be an API call. This collects them.
        """
        return max(0, self._num("build", self.build,
                                "progress_min_interval_seconds", 5))

    @property
    def duplicate_title_ratio(self) -> float:
        return self._num("build", self.build, "duplicate_title_ratio", 0.8, float)

    @property
    def duplicate_lookback_days(self) -> int:
        return self._num("build", self.build, "duplicate_lookback_days", 365)

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
        return self._num("kb", self.kb, "weekday", 6)

    @property
    def kb_hour(self) -> int:
        return self._num("kb", self.kb, "hour", 18)

    @property
    def kb_at(self) -> tuple[int, int]:
        """(hour, minute) the weekly kb pass runs at, local time."""
        return self.kb_hour, KB_MINUTE

    @property
    def kb_min_decisions(self) -> int:
        """Below this many new decisions there is nothing to generalise from,
        and the run is skipped rather than producing a confident rule out of
        two data points."""
        return self._num("kb", self.kb, "min_decisions", 5)

    @property
    def kb_lookback(self) -> int:
        return self._num("kb", self.kb, "lookback", 60)

    @property
    def kb_timeout(self) -> int:
        return self._num("kb", self.kb, "timeout_seconds", 180)

    # --- triage -------------------------------------------------------------
    @property
    def triage_enabled(self) -> bool:
        """Default False, unlike every other `_enabled` flag in this file.

        Every existing `poll_once` test constructs a bare `Config()`, and this
        default is what keeps that construction from spawning a `claude`
        subprocess. config.toml itself ships `enabled = true` — the file is
        the on switch for a live deployment, the code default is a harness
        guard for everything that is not one.
        """
        return bool(self.triage.get("enabled", False))

    @property
    def triage_model(self) -> str:
        return str(self.triage.get("model", "haiku"))

    @property
    def triage_batch_size(self) -> int:
        """Small on purpose: the prompt is dominated by the profile digest,
        not by per-posting content, since triage never sees a description."""
        return self._num("triage", self.triage, "batch_size", 200)

    @property
    def triage_timeout(self) -> int:
        return self._num("triage", self.triage, "timeout_seconds", 120)

    @property
    def triage_max_per_cycle(self) -> int:
        """Caps how many candidates one poll cycle sends to triage.

        Above the cap, the excess is deferred to the next cycle rather than
        judged — never marked unsure just to clear the queue, and never
        dropped by exhaustion. This is the safety net for the one-time
        backfill, where thousands of never-stored candidates would otherwise
        arrive in a single cycle.
        """
        return self._num("triage", self.triage, "max_per_cycle", 600)

    @property
    def triage_drop_retention_days(self) -> int:
        return self._num("triage", self.triage, "drop_retention_days", 90)

    # --- recall audit --------------------------------------------------------
    @property
    def recall_enabled(self) -> bool:
        """Default False, for the same reason `triage_enabled` is.

        The audit re-scores real postings through the live matcher, which
        means model calls. A bare `Config()` in a test must not be able to
        start one, so the on switch lives in config.toml.
        """
        return bool(self.recall.get("enabled", False))

    @property
    def recall_weekday(self) -> int:
        """0 = Monday, per `datetime.date.weekday()`. Sunday by default, an
        hour after the kb proposal — both are weekly reviews and reading them
        together is the point."""
        return self._num("recall", self.recall, "weekday", 6)

    @property
    def recall_hour(self) -> int:
        return self._num("recall", self.recall, "hour", 20)

    @property
    def recall_at(self) -> tuple[int, int]:
        """(hour, minute) the weekly recall audit runs at, local time."""
        return self.recall_hour, RECALL_MINUTE

    @property
    def recall_sample_size(self) -> int:
        """How many drops one audit re-scores.

        The whole cost of the audit is here: 40 postings hydrated and scored
        through the real matcher, once a week. Raising it buys a tighter
        estimate of the miss rate and nothing else.
        """
        return self._num("recall", self.recall, "sample_size", 40)

    @property
    def recall_lookback_days(self) -> int:
        """Only drops last seen inside this window are counted when sizing the
        sample — a stage that stopped rejecting anything a month ago should not
        keep claiming a share of this week's budget."""
        return self._num("recall", self.recall, "lookback_days", 7)

    @property
    def recall_min_drops(self) -> int:
        """Below this many drops in the window there is nothing to measure, and
        a miss rate computed from four postings would read as a fact."""
        return self._num("recall", self.recall, "min_drops", 20)

    # --- generated portal queries -------------------------------------------
    @property
    def queries_enabled(self) -> bool:
        """Default False. This one changes what is *fetched*, so it moves every
        downstream number at once — it is switched on last and alone."""
        return bool(self.queries.get("enabled", False))

    @property
    def queries_model(self) -> str:
        """Sonnet: it runs once per profile edit, and a bad query set quietly
        narrows every portal until someone notices the boards went quiet."""
        return str(self.queries.get("model", "sonnet"))

    @property
    def queries_per_portal(self) -> int:
        return self._num("queries", self.queries, "per_portal", 8)

    @property
    def queries_timeout(self) -> int:
        return self._num("queries", self.queries, "timeout_seconds", 180)


@dataclass(frozen=True)
class SourceDefaults:
    countries: tuple[str, ...]
    #: There is deliberately no `title_allow`. A hand-written substring list
    #: decided what was ever *seen*, ahead of the profile that decides what is
    #: worth applying to, and it was never measured: `ats:Roche` fetched 200
    #: postings a cycle and stored none, ten of fifteen boards had stored
    #: nothing ever, and a title like "Specialist Advanced Analytics" had no way
    #: through. `title_deny` stays — a substring is a fine way to say never, and
    #: a poor way to say only.
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


def _stamp(path: Path) -> float | None:
    """Modification time, or None if the file is not there right now."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class _Reloader:
    """Caches a parsed config file and re-reads it when the file changes.

    The watcher runs for weeks. Loading config.toml and sources.toml once at
    startup meant every threshold, interval, and source change needed a restart
    — and a restart mid-poll is how a half-finished cycle gets abandoned. So the
    cache is keyed on the file's mtime instead of on the process: edit the file
    and the next thing to ask for it gets the new value.

    A broken edit does not take the watcher down with it. If the file is
    unparseable — which it inevitably is for the moment an editor has it half
    written — the error is logged once and the last good value is served until
    the file parses again. Only the very first load can raise, because at that
    point there is no good value to fall back to.
    """

    def __init__(self, path: Path, parse: Callable[[dict[str, Any]], Any],
                 label: str) -> None:
        self._path = path
        self._parse = parse
        self._label = label
        self._value: Any = None
        self._stamp: float | None = None
        self._complained = False

    def __call__(self) -> Any:
        stamp = _stamp(self._path)
        if self._value is not None and stamp == self._stamp:
            return self._value
        try:
            value = self._parse(_read_toml(self._path))
        except Exception as exc:  # noqa: BLE001 — any parse failure, same answer
            if self._value is None:
                raise
            if not self._complained:
                log.error("%s is unreadable (%s) — still using the last good "
                          "version; fix the file and it reloads by itself",
                          self._label, exc)
                self._complained = True
            # Do not adopt the bad stamp: keep retrying on every access so the
            # fix is picked up the moment it lands.
            return self._value
        if self._complained:
            log.info("%s reloaded cleanly", self._label)
            self._complained = False
        elif self._value is not None:
            log.info("%s changed on disk — reloaded", self._label)
        self._value, self._stamp = value, stamp
        return value

    def invalidate(self) -> None:
        """Force a re-read on the next call, whatever the mtime says.

        For the one case mtime cannot cover: this process wrote the file
        itself, and needs the new value now rather than whenever the timestamp
        resolution happens to notice.
        """
        self._stamp = None


def _parse_config(raw: dict[str, Any]) -> Config:
    # Populate the environment first: the numeric knobs read os.environ on every
    # access, and the CLI entry points (`-m watcher.match --replay`, the ctl
    # subcommands) never call load_env() themselves. Without this an override in
    # .env would apply to the long-running watcher but be silently ignored by
    # the tools used to check its behaviour — the worst possible split.
    load_env()
    return Config(
        poll=raw.get("poll", {}),
        match=raw.get("match", {}),
        notify=raw.get("notify", {}),
        build=raw.get("build", {}),
        kb=raw.get("kb", {}),
        triage=raw.get("triage", {}),
        recall=raw.get("recall", {}),
        queries=raw.get("queries", {}),
    )


_config_reloader = _Reloader(CONFIG_PATH, _parse_config, "config.toml")


def load_config() -> Config:
    """The current config.toml, re-read whenever the file changes on disk."""
    return _config_reloader()


class ConfigWriteError(RuntimeError):
    """A knob could not be changed. The message is shown to the user verbatim."""


def set_number(section: str, key: str, value: int) -> int:
    """Rewrite one numeric knob in config.toml, in place. Returns the old value.

    A line edit, not a re-serialisation: the file is more comment than setting
    — the paragraph above `notify_threshold` is the calibration record for why
    the cut is 70 — and round-tripping it through a TOML writer would drop all
    of that to save two lines of parsing.

    So the assignment is found by key inside its own `[section]` and only its
    right-hand side is replaced. Anything unexpected raises rather than
    guessing: this file is what the watcher reads on the next access, and a
    half-written one takes the whole thing down.

    Refuses when the matching environment variable is set, because that wins
    over the file (see `_num`) and the edit would appear to work while changing
    nothing at all.
    """
    env = env_name(section, key)
    if os.environ.get(env, "").strip():
        raise ConfigWriteError(
            f"{env} is set in the environment and overrides config.toml. "
            f"Change it there, or unset it, and try again.")

    try:
        original = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigWriteError(f"could not read config.toml: {exc}") from exc

    lines = original.splitlines(keepends=True)
    # `[section]` opens at a line of its own and runs until the next header.
    # Matching the key anywhere in the file would find `[kb] hour` from the
    # `[notify]` section, which is the kind of edit nobody notices until the
    # weekly job moves.
    in_section = False
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(-?\d+)(.*)$")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{section}]"
            continue
        if not in_section:
            continue
        match = pattern.match(line)
        if match:
            old = int(match.group(2))
            if old == value:
                return old
            lines[index] = f"{match.group(1)}{value}{match.group(3)}\n"
            break
    else:
        raise ConfigWriteError(
            f"no `{key}` setting under `[{section}]` in config.toml — "
            f"it may have been renamed or removed.")

    # Written via a temporary file in the same directory and moved into place,
    # so a crash mid-write cannot leave the watcher's own config truncated.
    # os.replace is atomic on both platforms this runs on.
    temp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    try:
        temp.write_text("".join(lines), encoding="utf-8")
        os.replace(temp, CONFIG_PATH)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ConfigWriteError(f"could not write config.toml: {exc}") from exc

    # The reloader keys on mtime, and it would pick this up on its own within
    # the second. Dropped explicitly anyway: the caller is about to quote the
    # new value back to whoever asked for the change, and "set to 60" followed
    # by a report still saying 70 is worse than a redundant re-read.
    _config_reloader.invalidate()
    log.info("config.toml: [%s] %s %d -> %d", section, key, old, value)
    return old


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


def _parse_sources(raw: dict[str, Any]) -> Sources:
    defaults = raw.get("defaults", {})
    return Sources(
        defaults=SourceDefaults(
            countries=tuple(defaults.get("countries", [])),
            # A `title_allow` left in an existing sources.toml is ignored
            # rather than rejected: the file is hand-edited and the key going
            # quiet must not stop the watcher. `watcherctl status` says so.
            title_deny=tuple(s.lower() for s in defaults.get("title_deny", [])),
        ),
        ats=tuple(raw.get("ats", [])),
        portals=tuple(raw.get("portal", [])),
        filters=_parse_filters(raw, defaults),
    )


_sources_reloader = _Reloader(SOURCES_PATH, _parse_sources, "sources.toml")


def load_sources() -> Sources:
    """The current sources.toml, re-read whenever the file changes on disk.

    Adding, removing, or disabling a source therefore takes effect on the next
    poll cycle — no restart, and no cycle interrupted halfway through.
    """
    return _sources_reloader()


def sync_build_settings() -> str:
    """Re-render build_settings.json from its template for this workspace.

    Called before every headless build so an edited template — or a clone that
    has been moved since the file was generated — applies to the very next run
    instead of waiting for someone to remember `init_workspace.py`. Raises if the
    result would deny the build write access to the workspace, which is worth
    refusing to start over: the alternative is a build that spends its whole
    timeout being told "denied by your permission settings" about its own files.
    """
    from build_settings import sync  # scripts/ is on sys.path — see above

    return sync(REPO_ROOT, BUILD_SETTINGS_PATH)


def require_env(name: str) -> str:
    load_env()
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {ENV_PATH} (see automation/README.md)."
        )
    return value
