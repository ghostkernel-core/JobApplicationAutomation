# Job Application Automation

A self-hosted job-application pipeline: a watcher finds the posting, you reply `yes` from your
phone, and a tailored, verified, compiled PDF application lands in a dated folder.

[![checks](https://github.com/ghostkernel-core/JobApplicationAutomation/actions/workflows/checks.yml/badge.svg)](https://github.com/ghostkernel-core/JobApplicationAutomation/actions/workflows/checks.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Built for Claude Code](https://img.shields.io/badge/runtime-Claude%20Code-6f42c1.svg)](https://claude.com/claude-code)

---

A new posting appears on a company's own job board at 09:14. Your phone buzzes:

```
🟢 82 · Machine Learning Engineer (NLP)
Acme Robotics — Munich, DE · remote · greenhouse
· senior · asks 5+ yrs
✓ production NLP; German market; PyTorch depth
⚠ no Kubernetes ownership

https://boards.greenhouse.io/acmerobotics/jobs/4211

Reply: yes / no / later — extras like "yes, add German" pass through.
```

You reply `yes, add German` and put your phone down. The next message is the finished build:

```
✅ Built · Acme Robotics — Machine Learning Engineer (NLP)
2026/Acme Robotics/2026-08-03 - Machine Learning Engineer
CV · Cover Letter · Lebenslauf · Anschreiben · Interview Prep · tracker row ✓
```

Five PDFs, typeset from locked LaTeX templates, every factual claim traced back to your profile,
proofread, and logged as a row in this year's tracker workbook. You never opened a browser.

## What it does

- **Watches** company job boards (Greenhouse, Ashby, Lever, Workday) and aggregators
  (Arbeitsagentur, hiring.cafe, StepStone) on an interval you set.
- **Filters and scores** each new posting — a free keyword/location/seniority prefilter first,
  then a small model against a cached digest of your profile. Strong matches ping immediately;
  middling ones collect into a digest.
- **Builds** on your reply: archives the posting, researches the company and hiring contact,
  drafts and verifies a CV and cover letter, optionally a German *Lebenslauf* + *Anschreiben*,
  renders LaTeX, compiles PDFs, proofreads them, runs whole-folder QA, and appends one tracker row.
- **Writes private interview prep** for the same role, as its own PDF.

There is no web UI and no hosted service. Everything personal — your profile, your applications,
your tokens — stays on your machine. The repository holds the machinery only.

The same run happens if you skip the watcher entirely and paste a job URL into an interactive
[Claude Code](https://claude.com/claude-code) session in the workspace. One trigger, one
instruction set.

## The architecture worth stealing

**Models write and verify structured JSON payloads. Ordinary deterministic Python renders the
LaTeX and compiles the PDFs.** No model is ever asked to debug a compiler, chase a `Missing
$ inserted`, or fight a page break. Layout is a locked template plus a `.py` file; content is a
payload that either validates or doesn't.

Three more decisions follow from that:

- **Every generative step has a dedicated adversarial verifier.** The CV writer is one agent; the
  CV verifier is a different agent with a different prompt, checking integrity, relevance, voice,
  and layout risk before anything renders. Same for the cover letter.
- **Failures are caught deterministically first.** `latex_healthcheck.py` runs over the compiled
  PDFs before any model reads them, so the proofreader spends its tokens on language, not on
  defects a script can find for free.
- **Model tiers are explicit configuration.** Each subagent pins its own model: Opus for the two
  steps where prose quality actually originates, Sonnet for matching, research, verification,
  translation and QA, Haiku for capture and extraction. The orchestrator runs on Sonnet on
  purpose — it carries the whole run's context and only does glue work.

One detail people either love or find alarming: [`CLAUDE.md`](./CLAUDE.md) **is** the runtime
instruction set, not documentation of one. The 11-step dependency table in that file is what
executes. There is no separate headless prompt. Editing the table edits the pipeline.

Full walkthrough: [docs/the-pipeline.md](./docs/the-pipeline.md).

## Quickstart

You need Python 3.11+, the Claude Code CLI on PATH, and a LaTeX toolchain (`xelatex` or
`latexmk`). A Telegram bot and Playwright are only needed for the watcher.

```bash
git clone https://github.com/ghostkernel-core/JobApplicationAutomation.git
cd JobApplicationAutomation
python scripts/init_workspace.py     # scaffolding + local-only personal files
```

Then open Claude Code in the workspace and run the **`12-workspace-init`** agent. It interviews
you, reads your existing CV, and drafts your identity, canonical profile, watcher sources, and
facts slice — one file at a time, each shown for your line-by-line approval. It is forbidden from
inventing anything; unknowns come back as literal `FILL IN — <what is missing>` markers.

```bash
python scripts/init_workspace.py --verify
```

That is the manual path working. Paste a job URL into the session and you get an application
folder. Setting up the watcher, the Telegram bot, and the venv is another ten minutes:
[docs/getting-started.md](./docs/getting-started.md).

Once it is set up, the watcher is driven entirely from one script in the repository root:

```bash
python start_watcher.py                 # start it detached (no-op if already running)
python start_watcher.py status          # running? since when? how are the sources?
python start_watcher.py restart
python start_watcher.py stop
python start_watcher.py logs -n 50 -f   # tail the log, -f to follow
python start_watcher.py --help          # poll, health, reset, digest, ...
```

Each sub-command is forwarded verbatim to `automation/watcherctl.py`, which is the same control
surface from inside that folder — one implementation, two spellings.

## Why it won't embarrass you

**It refuses to lie.** Every claim in a generated CV or cover letter must trace to
`rules/00-canonical-profile.md`, the single fact source — not the posting, not the web, not what
would be plausible for someone with your background. That profile carries an explicit **"may not
be claimed"** list: certifications you don't hold, degrees started but not finished, tools you've
only touched privately, seniority you haven't had, work authorization you can't support. Verifier
agents check every payload against it before rendering. When a posting demands a claim the profile
can't support, the run **stops and asks** rather than writing it.

**Privacy is structural, not remembered.** `.gitignore` is a top-level whitelist: everything at
the workspace root is ignored and only named entries are re-admitted, so a new folder — an
application year, an export, a scan of your passport — is private by default rather than private
if you thought about it. A fresh clone contains no person at all; the two files that would carry
one are git-ignored and generated locally.

**Headless runs are contained.** They run under `bypassPermissions` (an unattended 11-step run is
otherwise impossible), narrowed by a generated deny-list plus `hooks/guard.py`, a blocking
`PreToolUse` hook that rejects destructive commands, credential paths, and any *write* outside the
workspace. Every decision is logged. This is defence against a confused agent, not a hostile one —
and the honest limits are written up in
[docs/privacy-and-security.md](./docs/privacy-and-security.md), along with the CI scan that checks
for leaked personal data using regexes held in a repository secret, so the patterns themselves
never go public.

## Documentation

| | |
|---|---|
| [Getting started](./docs/getting-started.md) | Full install, the init agent, Telegram, first run |
| [The pipeline](./docs/the-pipeline.md) | All 11 steps, the agents, payloads, LaTeX rendering |
| [The watcher](./docs/watcher.md) | Sources, filters, scoring, replies, containment |
| [Customising](./docs/customising.md) | Your filters, templates, fonts, rules, model tiers |
| [Privacy & security](./docs/privacy-and-security.md) | The whitelist, the sandbox, CI scanning |
| [Troubleshooting](./docs/troubleshooting.md) | LaTeX, Telegram conflicts, fragile portals |

Index: [docs/README.md](./docs/README.md).

## Repository layout

```
CLAUDE.md            the pipeline definition — the runtime instruction set
scripts/             deterministic tooling, no model tokens: init_workspace,
                     render_latex_application (payload → .tex → PDF),
                     latex_healthcheck, check_latex_toolchain, append_tracker_entry,
                     pdf_to_images (rasterise a page for visual inspection)
rules/               writing style, CV/letter rules, German-market conventions,
                     integrity, naming, anti-fingerprint; slices/ = per-agent context
master/LaTeX/        locked templates (cv_en, cv_de, letter_en, letter_de,
                     interview_prep_en), shared macros, fonts, images
automation/          the watcher — self-contained: own venv, config, sqlite state,
                     logs, one fetcher module per board; hooks/guard.py sandbox hook;
                     watcherctl.py is the control surface (start_watcher.py in
                     the root forwards to it)
.claude/agents/      the specialist subagents, 00–12
docs/                the long-form documentation
```

## Licence

MIT — see [LICENSE](./LICENSE). Bundled font licences are listed in
[THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md); the documents typeset in Liberation Sans out
of the box, and a fresh clone compiles with no font work at all.
