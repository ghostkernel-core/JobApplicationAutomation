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

## End to end

Two ways in, one pipeline. The watcher finds work and asks; you paste a URL when you have found
it yourself. Both end up running `../CLAUDE.md` against a URL and an optional note, which is why
there is no second set of instructions anywhere — see **One pipeline, two front doors** below.

The numbers below are the shipped `config.toml`; `/status` prints the ones actually in force.

### 1 · Poll — every 30 minutes

```
 sources.toml
      │
      ▼
 fetch each source ─┬─ failed 3× running (6× if fragile)? ──► disabled, one alert,
      │             │                                        probed again after 1h,
      │             │                                        2h, 4h … capped at daily
      │             └─ failed structurally? ──► parked: no auto-retry at all,
      │                                        the fetcher needs fixing
      ▼
 for each posting
      │
      ├─ fingerprint already stored, or a loose company+title match in 30 days?
      │        └──► dropped as already known
      │
      ├─ prefilter (free): title allow/deny, posted ≤ 14d ago, hard blockers,
      │  location, seniority — years is extracted and shown, not enforced
      │        └──► dropped, with a reason (`poll --dry-run` prints them)
      │
      ├─ body under 800 chars? fetch the real ad and run the prefilter again
      │        └──► dropped — the hard blockers usually sit in the body, not the title
      │
      └──► stored in state/watch.db
```

The second prefilter pass matters more than it looks: StepStone and hiring.cafe both ship the
search tile's opening sentence, which is *not* empty, so the 800-char floor is what tells a
teaser from an ad. See `rehydrate` under **Run it** for the back catalogue that predates it.

Fetching the real ad means rendering the employer's own page, and Workday, BambooHR and HiBob
serve an empty shell that fills from an XHR a second or three after the page reports itself
loaded. Reading it once on a fixed delay returned *nothing at all* for those, and the teaser was
kept without anything raising or being logged; hiring.cafe surfaces employers' own ATS pages
rather than its own, so it met them most often, and 15 of its first 85 postings were scored on a
teaser. `page_text` now re-reads until the text stops growing, bounded by a budget. A page that
was already rendered returns on the first read and pays nothing extra, and one that is genuinely
short — a login wall, a posting taken down — is believed rather than waited out.

### 2 · Score — same cycle, everything unscored

```
 postings with no verdict row
      │
      ▼
 haiku, 8 at a time, 4000 chars of description each
      │     ├─ profile digest + profile_kb.md go in with every batch
      │     │
      │     └─ scorer failed (timeout, bad JSON, upstream 503)?
      │            └──► no verdict written, so the next cycle picks it up again.
      │                 After 3 attempts the fallback score is stored as the
      │                 real answer — by then it is the posting, not the weather.
      ▼
 verdict row: score 0–100, verdict, reasons, stop-and-ask flag
```

**A missing verdict is the work queue.** Nothing carries a "needs scoring" flag; `unscored()`
selects on the absence of a verdict, so deleting one re-queues the posting. That is how
`/rescore`, `rehydrate`, and `--rescore-before` all work.

### 3 · Notify — by band

```
 score ≥ 65 (notify_threshold) ──► instant ping, at most 6 per cycle
      │                             └─ beyond 6: left unrecorded, so they fall
      │                                into the digest instead of vanishing
 score ≥ 40 (digest_threshold) ──► one line in the 19:00 digest
 below 40                      ──► stored silently; `match --replay` to see them
```

A `notifications` row is written *only when a message is actually sent*, and
`unnotified_in_band` skips anything that has one. So a ping cannot repeat — and deleting the
message in Telegram does not bring it back, which is what `resend` is for.

Also on the clock: the 09:00 heartbeat, and the weekly rules proposal (Sunday 18:00), which
never writes `profile_kb.md` without an explicit yes.

### 4 · Reply — you answer the ping

Replies are resolved by *which message you replied to*, so several can be in flight at once.

