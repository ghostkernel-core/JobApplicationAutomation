# Job watcher

An always-on process that watches job boards, scores new postings against the profile, pings
Telegram when something fits, and — on a reply from you — runs the *existing* CV pipeline
headlessly and reports back where the folder landed.

Nothing in `scripts/`, `rules/`, `master/`, or `.claude/agents/` was changed for this. The
watcher is strictly a new caller of the pipeline described in `../CLAUDE.md`.

```
poll sources → dedupe → prefilter (free) → score (haiku) → Telegram
                                              ⟵ you reply "yes"
                          dedupe vs folders + tracker → claude -p → "✅ Built · <path>"
```

## Run it

`watcherctl.py` is the front door. It is the one file here that imports nothing from `watcher`
and needs no third-party package, so **any** interpreter on the machine can run it — including
the global Python, which cannot run the watcher itself. Commands that need the real dependencies
are handed to `.venv` as child processes.

```cmd
cd "<workspace root>\automation"
py watcherctl.py status          :: running? since when? how are the sources?
py watcherctl.py start           :: start in the background, detached from this console
py watcherctl.py stop            :: stop every running instance
py watcherctl.py restart
py watcherctl.py poll            :: one fetch/score/notify cycle in the foreground
py watcherctl.py poll --dry-run --source portal:stepstone
py watcherctl.py health          :: per-source status table
py watcherctl.py reset portal:stepstone
py watcherctl.py digest          :: send the digest now
py watcherctl.py logs -n 50 -f   :: tail watcher.log, -f to follow
```

The repository root has `start_watcher.py`, which takes every one of these sub-commands and
forwards it here unchanged — `python start_watcher.py restart` and `py watcherctl.py restart` are
the same command. It defaults to `start` when given no arguments, so the launcher shortcuts and
scheduler tasks that predate the sub-commands still work. Use whichever is closer to hand; there
is one implementation behind both.

Only one instance may run at a time — two processes long-polling the same bot token produce a
stream of `telegram.error.Conflict: terminated by other getUpdates request`. `start` refuses when
one is already up, and `status` finds instances by command line rather than by a pid file, so it
still sees a watcher someone started by hand. A venv's `pythonw.exe` is a launcher stub that
re-executes the real interpreter as a child, so one watcher is two OS processes; `status` reports
the root and `stop` kills the tree.

Underneath, the entrypoint is still directly usable, and every sub-command below is still
`python -m watcher.<module>`:

```powershell
.\.venv\Scripts\python.exe run_watcher.py              # the real thing, in the foreground
.\.venv\Scripts\python.exe run_watcher.py --once
.\.venv\Scripts\python.exe run_watcher.py --digest
```

### Useful sub-commands

| command | what it does |
|---|---|
| `-m watcher.poll --dry-run` | fetch and normalize, store nothing; `--source ats:Bayer` to isolate one |
| `-m watcher.match --replay 30` | re-score the last 30 stored postings and print a table |
| `-m watcher.match --calibrate` | score postings you already applied to, to sanity-check the threshold |
| `-m watcher.match --refresh-digest` | rebuild the cached profile digest by hand |
| `-m watcher.dedupe_check --company Deluxe --role "AI Engineer"` | "have I already applied?" |
| `-m watcher.discover --company X` | probe ATS providers for a company's board token |
| `-m watcher.whoami` | print the chat id of whoever messages the bot next |
| `-m watcher.health` | per-source failure counts; `--reset <source>` re-enables a disabled one |
| `-m watcher.builder --posting <id> --dry-run` | print argv, cwd, prompt, and dedupe verdict; spawn nothing |
| `-m watcher.kb --propose` | build this week's profile_kb.md proposal and print it; write nothing |

`watcher.builder` refuses to start a real build from the command line. Builds begin from a
Telegram reply and nowhere else — that is the whole containment story (see below).

## Files you edit

