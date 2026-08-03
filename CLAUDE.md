# Job Application Automation — Claude Code / Cowork Project Instructions

This folder is the workspace owner's job-application workspace (identity in identity.toml, facts in rules/00-canonical-profile.md) for the German / EU market.

Claude Code / Cowork is the active agent system. It uses native subagents defined in
`.claude/agents/`. (This project was migrated from an earlier OpenCode setup; the old
OpenCode config, runner scripts, and the previous DOCX-based Claude setup have been removed.)

## Trigger

When the user pastes a job posting URL or the posting text, with or without notes like "apply as Data Scientist" or "add German", act as the orchestrator and run the full pipeline in one go. German (Lebenslauf + Anschreiben) is opt-in: produce English only unless the user asks for German (e.g. "add German", "German too", "EN+DE").

1. Use `rules/slices/<agent>.md` for per-agent rule context instead of listing individual rule files.
2. Dispatch specialist work through the Task tool, selecting the matching subagent in `.claude/agents/` (e.g. `01-experience-matcher`, `03-cv-writer-en`).
3. Stop to ask the user only if integrity is blocked, the URL cannot be captured with no text given, or the company name is ambiguous.
4. Fire independent pipeline steps in parallel through the Task tool whenever dependencies allow.

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
Browser MCP is not used — use pasted text, `webfetch`, and `scripts/save_singlefile.sh`.
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
| 04B | Interview prep payload | `08-interview-prep` | `interview_prep_payload_en` | 01B, 03A, 03B |
| 05 | Render LaTeX from payloads | `scripts/render_latex_application.py` | up to 5 `.tex` files | 03A, 03B, 04A, 04B |
| 06 | Compile PDFs | `scripts/render_latex_application.py` | up to 5 PDFs | 05 |
| 07 | Local deterministic healthcheck | `scripts/latex_healthcheck.py` | PASS / FAIL | 06 |
| 08 | Proofread final PDFs | `09-proofreader` | PASS/FIXED/ESCALATED | 07 |
| 09 | Final QA across entire folder | `10-final-verifier` | PASS / REJECTED | 08 |
| 10 | Log the application in the yearly tracker | `scripts/append_tracker_entry.py` | row in `<YYYY>/Job Applications Tracker <YYYY>.xlsx` | 09 |
| 11 | Present files to user | assistant | links + 3-line summary | 10, 04B |

Before step 09 can PASS, clean the deliverable folder. Keep only the final `.tex` + PDF for each application document produced (2 for English-only runs, 4 when German was requested), plus the Interview Prep `.tex` + PDF pair (always English, on top of the application-document count), and exactly one archived posting `.html`. Remove payload JSON, raw posting text, API captures, logs, and build artifacts from the deliverable folder.

## Parallel Execution

Phase A (after step 00): run 01A and 01B in parallel.
Phase B (after 01A, 01B): run CV track (02A→03A) and letter track (02B→03B) in parallel when speed matters; otherwise sequential is fine.
Phase C (after 03A, 03B): run 04A and 04B in parallel.
Phase D (after 04A, 04B): render → compile → healthcheck → proofread → final QA → tracker log sequentially.

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
- Missing/stale tracker row is not a reason to fail the run: if the script errors, report it in the step 11 summary and move on.

Both tracker scripts need `openpyxl` (already installed; `python -m pip install openpyxl` if a
new environment lacks it). The workbook lives outside every deliverable folder, so the step-09
cleanup rule does not touch it.

`scripts/make_application_tracker.py` rebuilds a year's workbook from scratch (prefilling rows
from the folder tree). It refuses to overwrite an existing file without `--force` — do not pass
`--force` on a workbook that has user-entered status data. Its column list is the *initial*
layout only; the live sheet is whatever the user has since made it.

`scripts/backfill_tracker_locations.py` is the one-off that filled locations for the pre-tracker
2026 applications, kept as the audit trail for where each one came from.

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

