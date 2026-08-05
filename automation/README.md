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
py watcherctl.py status          :: running? watching what? with which settings?
py watcherctl.py status --verbose      :: ...plus the endpoints actually polled
py watcherctl.py status --sources-only :: boards, portals and their URLs only
py watcherctl.py status --brief        :: process state + the health table, nothing else
py watcherctl.py start           :: start in the background, detached from this console
py watcherctl.py stop            :: stop every running instance
py watcherctl.py restart
py watcherctl.py poll            :: one fetch/score/notify cycle in the foreground
py watcherctl.py poll --dry-run --source portal:stepstone
py watcherctl.py health          :: per-source status table
py watcherctl.py reset portal:stepstone
py watcherctl.py rescore         :: re-queue postings whose scoring failed outright
py watcherctl.py digest          :: send the digest now
py watcherctl.py resend          :: re-enable pings whose Telegram message is gone
py watcherctl.py resend --min-score 65 --apply
py watcherctl.py rehydrate       :: re-fetch postings stored as one-line teasers
py watcherctl.py rehydrate --dry-run
py watcherctl.py logs -n 50 -f   :: tail watcher.log, -f to follow
```

`resend` exists because deleting a message in Telegram does not delete the watcher's record of
having sent it, and `unnotified_in_band` skips anything with such a record. Clearing the chat —
which is what reorganising a group into forum topics invites you to do — therefore removes every
ping *and* guarantees none of them can ever come back. `resend` probes each recorded ping against
the Bot API and forgets only the records Telegram positively reports as missing, so the postings
become eligible again on the next cycle. It reports without changing anything until `--apply`,
and defaults to `--min-score 75` so recovering one lost ping does not replay a month of them.

`rehydrate` is a one-off repair, and should find nothing to do once it has run. A poll used to
fetch the full ad only when a source sent no description at all; StepStone and hiring.cafe both
send the search tile's opening sentence, which is non-empty, so their bodies were never fetched
and 198 of the first 226 stored postings were filtered, scored and reported on ~300 characters.
`poll.TEASER_CHARS` fixes new postings; this fixes the ones already stored, re-reading the
seniority, years-of-experience, language, contract and work-arrangement columns from the real
body. It is resumable — a posting that gets a body stops matching the query that selects work —
and it runs while the watcher is up, on a copy of the browser profile, because Chromium locks a
profile directory to one process.

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
| `-m watcher.matcher --rescore-degraded` | drop the fallback verdicts left by a scoring outage so the next cycle judges those postings properly |
| `-m watcher.dedupe_check --company Deluxe --role "AI Engineer"` | "have I already applied?" |
| `-m watcher.discover --company X` | probe ATS providers for a company's board token |
| `-m watcher.whoami` | print the chat id of whoever messages the bot next, and a thread id for every forum topic it hears from |
| `-m watcher.health` | per-source failure counts and time to the next auto-retry; `--reset <source>` re-enables a disabled one immediately |
| `-m watcher.status` | every watched board and portal with its URL, then every setting in force |
| `-m watcher.builder --posting <id> --dry-run` | print argv, cwd, prompt, and dedupe verdict; spawn nothing |
| `-m watcher.kb --propose` | build this week's profile_kb.md proposal and print it; write nothing |

`watcher.builder` refuses to start a real build from the command line. Builds begin from a
Telegram reply and nowhere else — that is the whole containment story (see below).

## Files you edit

| file | |
|---|---|
| `sources.toml` | the boards being watched **and the filters applied to them**. `[[ats]]` entries are the reliable tier (public JSON); `[[portal]]` entries are marked `fragile` and disable themselves after repeated failures, then retry themselves once an hour until they recover. `[filters.location]`, `[filters.seniority]`, `[filters.experience]` decide what survives. |
| `config.toml` | intervals, score thresholds, timeouts, `[build] enabled`. No secrets. |
| `profile_kb.md` | matching preferences that grow from your replies. **Tunes matching only** — document facts come from `rules/00-canonical-profile.md` and nowhere else. |
| `.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, plus optional numeric overrides (below). Never commit, never move the tokens into `config.toml`. |

`py watcherctl.py status` prints what these three files add up to — every board and portal with
the URL being watched, then the filters, thresholds, schedules and build settings actually in
force, with any value coming from an environment override marked as such. It reads the same
loaders and URL builders the poller uses, so it cannot show a setting or an endpoint that
differs from the one being polled. It closes with the stored state — counts, the unsent bands,
and the last poll cycle broken down the same way `/status` breaks it down.

### Overriding a number from the environment

Every numeric knob in `config.toml` can also be set as `WATCHER_<SECTION>_<KEY>` — so
`[match] notify_threshold` is `WATCHER_MATCH_NOTIFY_THRESHOLD`. Resolution order is
environment → `config.toml` → built-in default, and a real shell export beats the `.env` file:

```
WATCHER_MATCH_NOTIFY_THRESHOLD=75 py watcherctl.py poll
```

A malformed value logs a warning and falls back to the file — a typo in an env var never
stops the watcher from starting. `automation/.env.example` lists all 20 names.