```
 "no" / "skip" / "nein" ──► decision recorded, note learned into profile_kb.md
 "later" / "snooze"     ──► snoozed 7 days
 "yes" / "ok" / "build" ──► approved.  Anything after the keyword is passed
       │                    through verbatim: "yes, add German",
       │                    "yes, apply as Data Scientist"
 "3" / "build 3"        ──► approves line 3 of a digest
 anything else          ──► "not sure what to do with that". A stray message
                            can never start a build.
```

A reply to a message the *watcher* asked something with is an answer to that
question instead, whatever it says — see **When the pipeline stops to ask**.

### 5 · Build — one at a time

```
 approved
      │
      ├─ [build] enabled = false? ──► recorded, nothing spawned
      │
      ├─ already applied? folder tree + tracker, 365 days back, title
      │  similarity ≥ 0.8, company matched on its normalised key
      │        └──► duplicate — build refused, prior folder named
      │        └──► an *incomplete* prior folder does not block; it is rebuilt over
      │
      ▼
 queued (one build at a time; "N ahead" if something is running)
      │
      ▼
 claude -p, stdin = "<url>\n<note>" and nothing else
      │  cwd pinned to the workspace · bypassPermissions · guard.py on every call
      │  45-minute timeout · full NDJSON stream to logs/builds/
      │
      ├─ live checklist: the "🛠 Building …" message is edited in place, one row
      │  per pipeline step with its duration, refreshed every 30s
      │
      ├─ CV + cover letter appear on disk ──► "ready" message goes out *now*,
      │  without waiting for Interview Prep (the last ~7 of ~40 minutes)
      │
      ▼
 process exits — and then the disk is asked, never the model
```

### 6 · Outcome

```
 done             exit ok + every required PDF present        → Completed topic
 done (salvaged)  died late, but the PDFs are there anyway.   → Completed topic
                  The study aid is optional; the application is the point.
 duplicate        blocked before spawning                     → Targeted topic
                  (reply "yes anyway" to build it regardless)
 needs_decision   clean exit, no documents — the pipeline hit a stop-and-ask.
                  Its closing words are relayed verbatim      → Targeted topic
 incomplete       ran, wrote some of the documents            → Failed topic
 failed           timeout, crash, upstream error              → Failed topic
 cancelled        stopped by /cancel                          → Failed topic
 interrupted      the watcher restarted mid-build             → General
                  (reply "yes" again to run it afresh)

 failed, incomplete and cancelled erase their folder via
 scripts/cleanup_application.py — dated folder, empty company folder, the
 tracker row, and that run's _tmp scratch. A folder with no PDFs reads as
 "already applied" to everything that scans the tree, including this watcher.

 `needs_decision` keeps its folder while the question is open: the run is
 paused, not abandoned, and the archived posting, Match Brief and Research
 Note are what make answering it cheap. It is erased when you decline the
 question, cancel it, or it expires unanswered after two days.

 `interrupted` is the exception: the process was killed, so nothing ran to
 tidy up. The next build for that role reports the partial folder and
 rebuilds over it.
```

`retries` is currently `0`, so each build gets one attempt. Set it back to `1` and a build that
dies on a *transient upstream* failure — a 503, a rate limit, a dropped connection — gets a
second run 120s later, after its partial folder has been erased. See **Retry on a transient
upstream failure**.

### One pipeline, two front doors

```
  ┌─────────────────────────────┐        ┌──────────────────────────────────┐
  │ watcher                     │        │ you, in Claude Code              │
  │ approved posting            │        │ paste a URL (+ optional note)    │
  └──────────────┬──────────────┘        └───────────────┬──────────────────┘
                 │                                       │
     claude -p, stdin = "<url>\n<note>"        the same text, typed
                 │                                       │
                 └───────────────┬───────────────────────┘
                                 ▼
                    ../CLAUDE.md  §Trigger
                                 │
                          00 archive posting
                                 │
                 ┌───────────────┴───────────────┐
            01A match brief              01B research note
                 └───────────────┬───────────────┘
                 ┌───────────────┴───────────────┐
            02A CV → 03A verify        02B letter → 03B verify
                 └───────────────┬───────────────┘
                                 │  [04A German, if asked for]
                 ┌───────────────┴───────────────┐
        05A render → 06A QA              04B interview prep
                 │                              → 05B render → 06B QA
      ready ◄────┤ the employer-facing              │
                 │ half is finished here            │
                 └───────────────┬──────────────────┘
                    08 proofread → 09 final QA → clean
                          → 10 tracker row → 11 report
```

