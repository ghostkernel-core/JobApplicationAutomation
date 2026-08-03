# The Watcher

`automation/` is an always-on process that polls job boards, scores new postings against your
profile, pings you on Telegram, and — on your reply — runs the *same* pipeline described in
[The Pipeline](./the-pipeline.md) headlessly. Nothing in `scripts/`, `rules/`, `master/`, or
`.claude/agents/` changes for this; the watcher is strictly a new caller of that pipeline.

```
poll sources → dedupe → prefilter (free) → score (haiku) → Telegram
                                              ⟵ you reply "yes"
                          dedupe vs folders + tracker → claude -p → "Built · <path>"
```

The watcher is self-contained: its own virtual environment, config, SQLite state, and logs — see
[Getting Started](./getting-started.md) step 4 for the one-time setup.

## Configuring `automation/sources.toml`

This file is hand-edited and git-ignored — your live copy never leaves your machine. It is
generated from `automation/sources.toml.example`, a template with everything commented out, so
the committed version carries nobody's field or geography.

**`[defaults] title_allow` / `title_deny`** is the free prefilter, and `title_allow` is the
single most load-bearing setting: a posting must match one of its keywords to survive at all.
Fill it with the roles you would actually apply to; put look-alike titles you never want
(`intern`, `werkstudent`, `head of`, …) into `title_deny`.

**`[filters.location]`** expresses geography at whatever grain suits — continents, regions
(`EU`, `DACH`, `NORDICS`, …), ISO country codes or names, or cities — and the lists add up rather
than override:

```toml
[filters.location]
regions           = ["EU", "EFTA"]
cities            = []                 # empty = any city inside the allowed countries
exclude_countries = []
remote_ok         = true               # a remote posting survives a country miss...
remote_anywhere   = false              # ...but only if the employer is still in-region
```

`python -m watcher.geo --expand EU DACH` shows what a region name expands to before you commit
to it; `python -m watcher.geo --resolve "Düsseldorf, Germany"` resolves a single location.

**`[filters.seniority]` / `[filters.experience]`** decide which bands survive and whether the
years-of-experience bar is enforced or merely shown:

```toml
[filters.seniority]
allow = ["junior", "mid", "senior", "lead", "staff", "principal"]
deny  = ["intern", "executive"]

[filters.experience]
mode      = "annotate"   # read the years bar, show it, never filter on it
max_years = 0            # only used when mode = "filter"
```

Seniority is read from the **title only** — the posting body looked like free evidence and is
not; a real archived posting once read as `intern` purely because an unrelated internship tile
sat in its page sidebar. `unknown` seniority can never be denied, because most good titles in
this field state no rank at all.

**`[[ats]]` entries** are one per company job board, read through a public JSON endpoint
(Greenhouse, Ashby, Lever, Workday). The board slug is not guessable, so let the resolver find
it:

```bash
python -m watcher.discover --company "Celonis"
```

This probes every supported provider and prints a ready-to-paste block — check the sample job
titles it shows, since slugs collide (`aleph` on Lever, for instance, is an advertising company,
not a well-known AI lab of a similar name). Workday cannot be probed; it needs three values
pulled out of the careers URL by hand (`host`, `tenant`, `site`).

**`[[portal]]` entries** are aggregators, keyed by `name`, which selects the fetcher — there is
no URL to configure, since each portal reads a different internal shape:

| name | how it reads | fragile | location knob |
|---|---|---|---|
| `arbeitsagentur` | public JSON API, static client key | no | `location` = city, `radius_km` |
| `hiringcafe` | Next.js `_next/data` endpoint, via the shared browser | yes | none — worldwide; geography is filtered by `[filters.location]` |
| `stepstone` | the search page's preloaded state, via the same browser | yes | `location` → `/in-<slug>`; `"Deutschland"` covers the whole country |

