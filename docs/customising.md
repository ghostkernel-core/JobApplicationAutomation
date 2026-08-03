# Customising

## LaTeX templates

The active layouts live in `master/LaTeX/templates/`: `cv_en.tex`, `cv_de.tex`, `letter_en.tex`,
`letter_de.tex`, and `interview_prep_en.tex`. Shared macros, colours, and layout live in
`master/LaTeX/shared/common.tex`. These are the files `scripts/render_latex_application.py`
renders a structured payload into before compiling a PDF in a temporary build directory and
copying only the finished `.tex`/`.pdf` back into the deliverable folder — see
[The Pipeline](./the-pipeline.md) for how a payload becomes a document.

Edit a template directly to change layout, spacing, or section structure; the renderer fills in
whatever placeholders and Jinja-style substitutions the template defines from the payload's JSON
keys. Because the templates are shared across every application, a change here affects every
future run, not just one — treat them as locked infrastructure and test a render before trusting
it:

```bash
python scripts/render_latex_application.py payload.json "2026/Company/YYYY-MM-DD - Role"
python scripts/latex_healthcheck.py "2026/Company/YYYY-MM-DD - Role"
```

`master/LaTeX/README.md` documents the folder in more detail, including which files under it are
personal reference material from an earlier Word-to-LaTeX migration and are never a factual or
style source.

## The `identity.toml` placeholder mechanism

Every document's contact block — name, address, phone, email, nationality — is filled in from
`identity.toml` via `{{identity_*}}` placeholders in the templates, resolved by
`scripts/workspace_identity.py`. This is what lets the templates stay generic: the same
`cv_en.tex` produces a correctly addressed CV for whoever's `identity.toml` is loaded.

`identity.toml` has three sections:

- `[person]` — the printed contact block and `file_prefix`, the filename stem used for every
  deliverable (`<file_prefix> - CV.pdf`, conventionally `"Surname, Firstname"`).
- `[en]` / `[de]` — `city_line` and `nationality`, written separately because both are
  language-dependent and cannot be a single value. The full address is composed as
  `"<street>, <city_line>"` — do not repeat the street in `city_line`.

`identity.toml` is not a fact source for document content — `rules/00-canonical-profile.md` is.
It only ever carries the contact block that gets printed on the page. Both files are
git-ignored; `identity.toml.example` and `rules/00-canonical-profile.example.md` are the
committed, person-neutral templates they are created from (`scripts/init_workspace.py`, or the
`12-workspace-init` agent — see [Getting Started](./getting-started.md)).

## Fonts

The templates are typeset in Helvetica Neue by default, which is proprietary and not part of
this repository. `master/LaTeX/shared/common.tex` performs a file-existence check
(`\IfFileExists{fonts/HelveticaNeue-regular.ttf}`): if the four Helvetica Neue weights are
present in `master/LaTeX/fonts/`, they are used; otherwise the bundled, openly licensed
**Liberation Sans** loads automatically. A fresh clone compiles correctly with no font work at
all.

To use your own font, drop the `.ttf` files into `master/LaTeX/fonts/` and adjust the
`\setmainfont` lines in `common.tex` to match. Files you add there stay local unless you commit
them — the Helvetica Neue filenames are explicitly git-ignored.

One warning if you swap fonts: **re-verify that punctuation survives PDF text extraction.** The
proofreading and healthcheck steps both read the compiled PDF back as text (via `pypdf`), and
some fonts silently drop or mangle apostrophes and semicolons during extraction, which breaks
those checks without breaking the visible PDF. Compile one document, run `pdftotext` over it (or
inspect the text `latex_healthcheck.py` extracts), and confirm punctuation survived before
committing to a font change.

`pypdf` reads text only — it has no renderer. When a step needs to *look* at a page as an image
(the proofreading and final-QA steps do this to judge alignment and spacing), rasterise with
`pypdfium2` (`pip install pypdfium2`), or shell out to `pdftoppm`, which ships with both MiKTeX
and Poppler. Do not reach for PyMuPDF: it is AGPL-licensed, and dropping it is precisely why
this project uses `pypdf` (see [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)).

## Tweaking the `rules/` files

`rules/00-canonical-profile.md` is the single fact source for every generated document; changing
career facts means editing it directly (or telling Claude Code what changed and letting it edit
the file for you). `rules/01` through `rules/07` hold writing voice, CV rules, cover-letter
rules, German-market conventions, the integrity/no-fabrication rule, folder-and-naming
conventions, and the anti-AI-fingerprint checklist — these govern *how* every agent writes, not
*what* it may claim.

`rules/slices/<agent>.md` are the compact per-agent excerpts each subagent actually reads, kept
short on purpose to keep cross-agent handoffs cheap. If you change a rule that a slice quotes or
summarizes, check whether the slice needs updating too — the slice, not the full rule file, is
what the agent sees at run time. `rules/slices/_facts.md` is the one exception: it is generated
from the canonical profile (by the `12-workspace-init` agent, or by hand) rather than hand-
written, and the writing/verification agents read it in place of the full profile.

## Per-person stale patterns in the healthcheck

`scripts/latex_healthcheck.py` scans every rendered `.tex`/PDF for a hard-coded list of generic
red flags (unrendered `{{placeholders}}`, `TODO`/`TBD`, model/vendor names, and a few other
repo-wide rules). Those are safe to keep in the script because they apply to anyone using this
system.

Anything specific to you — a superseded date from an old draft, a language level or clearance you
can no longer claim, a tool you've only used privately, a former address or affiliation — goes in
`rules/stale_patterns.txt` instead, one regex per line, blank lines and `#` comments ignored. This
file is git-ignored, exactly like `rules/00-canonical-profile.md`, so it never leaves your
machine. `rules/stale_patterns.example.txt` is the committed, fictional-example template; copy it
to `rules/stale_patterns.txt` and add your own lines. If the live file is absent, the healthcheck
just runs with the generic patterns — nothing to set up on a fresh clone until you have a stale
string to ban.

## Tracker scripts

Three scripts under `scripts/` manage the yearly application tracker workbook
(`<YYYY>/Job Applications Tracker <YYYY>.xlsx`):

- **`append_tracker_entry.py`** — the one used by the pipeline itself, after final QA passes
  (step 10 in [The Pipeline](./the-pipeline.md)). Idempotent on
  company + position + date applied.
- **`make_application_tracker.py`** — rebuilds a year's workbook from scratch, optionally
  prefilling rows from the folder tree. It refuses to overwrite an existing file without
  `--force`; do not pass `--force` on a workbook that already has hand-entered status data. Its
  column list is the *initial* layout only — the live sheet is whatever columns you have since
  added or removed, and `append_tracker_entry.py` reads columns from the header row rather than
  assuming this list.
- **`backfill_tracker_locations.py`** — a one-off, kept only as the audit trail for how
  pre-tracker applications had their locations filled in. Not part of the regular workflow.

The tracker workbook lives outside every deliverable folder, so the per-application cleanup rule
in [The Pipeline](./the-pipeline.md) never touches it, and it is never committed to git — see
[Privacy and Security](./privacy-and-security.md).