The two tracks after verification are genuinely concurrent, and the split is the reason the
watcher can say "ready" early: the CV and cover letter render the moment they pass, and interview
prep — the longest step, and the only one no employer sees — cannot take them down with it.

`build_prompt()` is one line — `f"{url}\n{note}"` — and that is deliberate. Every sentence of
scaffolding added there would be a second source of truth competing with `CLAUDE.md`, and the
day the two disagree the pipeline behaves differently depending on who started it. `answer_prompt()`
is the same idea for a resumed run: your answer, stripped, and nothing else. That session already
holds `CLAUDE.md`, the Match Brief and the folder, so re-stating any of it would be that second
source of truth arriving by another route.

What actually differs between the two doors:

| | pasted into the CLI | spawned by the watcher |
|---|---|---|
| stop-and-ask | you answer, the run continues | the question goes to Telegram; you answer there and the same session resumes |
| duplicate check | the pipeline's own | plus the watcher's, before spawning |
| permissions | your session's | `bypassPermissions` + `build_settings.json` + `guard.py` |
| timeout | none | 45 minutes, then the process tree is killed |
| tracker row | step 10 | step 10, unchanged |

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

**A body arriving deletes the posting's verdict**, which is what re-queues it: `unscored()`
selects on the absence of one, so the next cycle judges it on the real ad. The first version of
the sweep replaced the description and left the score, which corrected the record and kept every
wrong answer in it. The rows filled before that changed are re-queued once by
`rehydrate --rescore-before <ISO timestamp>` — no fetching, and the cutoff comes from the
operator because nothing records when a body arrived. It also drops the *digest* notification
rows for those postings, since `unnotified_in_band` would otherwise mean a corrected score can
never reach you; postings that were pinged outright, or that you have already replied to, keep
their record and stay quiet.

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
open questions, source health), `state/profile_digest.md` (cache, regenerates when the canonical profile
changes), `decisions.jsonl` (append-only audit of every notification and its outcome),
`logs/watcher.log`, `logs/builds/`.

Two log files, and only one of them is the log. `logs/watcher.log` is it: formatted, rotated
at 5 MB with three backups, and what `watcherctl logs` reads. `logs/watcher.out` is the raw
stdout and stderr of the windowed process, kept for the crash that happens before logging is
configured and has nowhere else to go. It is a duplicate of everything else, so it is cut back
to its last 200 KB on every restart and again if a single run ever pushes it past a megabyte.

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

`queries` is search text, verbatim and per portal. Broad is fine — there is no title allow-list
to narrow it further; relevance is judged by `[triage]` against the profile, downstream of
fetching.

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
yes anyway               build it despite a duplicate verdict
```

### Answering a question the watcher asked

A build can end by asking you something — a stop-and-ask from the pipeline, or a duplicate it
declined. Those messages are answerable, and answering one is not the grammar above:

- **Reply to the message that asked**, and whatever you type goes to the run that asked it. Free
  text is passed through verbatim rather than parsed, so `no sponsorship needed, I already hold
  a permit` reaches the model as an answer instead of being read as a decline.
- **Or just send a message in that topic** when exactly one question is open there. Two or more
  and they are listed rather than guessed between. With none open, a bare message still starts
  nothing — that rule has not moved.
- **A bare `no`** (nothing after it) declines: the question is closed and the paused run's folder
  is erased. A duplicate's `no` erases nothing, because the folder it matched belongs to a
  finished application.

`/pending` lists everything waiting on you, numbered; `/cancel <n>` closes one.

## Sorting into topics

If the chat is an ordinary one, skip this section — nothing below changes anything for you.

If it is a forum (a supergroup with Topics turned on), the watcher can file each kind of
message into its own topic instead of piling everything into one stream. Fill in `[notify.topics]`
in `config.toml` with the thread ids:

```toml
[notify.topics]
new_posting     = 0    # instant pings and the evening digest
targeted_build  = 0    # postings you approved, any question a run stops to ask,
                       #   and any duplicate it declined