| file | |
|---|---|
| `sources.toml` | the boards being watched **and the filters applied to them**. `[[ats]]` entries are the reliable tier (public JSON); `[[portal]]` entries are marked `fragile` and disable themselves after repeated failures. `[filters.location]`, `[filters.seniority]`, `[filters.experience]` decide what survives. |
| `config.toml` | intervals, score thresholds, timeouts, `[build] enabled`. No secrets. |
| `profile_kb.md` | matching preferences that grow from your replies. **Tunes matching only** — document facts come from `rules/00-canonical-profile.md` and nowhere else. |
| `.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Never commit, never move into `config.toml`. |

Generated, not edited: `state/watch.db` (postings, verdicts, notifications, decisions, builds,
source health), `state/profile_digest.md` (cache, regenerates when the canonical profile
changes), `decisions.jsonl` (append-only audit of every notification and its outcome),
`logs/watcher.log`, `logs/builds/`.

## Where the boards are set

Both tiers live in `sources.toml`.

**Company boards** — `[[ats]]`, one per company, identified by provider plus board token.
`python -m watcher.discover --company "Celonis"` finds the token and prints the block; the slug
is not guessable and slugs collide, so check the sample titles it shows.

**Aggregators** — `[[portal]]`, keyed by `name`, which selects the fetcher. There is no URL to
configure: each portal reads a different internal shape, so a fourth one means a new
`watcher/fetchers/<name>.py`. What exists:

| name | how it reads | fragile | location knob |
|---|---|---|---|
| `arbeitsagentur` | public JSON API, static client key | no | `location` = city, `radius_km` |
| `hiringcafe` | Next.js `_next/data` endpoint, via the shared browser | yes | none — worldwide, geography is filtered below |
| `stepstone` | the search page's `__PRELOADED_STATE__`, via the same browser | yes | `location` → `/in-<slug>`; `"Deutschland"` is all of it |

`queries` is search text, verbatim and per portal. Broad is fine — the `[defaults] title_allow`
list decides relevance and is stricter than any of the three (a real run: 209 fetched, 92 kept).

The two fragile ones share **one persistent Playwright context** in `state/browser/`, so a
Cloudflare challenge solved once is reused instead of re-triggered every 30 minutes. Playwright
is a lazy, soft dependency: a machine without it runs the ATS tier and Arbeitsagentur normally
and disables those two after `failures_before_disable` — it does not fail to boot.

They will break, and the contract when they do is deliberate: three consecutive failures, then
the source disables itself and sends **exactly one** notification. No retry loop, no repeat.
`python -m watcher.health --reset portal:stepstone` puts it back.

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium   # ~150 MB, once
python -m watcher.poll --dry-run --source portal:stepstone
```

## Filters

All of them live in `sources.toml`. Geography is expressed at whatever grain suits — continents,
regions, countries, cities — and the four lists add up rather than override:

```toml
[filters.location]
regions = ["EU", "EFTA"]      # EU EFTA EEA DACH BENELUX NORDICS BALTICS IBERIA CEE UK_IE EMEA
cities  = []                  # empty = any city inside the allowed countries
exclude_countries = []
remote_ok = true              # a remote posting survives a country miss...
remote_anywhere = false       # ...but only if the employer is still in-region

[filters.seniority]
allow = ["junior", "mid", "senior", "lead", "staff", "principal"]
deny  = ["intern", "executive"]

[filters.experience]
mode = "annotate"             # read the years bar, show it, never filter on it
```

Every filter here is **one-sided**: it rejects only what it positively resolved. A location that
parses to no country, a title stating no rank, a posting with no experience bar — all pass
through to the matcher. A filter that drops what it *failed to read* turns each new board format
into silent data loss, so uncertainty costs one scored posting and never a missed job. `unknown`
seniority is unfilterable for the same reason: most good ML titles state no rank at all.

Seniority is read from the **title only**. The body looked like free evidence and is not — a real
archived posting, "Solutions Architect – AI & Data Integration", read as `intern` purely because
an "Intern – Legal & Regulatory Affairs" tile sat in its page sidebar.

Years of experience is annotated, never enforced (`mode = "filter"` with `max_years` exists if
that changes). Postings overstate the bar routinely and `profile_kb.md` already treats being one
or two years short as a minor gap.

```powershell
python -m watcher.prefilter --explain          # every rule, fully expanded
python -m watcher.geo --expand EU DACH         # what a region name becomes
python -m watcher.geo --resolve "Düsseldorf, Germany"
python -m watcher.roles --title "Teamleiter Data Science"
python -m watcher.poll --dry-run --show-filtered
```

## Replying

Reply *to the notification message* — several can be in flight, so a bare message is ambiguous.

```
yes                      build it
yes, add German          anything after "yes" is passed to the pipeline verbatim
no / no, too much devops the reason is appended to profile_kb.md
later                    snooze for [notify] snooze_days
build 3                  promote line 3 of a digest
```

