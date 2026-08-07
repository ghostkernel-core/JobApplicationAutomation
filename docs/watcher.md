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

**`[defaults] title_deny`** is the free prefilter. Put look-alike titles you never want
(`intern`, `werkstudent`, `head of`, …) into it. There is deliberately no `title_allow` any
more: a hand-written substring list decided what was ever *seen*, ahead of the profile that
decides what is worth applying to, and it was the narrowest gate in the pipeline —
`ats:Roche` fetched 200 postings a cycle and stored none, ten of fifteen company boards had
stored nothing ever, and a title like "Specialist Advanced Analytics" had no way through no
matter how well the job fit. A substring list is a good way to say *never* and a poor way to
say *only*, which is why only the deny half survived. Titles are now judged against your
profile by `[triage]`, which reads the same digest the matcher scores with and fails open.
A leftover `title_allow` key is ignored rather than rejected — delete it when you next edit
the file.

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

`queries` is verbatim search text per portal, and broad is fine — nothing narrows a title
before it is fetched, and relevance is judged against your profile by `[triage]` once the
posting is in hand. Broad is not enough on its own, though: those few terms are the entire
aperture of this half of the watcher, so a role you are qualified for that nobody thought to
type is never fetched, and nothing reports its absence. `[queries]` in `config.toml` closes
that by writing the terms from your profile instead — see
[Generated search terms](#generated-search-terms) below. The two fragile portals
share one persistent Playwright browser context, so a Cloudflare challenge solved once is reused
rather than re-triggered every poll. Three consecutive failures disable a source and send
exactly one notification — no retry loop; `python -m watcher.health --reset portal:stepstone`
re-enables it.

### Generated search terms

`[queries]` in `config.toml` writes each portal's search terms from your profile digest
instead of from `sources.toml`. One model call per profile edit, cached against the digest
that produced it, so a poll cycle never pays for it.

A bad generation narrows nothing. A generated set replaces a portal's hand-written list only
when enough of it survives validation, and below two valid queries that portal keeps
`sources.toml` verbatim. The fallback is per portal, because `arbeitsagentur` is asked for
German and the other two for English and those halves fail independently.

Validation is strict, because both failure modes are silent:

- **No location, country, region, or work-arrangement word.** Every portal takes location as
  its own field, so a city inside the query text double-filters and returns an empty page
  that reads exactly like a quiet day.
- **No boolean operators, quotes, brackets or wildcards.** A malformed query can 4xx a
  fragile portal, and a structural failure *parks* the source with no automatic retry — one
  unbalanced quote can take StepStone off the board until somebody reads a status page.
- **Two to six words, at most 60 characters.** One word matches half the board; a sentence
  is treated as an implicit AND of all its words and matches nothing.

| command | what it does |
|---|---|
| `python -m watcher.queries` | the terms each portal is polled with now, and whether they came from `sources.toml` or your profile |
| `python -m watcher.queries --regenerate [--dry-run]` | write a fresh set now, without waiting for the digest to change |
| `python -m watcher.queries --check "Data Scientist Berlin"` | run one query through the validator and say which rule it breaks |
| `python -m watcher.queries --explain` | the config, the cache key, and the prompt a real call would send — no model call |

It is off by default and is the last switch to flip: it changes what is *fetched*, so it moves
every downstream number at once. Turn it on alone, after `[triage]` and `[recall]` have had a
week to settle, or there is no way to tell which change moved what.

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

Three sections govern discovery — what is fetched, what survives, and how much of what was
thrown away should have been kept.

**`[triage]`** is the profile-derived gate that replaced `title_allow`. It judges title,
company and location only, in batches, before a posting is ever hydrated or stored, and it
**fails open**: anything it cannot judge — a degraded batch, a malformed reply, a verdict that
is not the exact string `drop` — is kept. Only a deliberate `drop` drops. `--replay 30`
re-judges the last 30 stored postings against their stored scores and writes nothing;
`--explain` prints the prompt without a model call; `--backfill` works through more than one
cycle's worth at once.

Failing open has a blind spot: triage that fails *every* batch keeps everything, which is
indistinguishable from triage that is working. `--replay` is the check, and it exits non-zero
when it could not certify the run — including when the batch degraded and therefore judged
nothing. Keep `batch_size` where a batch finishes well inside `timeout_seconds`; the cost of
a batch is superlinear in its item count, so a bigger one is both slower per posting and more
likely to time out.

**`[recall]`** is the weekly answer to "what did all of that throw away?". Every rejected
posting is recorded with the stage that rejected it, and once a week a stratified sample is
re-scored through the same matcher a stored posting goes through. It decides nothing — no
verdicts, no postings, no config changes; the only thing it writes is `drops.audited_at`, so
the same row does not consume next week's sample too. The denominator is kept honest: a
posting it could not re-fetch is reported as *no description available*, never as correctly
dropped. Off until the drop table has a week of real data in it, because a miss rate computed
from a nearly empty table reads as a fact and is not one.

**`[queries]`** is covered under [Generated search terms](#generated-search-terms) above.

## Changing settings while it runs

`config.toml` and `sources.toml` are re-read whenever they change on disk. Edit a threshold, an
interval, or a source and the next poll cycle uses the new value — no restart, and no cycle
abandoned halfway through by one. A file that is briefly unparseable (an editor mid-save, a
stray bracket) is logged once and the last good version stays in force until it parses again,
so a typo cannot take down a watcher that has been up for weeks.

`automation/build_settings.json` is re-rendered from its template before every build, so
template edits and a moved clone both apply to the next run. It is a **generated** file: edit
`build_settings.template.json`, not the `.json`, or your change is overwritten. If the deny rules
in it would block writes inside the workspace, the build refuses to start and says which rules —
containment outside the workspace is `hooks/guard.py`'s job, and a broad path deny standing in
for it is only correct until the workspace moves.

Two things still need `python start_watcher.py restart`: **`automation/.env`**, because swapping
the credentials of a live long-poll connection underneath itself is not something to do between
two poll cycles, and any change to the watcher's own **Python code**.

Nothing in `CLAUDE.md`, `rules/`, or `.claude/agents/` needs a restart or is cached anywhere —
each build is a fresh `claude -p` process that reads all of them at spawn time. Adding, editing,
or removing a pipeline agent changes the very next run, headless and interactive alike.

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

Only one instance may run at a time, and in day-to-day use you do not drive `run_watcher.py`
directly — `automation/watcherctl.py` wraps it. It preflights the venv, `.env` token and
`build_settings.json`, starts the watcher detached, refuses to start a second instance, and
handles everything after the start too:

```bash
python start_watcher.py status         # running? since when? how are the sources?
python start_watcher.py start          # or just `python start_watcher.py`
python start_watcher.py stop
python start_watcher.py restart
python start_watcher.py logs -n 50 -f
python start_watcher.py --help         # every sub-command
```

`start_watcher.py` in the repository root and `automation/watcherctl.py` take the same
sub-commands and do the same thing — the root launcher forwards each one unchanged and defaults to
`start` when given none, so you never have to remember which script owns which verb. Run whichever
is closer to where you are; the examples here use the root one because that is where a session
usually starts.

It imports nothing from `watcher` and needs no third-party package, so it runs under any
interpreter on the machine and hands the commands that need real dependencies to
`automation/.venv`. Instances are found by scanning the process table for a `run_watcher`
command line rather than by a pidfile, so it also sees a watcher that was started by hand.

For unattended operation, use a scheduler task at logon running
`py automation/watcherctl.py start` with restart-on-failure, or the same line as a shortcut in
`shell:startup` (Windows). Going through `watcherctl` means a logon while the watcher is already
running is a no-op rather than a second instance fighting the first for the bot token. A daily
heartbeat message exists specifically so silence is distinguishable from a crash.

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