processing_build = 0   # queued, building, retrying
failed_build    = 0    # a build that failed or was cancelled, and any retraction
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
- **The approval record sends nothing until `targeted_build` is set.** It is a new message with no
  equivalent in a plain chat, so it stays silent rather than adding traffic to a setup that never
  asked for it. Questions routed to the same topic are not affected — those have somewhere to go
  either way, and fall back to General like everything else.
- The watcher's own talk — heartbeats, source-disabled and recovered alerts, the weekly
  `profile_kb.md` proposal, interrupted-build notices — stays in **General**, next to the
  commands below.

One consequence worth knowing: a message routed to a topic loses its `reply_to`. Telegram rejects
the whole send when a reply points across topics, and the topic is the more useful half of the
pair — every routed message already names its posting in its own text.

Replies still work exactly as before. Reply inside whichever topic the posting was announced in.

## Commands

The commands are answered in the chat itself — in **General**, or anywhere in a non-forum chat.
They are deliberately ignored inside a posting topic, where a stray `/status` is far more likely
a mis-sent reply than a question. They are published to Telegram's own `/` menu at startup.

```
/status                  the full technical report (below)
/pending                 everything waiting on you, numbered
/build <url>             build a posting without hunting for its ping
/cancel                  stop the running build, or drop the next queued one
/cancel 2                close question 2 from /pending
/threshold               current score cuts, and what is waiting under them
/threshold 60            move the instant-ping cut
/threshold digest 30     move the digest cut
/recheck                 send anything qualifying under the current cut
/recheck 50              …with a different cap for this run
/rescore                 re-queue the postings the scorer could not judge
/restart                 restart the watcher process
/restart force           …even with a build running
```

### /pending

Three separate kinds of waiting, which used to live in three different places or nowhere at all:

```
❓ Waiting on an answer
1. RWE — AI Data Engineer · stopped to ask · 3h ago
    The Match Brief just came back with a real integrity flag…
2. Roche — Machine Learning Engineer · duplicate · 1d ago
    Already applied — Roche — Data Scientist ML Engineer · applied 2026-06-24 · …
Reply to the message that asked, or just send your answer here if only one is open.

🛠 Builds in flight
• Bayer — Data Scientist · running

📨 Pinged, never answered (4, last 7 days)
• Merck — ML Engineer
…

12 older ping(s) aged out after 7 days and are no longer listed. Nothing was deleted.
```

The numbers under the first block are what `/cancel <n>` takes.

The third block only reaches back a week. A posting pinged a month ago and never replied to is
not a decision still outstanding — it is one already made by silence, and listing it forever
turned the one section that should be short into the longest: it had reached 149 entries, which
is the same as not having the list. The count of what aged out is still reported, because the
difference between "you are on top of it" and "you stopped reading a month ago" is worth one
line.

Nothing is written or deleted when a ping ages out. It is a display filter, not a sweep — an
"expired" decision row would record a choice you never made, and `rehydrate --rescore-before`
deliberately leaves anything already decided alone, so writing one would put every aged-out
posting permanently out of reach of a rescore after a profile change. That is exactly when you
would want it back.

### /build

For a posting the watcher already has on record, this is identical to replying `yes` — the
decision is recorded, the approval is filed in Targeted, and the build is queued.

```
/build https://…                                 a posting it already knows
/build https://… | Acme | Data Scientist         one it has never seen
/build https://… | Acme | Data Scientist | add German
```

The employer and role are **required** for an unknown URL and are never guessed. Both the
duplicate check and the folder lookup search by company and title, so a wrong guess produces a
build that renders into one folder and is then reported as "no dated folder appeared" — a run
that worked, reported as a failure, with the documents left somewhere nothing will find them.

### /cancel

With a build running, it kills the CLI and everything it started — including any `latexmk`
children — and erases the half-application, the same rule the rest of this file follows. With
nothing running it drops the next queued job instead; the queue has no visible handles, so "the
next one" is the only thing you can name without one, and it is the one just queued by mistake.
`/cancel <n>` closes question `n` from `/pending` and cleans up after it.

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

