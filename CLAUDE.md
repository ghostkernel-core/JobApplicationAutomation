# Job Application Automation — Claude Code / Cowork Project Instructions

This folder is the workspace owner's job-application workspace (identity in identity.toml, facts in rules/00-canonical-profile.md) for the German / EU market.

Claude Code / Cowork is the active agent system. It uses native subagents defined in
`.claude/agents/`, one per pipeline step.

## Trigger

When the user pastes a job posting URL or the posting text, with or without notes like "apply as Data Scientist" or "add German", act as the orchestrator and run the full pipeline in one go. German (Lebenslauf + Anschreiben) is opt-in: produce English only unless the user asks for German (e.g. "add German", "German too", "EN+DE").

1. Use `rules/slices/<agent>.md` for per-agent rule context instead of listing individual rule files.
2. Dispatch specialist work through the Agent tool, selecting the matching subagent in `.claude/agents/` (e.g. `01-experience-matcher`, `03-cv-writer-en`).
3. Stop to ask the user only if integrity is blocked, the URL cannot be captured with no text given, or the company name is ambiguous.
4. Fire independent pipeline steps in parallel through the Agent tool whenever dependencies allow. Parallel means **both Agent calls in a single message, as two tool_use blocks** — two messages one after the other is sequential no matter how quickly they follow.

### The same trigger arrives headlessly

`automation/` runs an always-on watcher that polls job boards, scores postings against the
profile, and pings Telegram. When the user replies `yes` (optionally `yes, add German`), the
watcher spawns `claude -p` in this workspace and puts **exactly the URL plus that note** on
stdin — nothing else. There is no separate headless prompt or instruction set: this file is it.

So the pipeline must stay runnable from a bare URL + optional note, with no interactive
follow-up. If a run genuinely hits a stop-and-ask condition, say so and stop — the reply lands
in the Telegram thread. Do not invent an answer to keep going. Details: `automation/README.md`.

## Pipeline (orchestrator reference)

Architecture: models write and verify structured payloads; deterministic scripts render LaTeX and build PDFs.
Browser MCP is not used — use pasted text, or `scripts/capture_posting.py` for a URL.
PDF compilation requires `latexmk` or `xelatex` on PATH, or `LATEX_ENGINE` set. Check with `python scripts/check_latex_toolchain.py`.

| # | Step | Agent/tool | Output | Depends on |
|---|---|---|---|---|
| 00 | Capture + archive posting; create folder | `00-posting-archiver` | folder + `.html` | posting text/URL |
| 01A | Parse posting, requirements map, fit/gap | `01-experience-matcher` | Match Brief | 00 |
| 01B | Research company and HR contact | `02-company-role-researcher` | Research Note | 00 |
| 02A | Draft English CV payload | `03-cv-writer-en` | `cv_payload_en` | 01A, 01B |
| 02B | Draft English cover-letter payload | `05-cover-letter-writer-en` | `letter_payload_en` | 01A, 01B |
| 03A | Verify English CV payload | `04-cv-verifier-en` | PASS/FIXED/REJECTED | 02A |
| 03B | Verify English cover-letter payload | `06-cover-letter-verifier-en` | PASS/FIXED/REJECTED | 02B |
| 04A | German Lebenslauf + Anschreiben payloads (only if German requested) | `07-translator-de` | `cv_payload_de`, `letter_payload_de` | 03A, 03B |
| 05A | **Render + compile the application documents** | `scripts/render_latex_application.py` | CV + letter `.tex` + PDF (×2 if German) | 03A, 03B, 04A |
| 06A | QA the application documents | `scripts/qa_application.py --no-images` | PASS / FAIL | 05A |
| 04B | Interview prep payload | `08-interview-prep` | `interview_prep_payload_en` | 01B, 03A, 03B |
| 05B | Render + compile the interview prep | `scripts/render_latex_application.py` | Interview Prep `.tex` + PDF | 04B |
| 06B | QA the interview prep | `scripts/qa_application.py --require-prep --no-images` | PASS / FAIL | 05B |
| 08 | Proofread final PDFs | `09-proofreader` | PASS/FIXED/ESCALATED | 06A, 06B |
| 09 | Final QA across entire folder | `10-final-verifier` | PASS / REJECTED | 08 |
| 10 | Log the application in the yearly tracker | `scripts/append_tracker_entry.py` | row in `<YYYY>/Job Applications Tracker <YYYY>.xlsx` | 09 |
| 11 | Present files to user | assistant | links + 3-line summary | 10, 05B |