`queries` is verbatim search text per portal; broad is fine, since `title_allow` is stricter than
any portal's own relevance ranking (a real run: 209 fetched, 92 kept). The two fragile portals
share one persistent Playwright browser context, so a Cloudflare challenge solved once is reused
rather than re-triggered every poll. Three consecutive failures disable a source and send
exactly one notification — no retry loop; `python -m watcher.health --reset portal:stepstone`
re-enables it.

### The one-sided-filter design principle

Every filter in `sources.toml` is deliberately **one-sided**: it can only reject what it
positively resolved. A location it cannot parse, a title stating no rank, a posting with no
experience bar — all pass through to the scoring step rather than being silently dropped. A
filter that drops what it *failed to read* would turn each new job-board format into invisible
data loss; the design accepts a few irrelevant postings reaching the matcher in exchange for
never quietly losing a real one.

## `automation/config.toml`

Tuning that is not source-specific — poll interval, max posting age, notify/digest score
thresholds, notification hours, the headless build timeout, and `[build] enabled` (`false`
records an approval without spawning anything). No secrets belong here; those go in
`automation/.env` — see [Privacy and Security](./privacy-and-security.md).

The score thresholds matter in particular: `notify_threshold` triggers an instant ping,
`digest_threshold` a line in the evening digest, and anything below that is stored silently
(visible via `watcher.match --replay`). `python -m watcher.match --calibrate` scores postings you
already applied to, to sanity-check wherever you set these.

## Running it

Dry-run first — it fetches and normalizes, and stores nothing:

```bash
python -m watcher.poll --dry-run
python -m watcher.poll --dry-run --show-filtered   # including what the prefilter dropped
python -m watcher.prefilter --explain              # every rule, fully expanded
```

Then run it for real, from `automation/`, with that folder's own interpreter (its pins conflict
with a typical global install):

```bash
python run_watcher.py                              # the always-on process
python run_watcher.py --once                       # one poll + score + notify, then exit
python run_watcher.py --digest                      # send the digest now
```

Only one instance may run at a time. Two launchers at the repository root wrap this:
`python start_watcher.py` preflights the venv, `.env`, and `build_settings.json`, then starts the
watcher detached, guarded by a pidfile so a second instance cannot start by accident;
`python start_watcher.py --status` reports whether it's running without starting anything.

For unattended operation, put a shortcut to `pythonw run_watcher.py` in `shell:startup`
(Windows), or use a scheduler task with restart-on-failure. A daily heartbeat message exists
specifically so silence is distinguishable from a crash.

## Replying on Telegram

Reply *to the notification message* — several can be in flight, so a bare message is ambiguous.

```
yes                       build it
yes, add German           anything after "yes" is passed to the pipeline verbatim
no / no, too much devops  the reason is appended to profile_kb.md
later                     snooze for [notify] snooze_days
build 3                   promote line 3 of a digest
```

A reply that carries a reason is appended to `automation/profile_kb.md` verbatim, under
`## Learned from decisions` — no model, no paraphrase. Once a week (`[kb]` in `config.toml`) those
lines are condensed into a proposed set of `Prefer`/`Avoid` rules; the proposal is sent to
Telegram and nothing is written until you reply `yes` to it. It only ever *adds* bullets, never
touches the hand-calibrated `## Hard filters` or `## How to weigh gaps` sections, and every added
bullet is tagged with the date it was proposed, so undoing one is deleting one line.

## Digest and heartbeat

`digest_hour` and `heartbeat_hour` in `[notify]` (24-hour local time) control two independent
messages: the evening digest, which lists postings that scored above `digest_threshold` but
below `notify_threshold`, and the daily heartbeat, which exists purely so that silence means the
watcher is down rather than that nothing new was found.

## Duplicate protection

Three layers: a posting fingerprint (never re-notify on the same posting), a cross-source
collapse (the same role seen on two different boards is one posting), and — at build time — a
scan of `<YYYY>/<Company>/` plus the tracker workbook, which catches roles you applied to by hand
before the watcher existed. A folder alone is not proof of completion: a rebuild is blocked only
when both the CV and cover-letter PDFs are already present in that folder.