Then the same funnel again, split by source:

```
Per source:
source             fetch   seen   filt    new
ats:Broken         failed — HTTPError: 503
portal:stepstone     277     90    185      2
ats:Delivery Hero   1062      0   1062      0
portal:hiringcafe    931    180    751      0
```

Those totals cannot answer the question anyone actually asks of them, which is never "how many
listings were dropped" but "why is nothing coming from *that* board". One number covering
eighteen sources hides the difference between a board returning its whole catalogue and having
all of it filtered, and a board returning nothing at all: both read `0 new`. A source that
contributed nothing still gets a row, because the empty ones are the interesting ones, and a
source that could not be fetched says so rather than reporting a truthful but misleading four
zeros. Failures sort first, then whatever is producing, then by volume.

The list is capped, with a `+N more` line for the tail, and the block is the first thing dropped
if the report would otherwise exceed Telegram's message limit — an over-length message is
rejected outright rather than trimmed, and the funnel is worth less than the rest of the report.
`watcherctl.py status` prints the same table uncapped. Cycles recorded before this existed have
no per-source data and show no table, rather than an empty one that would read as "no sources
configured".

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

### /rescore

The one command here that does change a verdict, for the one case the cuts cannot reach.

When scoring fails, the posting is held back and tried again on later cycles — but only
`[match] max_score_attempts` times. After that the fallback 45/`maybe` is written as if it were
a real judgement, because a posting that has failed three times is usually an unreadable
posting rather than bad weather. When it *was* the weather, that write is unrecoverable by any
other route: `store.unscored()` only selects postings with no verdict row, so the posting is
never re-judged, and 45 sits below the digest cut so it is never mentioned either. An API
outage on 5 August parked 25 Data and AI engineering roles that way in about twenty seconds.
Twelve of them scored `strong` — up to 82 — the moment they were re-judged.

`/rescore` drops those verdicts and their attempt counters, which puts the postings in front of
the next cycle with a clean budget. Only verdicts carrying the scorer's own "could not judge
this" marker are touched, so a genuine 45/`maybe` survives. The scoring itself is left to that
cycle: 25 postings take over ten minutes, which is not something to hold a chat connection open
through.

`/status` counts these separately as **unjudged**, and says so — a bare number next to
"retrying" read as a queue that was still moving. `retrying` now counts only postings that
really will be tried again (an attempt row and no verdict yet); a posting that ran out of
attempts is unjudged, not retrying. The two counts previously described the same 25 postings in
contradictory terms.

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
   `logs/builds/guard.log`. `--self-test` runs the fixture table, which CI runs on every push;
   the drive-letter containment cases report themselves as skipped there, because a drive letter
   is only an absolute path on the platform that has drives.

   One rule is not about confinement at all: a double-quoted path containing `\$` is blocked.
   Inside double quotes bash reads that as an escaped dollar, so `"...\2026\Company\${TODAY} -
   Role"` loses the separator *and* keeps `${TODAY}` literal. `mkdir -p` then succeeds on a name
   nobody meant and the run writes a whole application into it — which happened, and was noticed
   only because that archiver echoed the path afterwards. Folders are scaffolded with
   `scripts/scaffold.py`, which prints the absolute path; there is no reason to build one in the
   shell.

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

### Live progress

A build takes 25–45 minutes. The `🛠 Building …` message in `processing_build` is edited in
place for that whole time, so there is one message per build that shows where the run is and
what each step cost:

```
🛠 Building · suena GmbH — Senior Data Scientist
Running · 12m 40s · 8/13 steps

  ✅ Archive posting   4m 15s
  ✅ Match brief       2m 47s
  ➖ Company research
  ⏳ CV draft          1m 12s
  ⏳ Cover letter      0m 58s
  ⬜ Verify CV
  …
```

Nothing in the pipeline reports this. The whole checklist is reconstructed from the build's own
NDJSON stdout, which `_spawn` already decodes: an orchestrator step is a `tool_use` block whose
`parent_tool_use_id` is null — the filter that matters, since one real log carried 5 of those
against 71 nested calls from inside subagents. Agent steps are keyed on `subagent_type`, the
deterministic ones on the script name in the Bash command, with `--require-prep` and
`--only interview_prep_payload_en` telling the two render/QA passes apart.