**The application documents render first, and they do not wait for interview prep.** The
moment 03A and 03B pass (plus 04A if German was asked for), call the renderer with
`--only` for exactly the application payloads:

```
python scripts/render_latex_application.py <payload>.json "<folder>" \
  --only cv_payload_en --only letter_payload_en          # add the _de keys if German
```

Then do 04B and render it in a second pass with `--only interview_prep_payload_en`.
Interview prep is the longest single step in a run and the least load-bearing — it is a
private study aid, not something an employer sees. Rendering everything in one call at the
end means a prep step that overruns or is killed takes the CV and cover letter with it,
which is exactly how one run ended with no PDFs at all despite both documents having
already passed verification. If prep fails after 06A, the application is still complete;
say so in the summary and move on.

Headless runs take that one step further: the watcher reports the application the moment the
required PDFs are on disk and have stopped growing, without waiting for prep. The user gets a
"ready" message at 06A time and a short "run complete" once the process exits. So a slow prep
step costs nobody any waiting — do not be tempted to skip it for speed.

### Steps 06/08/09 are one script, run once

`scripts/qa_application.py` does the whole deterministic half of QA in a single pass: the
LaTeX healthcheck, page counts against the limits, PDF text extraction, the AI-fingerprint
scan across every document, the folder inventory, and rasterisation of every page into
`_tmp/pdf_pages/<stem>/`.

```
python scripts/qa_application.py "<folder>"                        # 06A, after 05A
python scripts/qa_application.py "<folder>" --require-prep         # 06B, after 05B
python scripts/qa_application.py "<folder>" --json                 # feeds 08 and 09
```

Pass `--no-images` on the two gate checks (06A/06B) — nobody is looking at pages yet, and
rasterisation is the slow part. Prep is not required unless `--require-prep` is given, which
is what lets 06A pass before prep exists.

The models in steps 08 and 09 start from that report and judge only what needs a reader:
language, tone, register, and how the rendered page actually looks. They must not re-run
the healthcheck, re-grep PDF text, or call `pdf_to_images.py` again. Ten healthchecks and
three rasterisations of the same unchanged PDFs was the largest single block of waste in the
run this was written for.

Before step 09 can PASS, clean the deliverable folder:

```
python scripts/clean_deliverable.py --folder "<absolute deliverable folder path>"
```

Keep only the final `.tex` + PDF for each application document produced (2 for English-only
runs, 4 when German was requested), plus the Interview Prep `.tex` + PDF pair (always
English, on top of the application-document count), and exactly one archived posting
`.html`. The script keeps `.tex`/`.pdf`/`.html`, moves payload JSON and the Match Brief and
Research Note to `_tmp/payloads/` (so a later rule change can be re-rendered rather than
regenerated), deletes LaTeX build artifacts and page images, and reports anything it does
not recognise. Do **not** try to do this with `rm` — the headless guard blocks every
`rm -f`/`rm -rf` and the attempt only burns turns. Check the script's "left in place"
list and deal with those files yourself.

## Parallel Execution

Independent steps run in parallel — always, not only "when speed matters". Issue the two
Agent calls as two tool_use blocks in **one** message; a second message is a second turn and
runs after the first has finished.

Phase A (after step 00): run 01A and 01B in parallel.
Phase B (after 01A, 01B): run the CV track (02A→03A) and the letter track (02B→03B) in parallel. Each track is sequential within itself; the two tracks are not.
Phase C (after 03A, 03B): two independent tracks, started in the same message.
  - **Documents:** 04A first if German was requested (05A renders what it produces), then 05A → 06A. English-only runs go straight to 05A.
  - **Prep:** 04B, then 05B → 06B. It shares no dependency with the documents track.
  The model steps overlap freely. The two renderer calls (05A, 05B) must not — they write
  into the same folder, so let one finish before starting the other. In practice 04B is long
  enough that 05A is done well before 05B is ready.
