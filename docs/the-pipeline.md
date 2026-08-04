# The Pipeline

`CLAUDE.md`, at the repository root, is not documentation of the pipeline — it *is* the runtime
instruction set that Claude Code reads on every run, interactive or headless. This page explains
the same pipeline in narrative form; if the two ever disagree, `CLAUDE.md` is the source of
truth.

## Trigger

The pipeline starts from a job posting URL or pasted posting text, optionally with a short note
(`apply as Data Scientist`, `add German`). That is true whether you paste it into an interactive
`claude` session yourself, or the watcher spawns `claude -p` with the same input after you reply
`yes` to a Telegram ping (see [The Watcher](./watcher.md)). There is exactly one trigger and one
instruction set — nothing headless-specific is defined anywhere else.

German output (Lebenslauf + Anschreiben) is opt-in per run; English is produced every time.

## The 11 steps

| # | Step | Agent/tool | Output | Depends on |
|---|---|---|---|---|
| 00 | Capture + archive posting; create folder | `00-posting-archiver` | folder + `.html` | posting text/URL |
| 01A | Parse posting, requirements map, fit/gap | `01-experience-matcher` | Match Brief | 00 |
| 01B | Research company and HR contact | `02-company-role-researcher` | Research Note | 00 |
| 02A | Draft English CV payload | `03-cv-writer-en` | `cv_payload_en` | 01A, 01B |
| 02B | Draft English cover-letter payload | `05-cover-letter-writer-en` | `letter_payload_en` | 01A, 01B |
| 03A | Verify English CV payload | `04-cv-verifier-en` | PASS/FIXED/REJECTED | 02A |
| 03B | Verify English cover-letter payload | `06-cover-letter-verifier-en` | PASS/FIXED/REJECTED | 02B |
| 04A | German Lebenslauf + Anschreiben payloads (only if requested) | `07-translator-de` | `cv_payload_de`, `letter_payload_de` | 03A, 03B |
| 05A | Render + compile the application documents | `scripts/render_latex_application.py` | CV + letter `.tex` + PDF (×2 if German) | 03A, 03B, 04A |
| 06A | Healthcheck the application documents | `scripts/latex_healthcheck.py` | PASS/FAIL | 05A |
| 04B | Interview prep payload | `08-interview-prep` | `interview_prep_payload_en` | 01B, 03A, 03B |
| 05B | Render + compile the interview prep | `scripts/render_latex_application.py` | Interview Prep `.tex` + PDF | 04B |
| 06B | Healthcheck the interview prep | `scripts/latex_healthcheck.py` | PASS/FAIL | 05B |
| 08 | Proofread final PDFs | `09-proofreader` | PASS/FIXED/ESCALATED | 06A, 06B |
| 09 | Final QA across the whole folder | `10-final-verifier` | PASS/REJECTED | 08 |
| 10 | Log the application in the yearly tracker | `scripts/append_tracker_entry.py` | row in the tracker workbook | 09 |
| 11 | Present the files | the orchestrator | links + summary | 10, 05B |

Render and compile used to be steps 05 and 06 for the whole run at once, with a single
healthcheck at 07. They are now split per track — the numbering of 08 onwards is unchanged
so that references to "step 09" and "step 10" elsewhere still mean what they always did.

### Parallel execution

- **Phase A** (after step 00): 01A and 01B run in parallel.
- **Phase B** (after 01A, 01B): the CV track (02A → 03A) and the letter track (02B → 03B) run in
  parallel. Each track is sequential within itself; the two tracks are not.
- **Phase C** (after 03A, 03B): two independent tracks run at once — the documents track
  (04A if German, then 05A → 06A) and the prep track (04B, then 05B → 06B). The renderer
  calls themselves do not overlap; they write into the same folder.
- **Phase D** (after 06A and 06B): proofread → final QA → clean → tracker log, strictly
  sequential.

The application documents are deliberately rendered before the interview prep rather than
alongside it. Prep is the longest step in a run and the least load-bearing — a private study
aid, not something an employer sees. When everything was rendered in one call at the end, a
prep step that overran took the CV and cover letter down with it; one run finished with no
PDFs at all despite both documents having already passed verification. If the prep track
fails now, the application is still complete.