Two traps are worth knowing, because both showed up as `0m 00s` rows before they were handled.
A backgrounded agent's `tool_result` returns in milliseconds saying only "Async agent launched";
the real completion arrives later as a `task_notification` carrying the same `tool_use_id`, timed
by the `duration_ms` the run reports for itself. And that notification has no timestamp of its
own, so a step that *does* get a stamped result later has its finish time corrected rather than
left on the stale stream clock.

A fifth mark, `➖`, is for a step the run went past without this ever seeing it — the flow has
reached a later phase and that row is not coming. It is not an error and it is not rare: an
orchestrator may do a short step itself instead of dispatching the agent watched for here, and a
resumed run did its early steps in the session before this one. Left blank those rows are
indistinguishable from "still to come", which is how a build four steps in came to show three
empty rows under a header reading `0/13 steps`. `STEPS` carries a phase number per row for this,
and only a *later* phase writes an earlier one off — much of the pipeline is deliberately
parallel, so position in the list proves nothing. The numbers are coarse on purpose: guessing
high costs a row briefly marked `➖` just before it starts, which the next event undoes.

A resumed build gets one more thing. Answering a stop-and-ask continues the same CLI session, but
the tracker is new, so `Job.resume_log` carries the asking build's log and the checklist is primed
from it before the first live event — the first half's steps come back with the durations they
actually took, rather than as a blank third of a message. Anything that session left running is
marked `➖` rather than carried, since its clock would otherwise run against this build's.

The message never blocks the build. The stdout pump only flips a dirty flag; a separate task owns
every edit, and a progress failure is logged and swallowed. On a retry the checklist resets and
the header gains `attempt 2/2` — same message, not a second one. When the run ends the message
stops updating and stands as that build's timing record.

`progress_refresh_seconds` is only how often the *running* step's clock is redrawn; a step
starting or finishing redraws immediately. `progress_min_interval_seconds` collapses the parallel
phases, where three or four steps land within a second of each other.
`progress_updates = false` sends the opener once and leaves it alone, exactly as before.

To see the checklist for a build that already ran:

```
python -m watcher.progress --replay "logs/builds/20260805-135256-suena-*.log"
```

### Duplicates