Phase D (after 06A and 06B): proofread → final QA → clean → tracker log, sequentially.
  Both model steps read the report from one `qa_application.py --json` run; neither repeats
  its deterministic work.

If the prep track fails or is killed, finish Phase D on the application documents alone
rather than abandoning the run — a complete application without the study aid is worth far
more than neither. 06A is the point from which that is true: once it passes, the employer-
facing half of the run is finished and checked, and headless runs announce it right there.

## Application Tracker (step 10)

Every completed run gets one row in `<YYYY>/Job Applications Tracker <YYYY>.xlsx`. Run it
yourself after final QA passes — never delegate it to a subagent, and never edit the workbook
by hand:

```
python scripts/append_tracker_entry.py \
  --company "<Company>" --position "<Role>" --date <YYYY-MM-DD> \
  --location "<City>" --country "<Country>" \
  --folder "<absolute deliverable folder path>"
```

Rules:
- `--company`, `--position`, `--date` must match the deliverable folder exactly (`<Company>/<YYYY-MM-DD> - <Role>`); those three fields are the dedupe key, so a re-run updates the row instead of adding a second one.
- `--location` and `--country` are required — no row may land with an empty location. Take them from the posting's own location field or the letter's recipient address, never from a guess about where the company is headquartered. A fully remote role is `--location Remote` plus the country the contract sits in. If the posting genuinely names several cities, join them with ` / `. If you truly cannot establish it, ask the user rather than inventing a city.
- The optional flags are `--status`, `--next-action`, `--follow-up`, `--salary`, `--notes`. Omit any you have no real value for; blank flags are skipped, so hand-entered values are never overwritten.
- `--status` defaults to `Applied`; leave it at the default. Status changes after that are the user's to make in Excel.
- The script reads its columns from the sheet's header row, so the user may add or remove columns in Excel without breaking the run. Do not re-add a column the user has deleted.
- If the workbook for that year does not exist yet, the script creates it — no separate setup step.
- Keep passing the **absolute** path to `--folder`, exactly as `scaffold.py` printed it. The script stores the workspace-relative form (`2026/<Company>/<date> - <Role>`) in the `Application Folder` column, so the row still resolves after the workspace is moved to another drive, cloned to another machine, or read on another OS. Everything this system persists follows that rule — see `scripts/workspace_paths.py`, and `scripts/normalize_stored_paths.py` to repair values written before it.
- Missing/stale tracker row is not a reason to fail the run: if the script errors, report it in the step 11 summary and move on.

Both tracker scripts need `openpyxl` (already installed; `python -m pip install openpyxl` if a
new environment lacks it). The workbook lives outside every deliverable folder, so the step-09
cleanup rule does not touch it.

`scripts/make_application_tracker.py` rebuilds a year's workbook from scratch (prefilling rows
from the folder tree). It refuses to overwrite an existing file without `--force` — do not pass
`--force` on a workbook that has user-entered status data. Its column list is the *initial*
layout only; the live sheet is whatever the user has since made it.

## Model Assignment

Each subagent pins its own Anthropic model in its frontmatter (`.claude/agents/<agent>.md`),
tiered by effort and tuned for token cost:

- Opus — the two generative writing steps only: `03-cv-writer-en`, `05-cover-letter-writer-en`.
- Sonnet — matching, research, all verification, German translation, interview prep, proofreading, final QA, and LaTeX/script maintenance (`01`, `02`, `04`, `06`, `07`, `08`, `09`, `10`, `11`).
- Haiku — capture, scaffolding, and extraction (`00-posting-archiver`).

Run the main/orchestrator model on Sonnet, not Opus. The orchestrator carries the whole run's
growing context and only does coordination and glue work, so Opus there is the single most
expensive and least useful place to spend it. Opus is reserved for the two subagents where
prose quality originates.

There is no cross-provider routing or fallback runner in Claude Code; the Agent tool invokes
the subagent with its pinned model directly. Do not escalate an agent to a heavier model
without asking the user first.

## Token Discipline

Keep cross-agent handoffs compact. Match Brief: 600–900 words. Research Note: 500–800 words. Pass Match Brief + Research Note + output path + relevant slice to writing agents — not full raw pages. Reference archived posting files instead of pasting their full text.

