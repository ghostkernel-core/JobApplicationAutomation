# Job Application Automation

A personal, self-hosted job-application system built on [Claude Code](https://claude.com/claude-code).
An always-on watcher polls company job boards and job portals, scores each new posting against
your own profile, and messages you on Telegram; if you reply `yes`, a headless pipeline of
specialist agents researches the company, drafts and verifies a tailored CV and cover letter,
renders them through locked LaTeX templates, compiles the PDFs, proofreads them, and logs the
application in a spreadsheet.

Everything personal — your profile, your contact details, your applications, your tokens — stays
on your machine; the repository holds the machinery only. The deeper operator manual is
[README - How to use this system.md](./README%20-%20How%20to%20use%20this%20system.md); the
pipeline definition is [CLAUDE.md](./CLAUDE.md), which *is* the runtime instruction set rather
than documentation of one.

## How it works

```
poll sources → dedupe → free prefilter → score → Telegram ping
                                            ⟵ you reply "yes"
              dedupe vs. folders + tracker → claude -p → "Built · <path>"
```

The watcher (`automation/`) fetches from two tiers: `[[ats]]` entries, which read a company's
own job board through a public JSON endpoint (Greenhouse, Ashby, Lever, Workday), and
`[[portal]]` entries, which read aggregators (arbeitsagentur, hiring.cafe, StepStone). Postings
survive a free title/location/seniority prefilter, then get scored by a small model against a
cached digest of your profile. High scores ping immediately; middling ones land in an evening
digest.

Replying to a ping spawns `claude -p` in this workspace with nothing on stdin but the URL and
your note (e.g. `yes, add German`). The same run happens if you paste a job URL into an
interactive Claude Code session here — there is one trigger and one instruction set.

The pipeline itself is 11 steps with an explicit dependency table, defined in full in
[CLAUDE.md](./CLAUDE.md). Grouped, it does this:

1. Capture and archive the posting; create `<YYYY>/<Company>/<YYYY-MM-DD> - <Role>/`.
2. In parallel: match the posting's requirements against your canonical profile with an honest
   gap analysis, and research the company, role context, and hiring contact.
3. Draft structured CV and cover-letter payloads (JSON, not prose files), then verify each for
   factual integrity, relevance, voice, and layout risk.
4. Optionally translate to a German Lebenslauf + Anschreiben (opt-in per run); write private
   interview prep.
5. Render the payloads into locked LaTeX templates, compile PDFs, and run a deterministic
   healthcheck (`render_latex_application.py`, `latex_healthcheck.py`).
6. Proofread the compiled PDFs, then run whole-folder final QA.
7. Append one row to `<YYYY>/Job Applications Tracker <YYYY>.xlsx`.

The division of labour is deliberate: models write and verify *structured payloads*; ordinary
Python renders LaTeX and builds PDFs. No model is ever asked to debug a compiler.

## Requirements

- **Windows or POSIX**, with **Python 3.11+** (the config loaders use `tomllib`), plus
  `openpyxl` for the tracker workbook.
- **Claude Code CLI** on PATH — the pipeline and the headless builds both invoke `claude`.
- **A LaTeX toolchain**: `xelatex` or `latexmk` on PATH, or `LATEX_ENGINE` pointing at the
  binary. MiKTeX and TeX Live both work; MiKTeX's `latexmk` may want Perl, and `xelatex` alone
  is sufficient. Verify with `python scripts/check_latex_toolchain.py`.
- **Playwright + chromium**, only for the browser-based portal fetchers. It is a soft
  dependency: without it the ATS tier and arbeitsagentur run normally and the two fragile
  portals disable themselves.
- **A Telegram bot**, created via [BotFather](https://t.me/BotFather), for notifications and
  approvals.

## Setup

A fresh clone contains no person. The two files that would carry one — `identity.toml` (the
printed contact block) and `rules/00-canonical-profile.md` (the single fact source) — are
git-ignored and created locally from committed `.example` stubs.

```bash
git clone <repo>
cd <repo>
python scripts/init_workspace.py
```

This generates the sandbox settings, copies each `.example` into its real git-ignored
counterpart, creates the runtime directories, and prints the manual checklist.

Next, open Claude Code in the workspace and run the **`12-workspace-init`** agent. It interviews
you and reads your existing CV (you give it the path), then drafts — one file at a time, each
presented for your line-by-line confirmation before it moves on:

- `identity.toml`
- `rules/00-canonical-profile.md`, including the explicit **"may not be claimed"** list, which it
  asks about rather than guessing
- `automation/sources.toml`, with `title_allow` set for *your* field
- `rules/slices/_facts.md`, compressed from the profile it just drafted

The agent is forbidden from inventing anything. Unknown values are left as literal
`FILL IN — <what is missing>` markers and reported back to you.

Then the manual steps it cannot do for you:

1. Put `photo.jpg` and `signature.png` into `master/LaTeX/images/`.
2. Create the watcher venv and install dependencies:
   ```bash
   python -m venv automation/.venv
   automation/.venv/Scripts/pip install -r automation/requirements.txt   # Scripts/ → bin/ on POSIX
   automation/.venv/Scripts/playwright install chromium                  # ~150 MB, once
   ```
   The watcher needs its own interpreter — its pins conflict with typical global installs.
3. Put a **new** BotFather token into `automation/.env` as `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`. Never into a TOML file, and never reuse a token across two installs —
   two processes long-polling the same token fight over `getUpdates`.
   (`python -m watcher.whoami` prints the chat id of whoever messages the bot next.)
4. Offset `interval_minutes` in `automation/config.toml` if another install polls the same
   boards, so you are not hammering them in lockstep.

Finally, `python scripts/init_workspace.py --verify` checks identity, the generated sandbox
settings, the guard hook's interpreter path, the facts slice, and the image assets.

## Configuring your own filters and sources

`automation/sources.toml.example` is a **template with everything commented out** — it ships with
empty title lists and example ATS blocks precisely so it carries nobody's field or geography.
Your live `automation/sources.toml` is git-ignored and never leaves the machine.

What to set:

- **`[defaults] title_allow` / `title_deny`** — the free prefilter. `title_allow` is the single
  most load-bearing setting: a posting must match one of its keywords to survive at all. Fill it
  with the roles you would actually apply to, and put the look-alike titles you never want
  (`intern`, `werkstudent`, `head of`, …) into `title_deny`.
- **`[filters.location]`** — geography at whatever grain suits: continents, regions (`EU`,
  `DACH`, `NORDICS`, …), ISO country codes or names, cities. The lists add up rather than
  override. `python -m watcher.geo --expand EU DACH` shows what a name becomes.
- **`[filters.seniority]` / `[filters.experience]`** — bands to allow or deny, and whether the
  years-of-experience bar is merely annotated or actually enforced.
- **`[[ats]]` entries** — one per company you want to watch. The board slug is not guessable, so
  let the resolver find it: `python -m watcher.discover --company "Celonis"` probes each provider
  and prints a ready-to-paste block. Check the sample titles it shows — slugs collide.
- **`[[portal]]` entries** — aggregators, keyed by `name` (which selects the fetcher). `queries`
  is verbatim search text; broad is fine, since `title_allow` is stricter than any portal's own
  filtering.

Every filter is deliberately **one-sided**: it rejects only what it positively resolved. A
location it cannot parse, a title stating no rank, a posting with no experience bar — all pass
through to the matcher, because a filter that drops what it *failed to read* turns each new
board format into silent data loss.

`automation/config.toml` holds the tuning that is not source-specific: poll interval and max
posting age, the notify/digest score thresholds, notification hours, build timeout, and
`[build] enabled` (`false` records approvals without spawning anything). No secrets belong in it.

## Running it

Dry-run the watcher first — it fetches and normalizes, and stores nothing. Then run it for real
(all from `automation/`, with that folder's own interpreter):

```bash
python -m watcher.poll --dry-run
python -m watcher.poll --dry-run --show-filtered   # including what the prefilter dropped
python -m watcher.prefilter --explain              # every rule, fully expanded

python run_watcher.py                              # the always-on process
python run_watcher.py --once                       # one poll + score + notify, then exit
python run_watcher.py --digest                     # send the digest now
```

Only one instance may run at a time. For unattended operation, put a shortcut to
`pythonw run_watcher.py` in `shell:startup`, or use a scheduler task with restart-on-failure.
A daily heartbeat message exists so that silence is distinguishable from a crash.

**The manual path needs none of this.** Open Claude Code in the workspace and paste a job
posting URL (or the posting text), optionally with a note like `apply as Data Scientist` or
`add German`. The same 11-step pipeline runs, per `CLAUDE.md`, and the deliverable folder lands
in `<YYYY>/<Company>/<YYYY-MM-DD> - <Role>/`.

## Privacy model

`.gitignore` is a **top-level whitelist**: everything at the workspace root is ignored, and only
named entries are re-admitted. New folders are therefore private by default — an application
year, a company folder, an export, a scan of your passport, a recruiter's CV never becomes
trackable merely because it was created under a new name.

Committed: `scripts/`, `rules/` (rules and `.example` stubs), `master/`, `automation/` (code,
`config.toml`, `sources.toml.example`), `.claude/agents/`, `.github/`, the READMEs. Never
committed: `identity.toml`, `rules/00-canonical-profile.md`, `rules/slices/_facts*.md`,
`automation/.env`, `automation/sources.toml`, `automation/profile_kb.md`,
`automation/decisions.jsonl`, `automation/state/`, `automation/logs/`, every application folder
and tracker workbook, and the photo/signature assets.

**Headless builds are sandboxed.** They run under `--permission-mode bypassPermissions` (an
unattended 11-step run is otherwise impossible), narrowed by two layers: a generated
`automation/build_settings.json` deny-list, and `automation/hooks/guard.py`, a blocking
`PreToolUse` hook that rejects destructive commands, credential and system paths, and any
*write* outside the workspace. Every decision is logged. This is defence against a confused
agent, not a hostile one — neither layer reaches inside a subprocess. What actually bounds the
blast radius is the pinned working directory, the full logs, and the fact that a build only ever
starts from an explicit reply.

**If you fork or clone this**, set a repository secret named `PII_PATTERNS` to a
newline-separated list of regexes matching your own name, address, and phone number. CI
(`.github/workflows/checks.yml`) runs gitleaks, structural checks on the tracked file set, and
an optional scan using those patterns, so an accidental commit of personal data fails the build
before it reaches a public branch. The patterns live in the secret rather than in the repo, so
they are never public themselves — and the scan reports only *that* a file matched, never the
matched content.

## Fonts

The documents are typeset in Helvetica Neue, which is proprietary and **not in this repository**.
`master/LaTeX/shared/common.tex` performs a file-existence check: if
`master/LaTeX/fonts/HelveticaNeue-regular.ttf` is present it is used, otherwise the bundled,
openly licensed **Liberation Sans** is loaded automatically. A fresh clone compiles correctly
with no font work at all.

To use your own font, drop the `.ttf` files into `master/LaTeX/fonts/` and adjust the
`\setmainfont` lines in `common.tex`. Files you add there stay local unless you commit them; the
Helvetica Neue filenames are explicitly git-ignored.

One warning if you swap fonts: **re-verify that punctuation survives PDF text extraction.** The
proofreading and healthcheck steps read the compiled PDF back as text, and some fonts drop or
mangle apostrophes and semicolons in extraction, silently breaking those checks. Compile one
document, run `pdftotext` over it, and confirm before committing to the change.

## Repository layout

```
CLAUDE.md                     the pipeline definition — the runtime instruction set
README - How to use this...   the operator manual
identity.toml.example         contact-block stub; the real file is local-only

scripts/                      deterministic tooling, no model tokens:
                              init_workspace.py (scaffold + --verify),
                              render_latex_application.py (payload → .tex → PDF),
                              latex_healthcheck.py, check_latex_toolchain.py,
                              append_tracker_entry.py, save_singlefile.sh
rules/                        writing style, CV/letter rules, German-market
                              conventions, integrity, naming, anti-fingerprint
  00-canonical-profile.example.md   the fact-source template
  slices/                     compact per-agent rule context
master/LaTeX/
  templates/                  locked layouts: cv_en, cv_de, letter_en,
                              letter_de, interview_prep_en
  shared/common.tex           shared macros, layout, font selection
  fonts/  images/             assets copied into temporary build folders
automation/                   the watcher — self-contained (own venv, config,
  watcher/  watcher/fetchers/ sqlite state, logs), one module per board
  hooks/guard.py              PreToolUse sandbox hook for headless builds
  sources.toml.example        boards and filters template
  config.toml                 intervals, thresholds, build tuning
.claude/agents/               the specialist subagents, 00–12
```

## Honesty

The pipeline does not fabricate. Every claim in a generated CV or cover letter must trace to
`rules/00-canonical-profile.md`, which is the only fact source in the system — not the posting,
not the web, not what would be plausible for someone with your background. The profile carries
an explicit **"may not be claimed"** list (certifications you do not hold, degrees started but
not finished, tools you have only touched privately, seniority you have not held, work
authorization you cannot support), and dedicated verifier agents check every payload against it
before anything is rendered. When a posting demands a claim the profile cannot support, the run
stops and asks rather than writing it.