This is for tuning a live deployment. Anything permanent belongs in `config.toml`, where the
value sits next to the comment explaining why it is that value and stays visible in git —
`.env` is git-ignored, so a threshold parked there is invisible to everyone including you in
three months. Model names, `claude_bin`, and the `[build]`/`[kb]` `enabled` switches are
deliberately **not** overridable: they change what runs, not how hard it runs.

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
the source disables itself and sends **exactly one** 🔴 notification — no retry-every-cycle,
no repeat alert.

It does not stay off, though. Every `[poll] retry_after_minutes` (default 60) a disabled
source gets **one** quiet probe. If it works the source is enabled again and a single 🟢
recovery ping goes out; if it fails the clock restarts and nothing is sent, so a permanently
dead board costs one attempt an hour and stays silent. `watcher.health` and `watcher.status`
both show the time to the next retry. `python -m watcher.health --reset portal:stepstone`
forces it immediately, and `retry_after_minutes = 0` turns auto-recovery off entirely.

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

## Sorting into topics

If the chat is an ordinary one, skip this section — nothing below changes anything for you.

If it is a forum (a supergroup with Topics turned on), the watcher can file each kind of
message into its own topic instead of piling everything into one stream. Fill in `[notify.topics]`
in `config.toml` with the thread ids:

```toml
[notify.topics]
new_posting     = 0    # instant pings and the evening digest
targeted_build  = 0    # postings you approved, kept as a record
processing_build = 0   # queued, building, retrying, declined as a duplicate
failed_build    = 0    # a build that failed, and any retraction
completed_build = 0    # the application is ready, and the run is complete
```

To find the ids, run the same tool that finds the chat id and post a message in each topic you
want to use:

```powershell
python -m watcher.whoami
```

It prints the chat id on the first message, then a thread id for every topic it hears from —
one pass fills in the whole section. General reports as having no thread id, which is correct:
that is the fallback, and where the commands below are answered. (Telegram Web's URL also ends
in the thread id, `.../#-1001234567890_25` → `25`, if you would rather read it off there.)

Everything here is optional and independent:

- **`0` means unset.** Any kind you leave at `0` stays in General, so configuring one topic and
  ignoring the rest is a legitimate setup.
- **No topics at all is the original behaviour**, byte for byte: same chat, no thread, and the
  replies still thread to the message they answer.
- **`targeted_build` sends nothing until you set it.** It is a new record with no equivalent in a
  plain chat, so it stays silent rather than adding traffic to a setup that never asked for it.
- The watcher's own talk — heartbeats, source-disabled and recovered alerts, the weekly
  `profile_kb.md` proposal, interrupted-build notices — stays in **General**, next to the two
  commands below.

One consequence worth knowing: a message routed to a topic loses its `reply_to`. Telegram rejects
the whole send when a reply points across topics, and the topic is the more useful half of the
pair — every routed message already names its posting in its own text.

Replies still work exactly as before. Reply inside whichever topic the posting was announced in.

## Commands

Four commands are answered in the chat itself — in **General**, or anywhere in a non-forum chat.
They are deliberately ignored inside a posting topic, where a stray `/status` is far more likely
a mis-sent reply than a question. They are published to Telegram's own `/` menu at startup.

```
/status                  the full technical report (below)
/threshold               current score cuts, and what is waiting under them
/threshold 60            move the instant-ping cut
/threshold digest 30     move the digest cut
/recheck                 send anything qualifying under the current cut
/recheck 50              …with a different cap for this run
/restart                 restart the watcher process
/restart force           …even with a build running
```

### /status

The report answers "is this thing still working", which is a harder question than "is it up".
`4068 fetched · 0 new · 0 pinged` is the healthy steady state — the same boards returning the
same listings every half hour, all already known — and it is *also* what a watcher whose poller
died three days ago says, because the numbers are frozen at whatever they were when it last
worked. So the report leads with when the last cycle finished and how the listings moved through
it:

```
Last cycle: 11:25:40 (11m ago) · took 41s
    4067 fetched → 4061 already seen → 0 filtered → 6 new
    6 scored · 4 pinged
    Next cycle: 11:54 (in 17m)
```

Past the interval with no cycle, that last line reads `overdue` instead. Below it: stored-state
counts and today's activity, the timestamps of the newest posting, the last score and the last
ping, the unsent bands, source health with any ailing source named, the build queue, the daily
schedule, and the database size. The cycle is written to sqlite, not just held in memory, so a
watcher that came back up thirty seconds ago still reports the poll it did before the restart.

### /threshold and /recheck

`/threshold` rewrites one line of `config.toml` — in place, comments and all — and the watcher
picks it up on its next access without a restart. It refuses when the matching environment
variable is set, since that wins over the file and the edit would look like it worked while
changing nothing.

It sends nothing. A cut change acts retroactively on every posting already scored and skipped
over, so lowering it by thirty points can qualify hundreds at once; firing those as a side
effect of a one-word command is not something to do without being asked. `/recheck` is that ask.