## The agent roster

Each step is a Claude Code subagent defined under `.claude/agents/`, numbered to match the table
above:

| Agent | Role |
|---|---|
| `00-posting-archiver` | Captures the posting, decides company/role naming, scaffolds the dated folder. |
| `01-experience-matcher` | Maps posting requirements to the canonical profile; produces the Match Brief with an honest gap analysis. |
| `02-company-role-researcher` | Researches the company, product, and hiring contact; produces the Research Note. |
| `03-cv-writer-en` | Drafts the structured English CV payload. |
| `04-cv-verifier-en` | Verifies that payload for factual integrity, relevance, voice, and LaTeX layout risk. |
| `05-cover-letter-writer-en` | Drafts the structured English cover-letter payload. |
| `06-cover-letter-verifier-en` | Verifies that payload for integrity, company specificity, voice, and one-page risk. |
| `07-translator-de` | Localizes verified English payloads into German Lebenslauf/Anschreiben payloads. |
| `08-interview-prep` | Writes the private interview-prep payload. |
| `09-proofreader` | Proofreads the compiled PDFs after the deterministic healthcheck passes. |
| `10-final-verifier` | Runs whole-folder QA before anything is presented. |
| `11-latex-workflow-engineer` | Maintains and debugs the LaTeX templates and rendering scripts; never used for application prose. |
| `12-workspace-init` | One-time, run by hand on a fresh clone — see [Getting Started](./getting-started.md). Not part of the per-application pipeline. |

## Model routing

Each subagent pins its own model in its own frontmatter (`.claude/agents/<agent>.md`), tiered by
effort and token cost:

- **Opus** — the two generative writing steps only: `03-cv-writer-en`, `05-cover-letter-writer-en`.
- **Sonnet** — matching, research, all verification, German translation, interview prep,
  proofreading, final QA, and LaTeX/script maintenance (agents `01`, `02`, `04`, `06`, `07`, `08`,
  `09`, `10`, `11`, `12`).
- **Haiku** — posting capture and folder scaffolding (`00-posting-archiver`).

The orchestrator itself (the top-level Claude Code session or headless `claude -p` run) is
pinned to Sonnet, not Opus — it only coordinates the run and carries the whole run's growing
context, so Opus there would be the single most expensive and least useful place to spend it.
There is no cross-provider routing or fallback: the Agent tool invokes each subagent with its
pinned model directly.

## Payload-vs-prose architecture

Models write and verify **structured JSON payloads** (`cv_payload_en`, `letter_payload_en`,
`cv_payload_de`, `letter_payload_de`, `interview_prep_payload_en`); deterministic Python
(`scripts/render_latex_application.py`) renders those payloads into the locked LaTeX templates
in `master/LaTeX/templates/` and compiles the PDFs. No model is ever asked to write LaTeX
directly, and no model is ever asked to debug a compiler error —
`scripts/latex_healthcheck.py` runs first and any defect is fixed at the payload, template, or
rendering level before a verifier agent is called again. See
[Customising](./customising.md) for the template and placeholder mechanism itself.

## The rules system

Two layers of rules feed the agents:

- `rules/00-canonical-profile.md` is the single fact source: every claim in a generated document
  must trace back to it (or to `/master`), never to the posting, the web, or what would be
  plausible. It carries an explicit **"may not be claimed"** list.
- `rules/slices/<agent>.md` are compact, per-agent excerpts of the broader rule set (writing
  style, CV/letter rules, German-market conventions, integrity, naming, anti-AI-fingerprint).
  Agents read their slice instead of the full rule files, and the orchestrator passes slices
  rather than full documents to keep cross-agent handoffs compact — Match Brief 600–900 words,
  Research Note 500–800 words.

## Honesty and stop-and-ask

The pipeline does not fabricate. Every claim traces to the canonical profile; the "may not be
claimed" list exists precisely so a verifier can catch a claim the profile does not support
before anything is rendered. The run stops and asks the user, rather than inventing an answer,
only when:

- the posting requires a claim not in the canonical profile (a certification, a clearance, a
  missing tool, a completed degree, inflated seniority);
- the posting URL cannot be captured and no posting text was pasted;
- the company name is genuinely ambiguous for folder naming.

This matters especially for headless runs: a `claude -p` build spawned by the watcher has nobody
to ask follow-up questions. If it hits one of these conditions it says so and stops — the
message lands back in the Telegram thread rather than the run inventing an answer to keep going.

If a stop-and-ask leads you to decline drafting for a posting, the entire application folder for
it (archived posting, Match Brief, Research Note, and anything else generated) is deleted
automatically, without a second confirmation — only that specific dated folder, never sibling
folders for other roles or dates under the same company.

## Deliverable folder layout and cleanup

Every application produces `<YYYY>/<Company>/<YYYY-MM-DD> - <Role>/`. Before step 09 can pass,
the folder is cleaned down to exactly:

- the final `.tex` + PDF for each application document produced — 2 files for an English-only
  run, 4 when German was requested;
- the Interview Prep `.tex` + PDF pair (always English, on top of the application-document
  count);
- exactly one archived posting `.html`.

Payload JSON, raw posting text, API captures, logs, and build artifacts are removed from the
deliverable folder — they never belong there in the first place, only in scratch space such as
`_tmp/`.

`scripts/clean_deliverable.py` does this, and it is the only sanctioned way — the headless
guard blocks `rm`, so a build that improvises one burns turns getting nowhere:

```bash
python scripts/clean_deliverable.py --folder "<absolute deliverable folder path>"
```

It keeps `.tex`, `.pdf` and `.html`; *moves* payload JSON, the Match Brief, the Research Note
and extracted posting text to `_tmp/payloads/<Company> <date> <Role>/` rather than deleting
them, so a later rule or template change can be re-rendered instead of regenerated; and
deletes only what is regenerable by definition — LaTeX build artifacts and rasterised page
images. Anything it has no rule for is left alone and printed under "left in place", so an
unexpected file is a line of output rather than a loss. `--dry-run` reports without changing
anything, and running it twice is a no-op.

Do not confuse it with `cleanup_application.py` below: this one tidies a run that *succeeded*,
that one erases a run that *failed*.

## The tracker step

After final QA passes, the orchestrator (never a subagent) appends one row to
`<YYYY>/Job Applications Tracker <YYYY>.xlsx`:

```bash
python scripts/append_tracker_entry.py \
  --company "<Company>" --position "<Role>" --date <YYYY-MM-DD> \
  --location "<City>" --country "<Country>" \
  --folder "<absolute deliverable folder path>"
```

`--company`, `--position`, and `--date` are the dedupe key — matching them exactly to the
deliverable folder means a re-run updates the row instead of duplicating it. `--location` and
`--country` are required and must come from the posting or the letter's recipient address, never
a guess. `--status` defaults to `Applied` and is left there; status changes after that belong to
you, in Excel. The script reads its columns from the sheet's header row, so you may add or
remove columns without breaking future runs. See [Customising](./customising.md) for the two
maintenance scripts that build and backfill the workbook.

## When a run does not finish

The reverse of the tracker step. A run that fails, crashes, or stops at a question you decline
leaves a dated folder with no documents (or only some), possibly a tracker row, and payload and
page-image scratch in `_tmp/`. All of it is erased before the failure is reported:

```bash
python scripts/cleanup_application.py \
  --folder "<absolute deliverable folder path>" \
  --reason "<one line: what failed>"
```

That removes the dated folder, its company folder if that leaves it empty, the matching tracker
row, and the run's `_tmp` scratch — never the workbook itself, and never a sibling folder for
another role or date. It refuses any path that is not shaped
`<YYYY>/<Company>/<YYYY-MM-DD> - <Role>` inside the workspace, and running it twice is a no-op.
`--dry-run` reports without deleting.

Headless builds do this on their own: the watcher calls the same script whenever a build fails
or comes back missing documents, and the Telegram reply lists what was removed.