Three layers. A posting fingerprint (never re-notify), a cross-source collapse (the same role
on hiring.cafe and the company's own board is one posting), and at build time a scan of
`<YYYY>/<Company>/` plus the tracker workbook — which is what catches roles you applied to
by hand, before this existed.

A folder alone is not proof: `2026/Synergeticon/2026-07-13 - AI Engineer` holds no PDFs at all.
A folder hit blocks a rebuild only when both `<file_prefix> - CV.pdf` and
`<file_prefix> - Cover Letter.pdf` (`<file_prefix>` from `identity.toml`) are present; otherwise the build proceeds and the
message says it is rebuilding over an incomplete attempt.

The tracker's `Application Folder` column holds a path relative to the workspace root, and it is
resolved against the *current* root when read. That matters because the check asks the filesystem:
a row still naming the drive the workspace used to live on looks like a folder with no PDFs, so a
finished application stops blocking a rebuild. The same rule covers `builds.folder`,
`builds.log_path` and `questions.folder` in `state/watch.db` — see `scripts/workspace_paths.py`,
and `scripts/normalize_stored_paths.py` to repair a store written before it (stop the watcher
first; the script refuses the database half while it is up).

**A decline is not final.** It lands in **Targeted**, next to the approval it answers, and ends
with an override:

```
⚠️ Duplicate · Roche — Machine Learning Engineer
Already applied — Roche — Data Scientist Machine Learning Engineer · applied 2026-06-24
  · 2026/Roche/2026-06-24 - Data Scientist Machine Learning Engineer
Nothing was built. Reply "yes anyway" to build it regardless.
```

It used to go to **Processing** with no override and no log line, so a build that never started
produced nothing anywhere and read as the approval having been dropped on the floor. It is now
an open question: it appears in `/pending`, it is logged, and `yes anyway` builds it.

Declining it — a bare `no` — closes the question and touches nothing else. The matched folder
holds a *finished* application, not this run's debris, so no cleanup runs against it. That is
also why the message names the folder in its text rather than the question carrying it.

**Titles.** Extra *rank* words are free (`Senior Data Scientist` matches `Data Scientist`), extra
*subject* words are not: `Machine Learning Engineer` against `Data Scientist Machine Learning
Engineer` scores 0.67 and builds. It used to score 1.00 — the subject halves were compared by
containment, which forgave every extra word on the longer side, so `Data Scientist` came free.
The fix loosens dedupe slightly, which is the right direction: a false positive costs a missed
application silently, a false negative costs one wasted build you can see.

### Retry on a transient upstream failure

**Currently off — `[build] retries = 0`, so every build gets one attempt.** The rest of this
section describes what setting it back to `1` does.

An API or gateway outage can land on the *last* turn of a run that has already spent
25 minutes producing a match brief, research note, CV, cover letter, both verifications,
and interview prep — and with cleanup wired in, that erases all of it. So a build that
dies on an upstream failure gets `[build] retries` more attempts after
`retry_delay_seconds`, and cleanup only fires once the last one is spent.

Only upstream symptoms qualify: `429`/`5xx`, "overloaded", "temporarily unavailable",
"bad gateway", a reset or refused connection, `fetch failed`. Not a timeout, not a crash,
not a build that wrote the wrong files, and not a 401 — those fail the same way twice and
the second 45 minutes buys nothing.

What made `0` reasonable is the first of the two bounds below. The expensive case the retry was
bought for — an outage on the last turn — is now settled by salvage instead: the documents are
already on disk, the failure is not retried, and the run is reported complete. What is left for
the retry to catch is a drop *early* in a run, where the second half-hour usually meets the same
gateway still unwell. Put it back to `1` if failed builds start clustering on 503s.

Two things bound the retry, both of them learned from one run.

**A finished application is never rebuilt.** `CLAUDE.md` renders the CV and cover letter as
soon as they pass QA and treats interview prep as optional after that, so an upstream drop
during prep lands on a run whose documents are already on disk and already announced. Before
retrying, the builder asks the disk: if both required PDFs are there, the failure is returned
unretried and settled as a salvage. Without that check one drop cost twenty-six minutes
rebuilding two documents from step 00 that had been announced as ready six minutes earlier.

**A partial folder is erased before the next attempt.** The previous design left it for the
retry to scaffold over, which holds only while both attempts name the folder identically — and
the archiver names it from the role title. One retry wrote `AWS AI & Data Engineer` and the
next `AWS AI Data Engineer`, leaving two folders for one application, the abandoned one reading
as an application already sent. Re-capturing the posting costs a minute; a phantom application
costs a real one.

Each attempt writes its own log (`…-retry1.log`), so the transcript of the failure that
caused the retry survives. Telegram says what happened rather than going quiet:

```
🔁 Retrying · Deluxe — AI Engineer
API Error: 503 All accounts are temporarily unavailable
Attempt 2 of 2, in 120s. The partial folder was removed first.
```

### When the pipeline stops to ask

`CLAUDE.md` gives the pipeline three reasons to stop mid-run rather than invent an answer: a
claim `rules/00-canonical-profile.md` does not support, a posting that cannot be captured with
no text pasted, and a company name too ambiguous to name a folder after. Headlessly there is
nobody to answer, so it stops and says why.

That is a question, and the watcher used to lose it. A run that stopped because the posting was
built around an agent-framework stack the profile does not carry was reported as `incomplete:
missing CV.pdf, Cover Letter.pdf`, filed under Failed, and its folder deleted — the three
options it had laid out went no further than the log.

A build that exits cleanly without producing documents is now filed as `needs_decision` and
answered into the **targeted** topic, where the approval came from, with what the run actually
said:

```
⏸ Stopped to ask · RWE — AI Data Engineer

The Match Brief came back with a real integrity flag — this is exactly the "stop and ask"
case CLAUDE.md calls out. …

Nothing was drafted yet, and the folder is being kept while this is open. Answer in this
topic — as a reply or a plain message — and the run picks up where it stopped.
Say "no" to drop it.
log: 20260805-184432-rwe-ag-ai-data-engineer.log
```

The quoted text is the last few turn-ending messages of the run, capped. It is more than the
final one on purpose: the orchestrator ends a turn every time it hands off to a background
subagent, and the run above asked its question a turn before the end and signed off with
"still holding on your decision from above" — useless alone, since the message it points at is
the one that was lost.

**The folder survives while the question is open**, which is the one place this file's otherwise
absolute cleanup rule bends. A paused run is not an abandoned one, and the archived posting,
Match Brief and Research Note are exactly what make resuming cheap instead of a second full
build. It is safe because a folder without both PDFs never blocks a rebuild anyway — see
[Duplicates](#duplicates). The folder is erased when you decline, when you `/cancel` it, or when
the question expires unanswered after two days.

Answering resumes the same CLI session, so the run keeps the posting, the brief and the folder
in context — see [Answering a question the watcher asked](#answering-a-question-the-watcher-asked).
If that session is gone by the time you answer, it falls back once to a fresh build carrying your
answer as the note, and says so.

No attempt is made to tell a stop-and-ask from a run that quietly produced nothing. Both end
cleanly with no application, and quoting what the run said beats "the build reported success but
no dated folder appeared" either way.

### Cleanup after a bad build

A build that fails, crashes, times out, is cancelled, or finishes without a full set of documents
is erased rather than left half-done — after its retries, if it had any. The builder runs `scripts/cleanup_application.py` on
whatever folder appeared, which removes the dated folder, the company folder if that
leaves it empty, the matching tracker row, and that application's `_tmp` scratch. The
tracker workbook itself is never deleted — only the one row whose company + position +
date match, keyed exactly as `append_tracker_entry.py` wrote it.

**One exception: a run that stopped to ask.** Its folder is kept while the question is open,
because the run is paused rather than abandoned and you may well be about to continue it. The
cleanup still happens — just later, at whichever of these comes first: you decline the question,
you `/cancel <n>` it, or two days pass with no answer. Everything else is erased immediately, as
before.

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

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q                        # offline, ~1 min
.\.venv\Scripts\python.exe -m pytest tests --cov=watcher             # with coverage
```

The default run is offline and deterministic: no network, no browser, no clock. CI runs
exactly that line plus `--cov-fail-under=90` on every push and pull request. The suite sits
well above the floor; the gate is there so a module added without tests is caught in a PR
rather than the first time it runs a build unsupervised at 3am.

### Checking the boards for real

Every payload mapping is pinned offline against recorded fixtures, which means the offline
suite stays green forever after a provider changes its API. `-m live` is what notices:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -m live                       # every source family
.\.venv\Scripts\python.exe -m pytest tests -m "live and not live_browser"  # HTTP only
```

Each of the six ATS providers is probed against **several public boards and passes if any one
works** — a single pinned tenant makes the check hostage to that one company switching ATS,
and the question worth asking is whether the *provider's* shape still matches the fetcher.
When one does fail, the message says to swap a token in
`tests/test_live_endpoints.py` before touching the fetcher.

`live_browser` is the second tier — hiring.cafe and StepStone, which need the shared
Playwright context and sit behind bot management. Run these locally when a portal parks
itself; that answers "did the endpoint move again?" directly, which is the first question
worth asking.

`.github/workflows/live.yml` runs both once a day, never on a pull request. The HTTP tier
opens (or comments on) one `live-sources` issue when it fails. The browser tier is run for
the log and its verdict ignored: from a GitHub runner's datacenter IP a refusal usually means
bot management rather than a broken source, and believing it would mean an issue a day.

## Autostart

A Task Scheduler "at logon" task running `py watcherctl.py start` (start-in set to this folder),
or the same line as a shortcut in `shell:startup`. Going through `watcherctl` rather than
`pythonw run_watcher.py` directly means a logon while the watcher is already running is a no-op
instead of a second instance fighting the first for the bot token.

The daily heartbeat exists so that silence is distinguishable from a crash — if it stops
arriving, the watcher is down.