Do not ask a model to debug deterministic LaTeX/PDF defects. Run `scripts/qa_application.py` first. Fix via payload/template/rendering locally before calling a verifier.

Do not let an agent re-derive the toolchain. Script signatures, payload keys, page limits, and
the locked layout constants are in `rules/slices/_toolchain.md`; every agent slice points at it.
Reading `scripts/` or `master/LaTeX/` source to work out an argument costs a turn per file and
has produced wrong guesses. Reading a past application's payload for phrasing is worse — it is
off-spec, since only `/master` and `rules/` are fact sources.

Two files that are not worth a turn:

- **Never `Read` the archived posting `.html` whole.** SingleFile inlines every image and
  stylesheet, so these run to hundreds of KB — past the Read limit, which fails the call
  outright and returns nothing (879 KB, twice, in one run). The posting has already been
  parsed into the Match Brief by 01A; use that. If you genuinely need the raw file, `Grep`
  it for the phrase you are after, or `Read` it with `offset`/`limit` — never bare.
- **Do not read `.claude/settings*.json` or anything else under `.claude/`.** Headless runs
  deny it, so every attempt is a blocked call and a wasted turn. If a tool call is being
  denied, the fix is in `automation/build_settings.json` and belongs to the user, not to the
  run in progress — report it and carry on with what is permitted.

## Workspace Map

- `rules/00-canonical-profile.md` — full candidate facts, single source of truth.
- `rules/slices/` — per-agent compact rule slices.
- `rules/slices/_toolchain.md` — script signatures, payload keys, page limits, locked layout
  constants. Agents read this instead of `scripts/` or `master/LaTeX/` source;
  `11-latex-workflow-engineer` owns it and updates it whenever an interface changes.
- `.claude/agents/` — Claude Code specialist-subagent files.
- `master/LaTeX/templates/` — locked LaTeX templates (cv_en, cv_de, letter_en, letter_de, interview_prep_en).
- `master/LaTeX/shared/` — shared LaTeX macros/style.
- `scripts/` — scaffold, render, healthcheck, one-pass QA, toolchain check, `capture_posting.py`
  for posting capture (SingleFile with a rendered-browser fallback), tracker
  build/append, `clean_deliverable.py` to tidy a finished folder down to its deliverables,
  and `cleanup_application.py` to undo a failed run entirely. The two are not
  interchangeable: the first runs on success, the second erases the application.
  `workspace_paths.py` defines how a path is written down (relative to the workspace
  root, forward slashes) and `normalize_stored_paths.py` repairs stores written
  before it. Neither is part of a run.
- `<YYYY>/Job Applications Tracker <YYYY>.xlsx` — application tracker, one row per application.
- `automation/` — the job watcher (polling, matching, Telegram, headless builds). Self-contained:
  its own `.venv`, config, sqlite state, and logs. Nothing in the pipeline reads from it.

## Locked Settings

- Output: `.tex + .pdf` per document; no DOCX by default.
- Interview prep: `.tex` + PDF (same shared LaTeX styling as CV/letter) as `<file_prefix> - Interview Prep.tex`/`.pdf` (`<file_prefix>` from identity.toml `[person].file_prefix`), English only, no strict page cap.
- Languages: English only by default; add German (Lebenslauf + Anschreiben) only when the user requests it.
- Folder: `<YYYY>/<Company>/YYYY-MM-DD - <Role>/`.
- No AI fingerprints in application documents (CV, Lebenslauf, cover letter, Anschreiben). No authorship signature or tool credit, no assistant voice, no meta-commentary, no bracketed placeholders, and no model or AI-vendor name in any casing — Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Gemini, Llama, Mistral, Copilot, Ollama, LM Studio, LangChain, MCP, or any other assistant/foundation-model brand. Canonical entries are not exempt: `OpenAI Gym` renders as "reinforcement-learning environments (Gym)", `Ollama` as "self-hosted LLM runtime". Generic terms (LLM, NLP, computer vision, PyTorch) are unaffected. Only the employer's own role title, company name, and product name may keep a banned word, never a line describing the candidate. Interview Prep is exempt from the name ban but not from the authorship-signature ban. Full rule: `rules/07-humanlike-anti-ai.md` section F.
- Source of truth: `/master` + `rules/00-canonical-profile.md`. Nothing outside `/master` and
  `rules/` is a fact source — not past application folders, not the posting, not the web. If the
  workspace holds a `/Documents` folder of scanned personal papers (residence permit, degree
  certificates, references), those are attachments for an application, never a fact source and
  never a style reference.