There is no cross-provider routing or fallback runner in Claude Code; the Task tool invokes
the subagent with its pinned model directly. Do not escalate an agent to a heavier model
without asking the user first.

## Token Discipline

Keep cross-agent handoffs compact. Match Brief: 600–900 words. Research Note: 500–800 words. Pass Match Brief + Research Note + output path + relevant slice to writing agents — not full raw pages. Reference archived posting files instead of pasting their full text.

Do not ask a model to debug deterministic LaTeX/PDF defects. Run `scripts/latex_healthcheck.py` first. Fix via payload/template/rendering locally before calling a verifier.

## Workspace Map

- `rules/00-canonical-profile.md` — full candidate facts, single source of truth.
- `rules/slices/` — per-agent compact rule slices.
- `.claude/agents/` — Claude Code specialist-subagent files.
- `master/LaTeX/templates/` — locked LaTeX templates (cv_en, cv_de, letter_en, letter_de, interview_prep_en).
- `master/LaTeX/shared/` — shared LaTeX macros/style.
- `scripts/` — scaffold, render, healthcheck, toolchain check, SingleFile capture, tracker build/append.
- `<YYYY>/Job Applications Tracker <YYYY>.xlsx` — application tracker, one row per application.
- `automation/` — the job watcher (polling, matching, Telegram, headless builds). Self-contained:
  its own `.venv`, config, sqlite state, and logs. Nothing in the pipeline reads from it.

## Locked Settings

- Output: `.tex + .pdf` per document; no DOCX by default.
- Interview prep: `.tex` + PDF (same shared LaTeX styling as CV/letter) as `<file_prefix> - Interview Prep.tex`/`.pdf` (`<file_prefix>` from identity.toml `[person].file_prefix`), English only, no strict page cap.
- Languages: English only by default; add German (Lebenslauf + Anschreiben) only when the user requests it.
- Folder: `<YYYY>/<Company>/YYYY-MM-DD - <Role>/`.
- No AI fingerprints in application documents (CV, Lebenslauf, cover letter, Anschreiben). No authorship signature or tool credit, no assistant voice, no meta-commentary, no bracketed placeholders, and no model or AI-vendor name in any casing — Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Gemini, Llama, Mistral, Copilot, Ollama, LM Studio, LangChain, MCP, or any other assistant/foundation-model brand. Canonical entries are not exempt: `OpenAI Gym` renders as "reinforcement-learning environments (Gym)", `Ollama` as "self-hosted LLM runtime". Generic terms (LLM, NLP, computer vision, PyTorch) are unaffected. Only the employer's own role title, company name, and product name may keep a banned word, never a line describing the candidate. Interview Prep is exempt from the name ban but not from the authorship-signature ban. Full rule: `rules/07-humanlike-anti-ai.md` section F.
- Source of truth: `/master` + `rules/00-canonical-profile.md`. The pre-2026 archives
  (`/Targeted`, `/Non Targeted`, `/2025`) were deleted on 2026-08-02; nothing outside `/master`
  and `rules/` is a fact source. `/Documents` holds scanned personal papers (residence permit,
  degree certificate, Zwischenzeugnisse) — attachments for an application, never a fact source
  and never a style reference.
- Never create compiler working directories inside the deliverable folder.

## Stop And Ask Only When

- Posting requires a claim not in `rules/00-canonical-profile.md` (certification, clearance, missing tool, completed doctorate, inflated seniority).
- URL cannot be captured and no posting text was pasted.
- Company name is genuinely ambiguous for folder naming.

## If The User Declines After A Stop-And-Ask

When a stop-and-ask flag (unsupported claim, skills gap, work-authorization mismatch, or similar) leads the user to decline drafting for that posting, delete the entire application folder created for it (archived posting, Match Brief, Research Note, and any other generated files) without asking for a second confirmation — it's not worth keeping as a record. Do this automatically; no need to check with the user first. Only ever delete the specific dated application folder (and its now-empty parent company folder, if the company has no other applications) — never touch sibling folders for other roles/dates under the same company.

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