## What the watcher learns

Two halves, deliberately unequal in trust.

A reply that carries a reason ("no, too much devops") is appended to `profile_kb.md` verbatim,
under `## Learned from decisions`, next to the posting it was about. No model, no paraphrase —
the file only gains lines you actually typed.

Once a week (`[kb]` in `config.toml`, Sunday 18:25 by default) those lines are read back and
condensed into proposed `Prefer` / `Avoid` rules. **The proposal is sent to Telegram and nothing
is written until you reply `yes`.** `no` discards it. Anything else leaves it pending, so a
stray message can neither apply nor lose it.

What the pass may do is narrow on purpose: it only *adds* bullets, only to those two sections,
and never touches `## Hard filters` or `## How to weigh gaps` — those are calibrated by hand
against the applications already in `2026/`. It cannot delete the raw lines it generalised from
either, so every rule keeps its audit trail. Added bullets are tagged `<!-- proposed YYYY-MM-DD -->`,
and undoing one is deleting one line.

It skips itself when there is nothing to learn from: fewer than `min_decisions` new decisions,
or none of them carrying a reason. A bare yes/no teaches the matcher nothing.

`python -m watcher.kb --propose` runs the whole thing as a dry run and prints the JSON.

## Builds and containment

Headless builds run `claude -p --permission-mode bypassPermissions --settings
build_settings.json`, cwd pinned to the workspace root, with the URL and note on stdin.
`bypassPermissions` is what makes an unattended 11-step run possible; two layers narrow it.

1. **`build_settings.json` deny rules.** Explicit denies still apply under `bypassPermissions`.
   Note the syntax traps: `Write(...)` rules are inert (only `Edit(...)` is matched, and it
   covers every file-editing tool), and a single leading `/` anchors at the settings file's
   directory — filesystem-absolute paths need `//`. Deny beats allow and there is no negation,
   so a deny list *cannot* express "everywhere except the workspace"; that job belongs to the
   hook.
2. **`hooks/guard.py`**, a blocking `PreToolUse` hook on Bash/Edit/Write/Read. Exit 2 blocks
   the call and its stderr becomes the reason the model sees. It blocks destructive commands,
   credential and system paths, and any *write* outside the workspace; reads may roam, because
   the canonical profile cites work under `D:\Research Archive`. Every decision is appended to
   `logs/builds/guard.log`. `--self-test` runs the fixture table.

   The hook's `command` must be the **absolute venv interpreter path**. Bare `python` resolves
   to the Microsoft Store alias stub on this machine and the hook silently never fires — the
   symptom is an empty `guard.log`.

**What this is not.** Claude Code's OS-level sandbox does not support native Windows, and
neither deny rules nor the hook reach *inside* a subprocess: once `python
scripts/render_latex_application.py` starts, it can write anywhere your account can. This is
defence against a confused agent, not a hostile one. What actually bounds the blast radius is
the pinned cwd, the full NDJSON stream in `logs/builds/`, `guard.log`, and the fact that a
build only ever starts from an explicit reply. If that stops feeling sufficient, moving builds
into WSL2 buys real enforcement, at the price of reinstalling the LaTeX and Python toolchain
there.

`[build] enabled = false` in `config.toml` stops builds at the source: a reply is still
recorded, nothing is spawned.

### Duplicates

Three layers. A posting fingerprint (never re-notify), a cross-source collapse (the same role
on hiring.cafe and the company's own board is one posting), and at build time a scan of
`<YYYY>/<Company>/` plus the tracker workbook — which is what catches roles you applied to
by hand, before this existed.

A folder alone is not proof: `2026/Synergeticon/2026-07-13 - AI Engineer` holds no PDFs at all.
A folder hit blocks a rebuild only when both `<file_prefix> - CV.pdf` and
`<file_prefix> - Cover Letter.pdf` (`<file_prefix>` from `identity.toml`) are present; otherwise the build proceeds and the
message says it is rebuilding over an incomplete attempt.

## Autostart

A Task Scheduler "at logon" task running `py watcherctl.py start` (start-in set to this folder),
or the same line as a shortcut in `shell:startup`. Going through `watcherctl` rather than
`pythonw run_watcher.py` directly means a logon while the watcher is already running is a no-op
instead of a second instance fighting the first for the bot token.

The daily heartbeat exists so that silence is distinguishable from a crash — if it stops
arriving, the watcher is down.