- Never create compiler working directories inside the deliverable folder.

## Stop And Ask Only When

- Posting requires a claim not in `rules/00-canonical-profile.md` (certification, clearance, missing tool, completed doctorate, inflated seniority).
- URL cannot be captured and no posting text was pasted.
- Company name is genuinely ambiguous for folder naming.

## If A Run Fails Or Is Abandoned

A run that does not finish leaves debris — a dated folder with no PDFs or only some
of them, a tracker row if step 10 already fired, and payload/rasterisation scratch in
`_tmp/`. None of it should survive. A folder with no documents reads as "already
applied" to anyone scanning the tree, and a tracker row for an application that was
never sent is simply false.

So before reporting a failure, erase the run:

```
python scripts/cleanup_application.py \
  --folder "<absolute deliverable folder path>" \
  --reason "<one line: what failed>"
```

Rules:
- Run it for any run that ends without a complete set of deliverables: a hard failure,
  a crash, a stop-and-ask the user declined, or a run that produced only some of its
  documents. Do not leave a partial folder behind "for reference".
- This script is the only sanctioned way to delete an application. Never `rm -rf` a
  folder and never edit the workbook by hand — the headless guard hook blocks
  recursive deletes outright, so a manual attempt fails mid-run anyway.
- It removes the dated folder, its company folder if that leaves it empty, the matching
  tracker row (company + position + date, the same key `append_tracker_entry.py` writes
  with), and that application's `_tmp` payloads and page images. The workbook itself is
  never deleted, only the one row.
- It refuses any path that is not `<YYYY>/<Company>/<YYYY-MM-DD> - <Role>` inside the
  workspace, and it is idempotent: running it twice, or on a run that left nothing
  behind, is a no-op. Add `--dry-run` to see what it would remove.
- Then report the failure plainly — what broke, and that nothing was left behind.

Headless runs get the same treatment without being asked: the watcher runs this script
itself whenever a build fails or comes out incomplete, so a Telegram-approved build
never leaves a half-application on disk either.

One exception, and only headlessly: a run that ends on a **stop-and-ask** keeps its folder
while the question is open in Telegram. The user can answer there, which resumes this same
session with the folder, Match Brief and Research Note still in place — the run is paused,
not abandoned. The watcher erases it when they decline, cancel, or leave it unanswered for
two days. Nothing changes for the run itself: stop and say why, exactly as above, and do
not clean up on the way out.

### If the user declines after a stop-and-ask

When a stop-and-ask flag (unsupported claim, skills gap, work-authorization mismatch, or
similar) leads the user to decline drafting for that posting, clean up the application
folder created for it — archived posting, Match Brief, Research Note, and everything else
— with the command above, without asking for a second confirmation. It's not worth keeping
as a record. Do this automatically. Only ever the specific dated folder (and its
now-empty parent company folder); sibling folders for other roles or dates under the same
company are never touched, which is exactly what the script's path check enforces.

## Running Multiple Applications At Once

Text-only steps are safe to overlap. PDF rendering is short and per-application; keep it sequential within each run.

## Setup On A Fresh Clone

The repo is person-neutral: the two files that carry a real person — `identity.toml` (contact
block) and `rules/00-canonical-profile.md` (fact source) — are git-ignored and created per
install. On a fresh clone:

1. `git clone <repo>` and open the folder.
2. `python scripts/init_workspace.py` — creates the local scaffolding and the placeholder
   personal files.
3. Run the `12-workspace-init` agent. It interviews the new user, reads their CV, and drafts
   `identity.toml`, `rules/00-canonical-profile.md`, and `sources.toml` from that CV and the
   answers.
4. `python scripts/init_workspace.py --verify` — confirms the workspace is complete and
   consistent before the first application run.