`/recheck` re-runs the poll's own notify step against the current cut. Nothing is re-scored —
every posting already carries a verdict, and what a cut change alters is which of them clears
the bar. The once-per-posting rule still holds, so anything already messaged about stays sent.
Beyond 20 pings (or whatever cap you pass) the rest are left unrecorded and go out with the
evening digest, exactly as instant-ping overflow does during a normal poll.

Postings that have never been scored are a different problem and are left to the next cycle,
which does that in the background.

### /restart

`/restart` re-executes the watcher with the same command line it started under, then confirms
with a short message once it is back. While a build is in flight it refuses and names the builds
instead: restarting kills them, and a killed build has its folder erased. `force` overrides that
when you mean it.

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

## Why nothing appears on screen

The watcher is detached under `pythonw.exe` and has no console, so every console program it
starts would otherwise be given a fresh, visible window — a flash per scoring call, and a cmd
window parked in front of you for the length of a 45-minute build. Every spawn therefore passes
`CREATE_NO_WINDOW` through `scripts/no_console.py`: the CLI, `taskkill`/`tasklist`, the cleanup
script, and the LaTeX and rasterisation passes inside a build. The deliberate exceptions are the
sub-commands `watcherctl.py` runs for you in your own terminal, where the output is the point.

## What reloads without a restart

`config.toml` and `sources.toml` are re-read when they change; a momentarily unparseable file
keeps the last good version and logs once. `build_settings.json` is re-rendered from its
template before every build. `.env` and the watcher's own Python code need a restart — which
is what `/restart` is for, so a code update no longer means finding the terminal.

Nothing about the pipeline itself is cached: `CLAUDE.md`, `rules/`, and `.claude/agents/` are
read fresh by each `claude -p` process, so an agent you add, edit, or delete applies to the very
next build — exactly as it does when you paste a URL into an interactive session.

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

   Do not approximate it here anyway. An earlier version denied `Edit(//C:/**)` and `Edit(~/**)`
   as a stand-in, which was true only while the workspace sat on another drive; the day the
   clone moved under the home directory those two rules denied every write *inside* the
   workspace, and builds spent their whole timeout being refused by their own settings file with
   nothing but a generic "denied by your permission settings" to explain it. The file is
   generated from `build_settings.template.json` by `scripts/build_settings.py`, which now
   refuses to produce one whose `Edit` rules match a path inside the workspace — checked again by
   `init_workspace.py --verify` and before every build.
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

### Retry on a transient upstream failure

An API or gateway outage can land on the *last* turn of a run that has already spent
25 minutes producing a match brief, research note, CV, cover letter, both verifications,
and interview prep — and with cleanup wired in, that erases all of it. So a build that
dies on an upstream failure gets `[build] retries` more attempts (default 1) after
`retry_delay_seconds`, and cleanup only fires once the last one is spent.

Only upstream symptoms qualify: `429`/`5xx`, "overloaded", "temporarily unavailable",
"bad gateway", a reset or refused connection, `fetch failed`. Not a timeout, not a crash,
not a build that wrote the wrong files, and not a 401 — those fail the same way twice and
the second 45 minutes buys nothing. `retries = 0` turns it off.

Each attempt writes its own log (`…-retry1.log`), so the transcript of the failure that
caused the retry survives. The folder from the previous attempt is left in place — the
pipeline scaffolds over it, so the archived posting is not re-fetched, and the tracker
de-dupes on company + role + date. Telegram says what happened rather than going quiet:

```
🔁 Retrying · Deluxe — AI Engineer
API Error: 503 All accounts are temporarily unavailable
Attempt 2 of 2, in 120s. Nothing has been cleaned up yet.
```

### Cleanup after a bad build

A build that fails, crashes, times out, or finishes without a full set of documents is
erased rather than left half-done — after its retries, if it had any. The builder runs `scripts/cleanup_application.py` on
whatever folder appeared, which removes the dated folder, the company folder if that
leaves it empty, the matching tracker row, and that application's `_tmp` scratch. The
tracker workbook itself is never deleted — only the one row whose company + position +
date match, keyed exactly as `append_tracker_entry.py` wrote it.

The Telegram reply says what was removed:

```
❌ Build failed · Deluxe — AI Engineer
latexmk exited 12
🧹 cleaned up: removed folder 2026\Deluxe\2026-08-02 - AI Engineer; removed 1 tracker row(s)
log: 20260802-143355-deluxe-ai-engineer.log
```

The NDJSON build log is kept — it is outside the deliverable folder and is the only
remaining account of what went wrong. Cleanup never fails a build twice over: if the
script itself errors, the reply says so and the log is still there.

Interactive runs behave the same way, through the same script — `CLAUDE.md` ("If A Run
Fails Or Is Abandoned") tells the orchestrator to call it. Nothing about the cleanup is
watcher-specific.

## Autostart

A Task Scheduler "at logon" task running `py watcherctl.py start` (start-in set to this folder),
or the same line as a shortcut in `shell:startup`. Going through `watcherctl` rather than
`pythonw run_watcher.py` directly means a logon while the watcher is already running is a no-op
instead of a second instance fighting the first for the bot token.

The daily heartbeat exists so that silence is distinguishable from a crash — if it stops
arriving, the watcher is down.
