# Toolchain Contract

Everything a pipeline agent needs to know about the deterministic scripts and the
locked LaTeX layout. **Read this instead of the script source.** The signatures and
constants below are the contract; if a run needs something that is not here, say so
in its output rather than reading `scripts/` or `master/LaTeX/` to work it out.

Reading script or template source to re-derive an interface costs a turn per file and
has produced wrong guesses (`--folder` on a script whose folder argument is
positional). Reading another application's payload to copy its phrasing is worse: it
is off-spec. Only `/master` and `rules/` are fact sources — never a past application
folder, never `_tmp/payloads/`.

In headless runs this is enforced, not advisory: the build guard blocks a `Read` of any
deliverable folder dated before this run, and of `_tmp/payloads/` outright. Do not try it
and then work around the refusal — a blocked call is a wasted turn, and the answer you
wanted is in this file or in `rules/`.

**Exempt: `11-latex-workflow-engineer`.** That agent maintains the scripts and templates,
so reading and editing their source is its job. When it changes an interface, it updates
this file in the same run — otherwise every other agent is working from a stale contract.

## Script signatures

Run every script from the workspace root. Paths with spaces need quoting.

```
python scripts/capture_posting.py <url> <output.html>   # the only capture call you need
       [--timeout 120]        # seconds per method
       [--min-chars 600]      # visible text below which a capture is rejected
       [--renderer auto|singlefile|playwright]
       # Tries SingleFile, checks the result is really the posting, and renders
       # the page in a browser if it is not. Prints METHOD/TEXT/HTML, and NOTE
       # when the first method was rejected. Exit 1 only if both fail.

python scripts/scaffold.py "<Company>" "<Role>"     # creates the deliverable folder
       [--date YYYY-MM-DD]   # default today
       # prints the absolute path on stdout — capture it, do not rebuild it by hand

python scripts/render_latex_application.py <payload.json> <target_folder>
       [--only PAYLOAD_KEY]   # repeatable; render exactly these keys
       [--no-pdf]             # .tex only, skip PDF compilation
       [--cv-only]            # recruiter-CV folder; only cv_payload_en required

python scripts/latex_healthcheck.py <folder>          # folder is POSITIONAL, not --folder
       [--cv-only] [--expect-phd]

python scripts/qa_application.py <folder>             # one-pass QA, see below
       [--json] [--no-images] [--require-prep]
       [--posting-text FILE]  # plain-text job description for the ATS number;
                              # better input than re-parsing the archived .html

python scripts/ats_report.py <folder>       # the ATS section on its own, for debugging
       [--posting-text FILE] [--json]       # qa_application already runs this

python scripts/pdf_to_images.py <pdf>
       [--outdir DIR]  # default _tmp/pdf_pages/<pdf stem>/
       [--pages "1,3-5"] [--dpi 150]

python scripts/clean_deliverable.py --folder <folder> [--dry-run]

python scripts/cleanup_application.py --folder <folder> --reason "<one line>"
       [--company C --position P --date YYYY-MM-DD]
       [--dry-run] [--keep-tmp] [--keep-tracker]

python scripts/append_tracker_entry.py --company C --position P --location L --country K
       [--date YYYY-MM-DD]   # default today
       [--status S] [--next-action A] [--follow-up YYYY-MM-DD] [--salary S]
       [--folder F] [--notes N]

python scripts/check_latex_toolchain.py    # no args; reports engine availability
```

## QA report shape

`--json` prints one object. Four keys decide nothing and two decide everything:

| Key | Weight |
|---|---|
| `errors`, `fingerprints`, `files.unexpected` | these three, and only these, set `verdict` |
| `documents[]` | per PDF: `pages`, `limit`, `within_limit`, `metadata`, `images` |
| `style` | `hits`, `metrics`, `bullet_runs` — AI tells, report-only |
| `ats` | `brief` and `posting` keyword coverage of the CV, report-only |

`style` and `ats` can never fail a run. Both are also written to
`_tmp/payloads/<Company> <date> <Role>/qa_summary.json`, which is where the watcher
reads them for its Telegram message — outside the deliverable folder, because a stray
`.json` inside it is an unexpected file and unexpected files fail the inventory.

A `null` ATS section means "not measured", not "scored zero": no Match Brief was
archived, or the saved posting page holds no readable description. The number is
keyword coverage, not a recruiter's ATS score — no vendor publishes that formula.

## Payload keys

The renderer dispatches on these exact keys. `--only` takes a key, never a filename.

| Payload key | Template | Output document |
|---|---|---|
| `cv_payload_en` | `cv_en.tex` | CV |
| `cv_payload_de` | `cv_de.tex` | Lebenslauf |
| `letter_payload_en` | `letter_en.tex` | Cover Letter |
| `letter_payload_de` | `letter_de.tex` | Anschreiben |
| `interview_prep_payload_en` | `interview_prep_en.tex` | Interview Prep |

A complete application requires `cv_payload_en` and `letter_payload_en`. Output
filenames are the document label prefixed from `identity.toml` `[person].file_prefix`
— the renderer builds them, so never hard-code a filename.

## Page limits

Enforced by the healthcheck. Overflow is a FAIL, not a warning.

| Document | Pages |
|---|---|
| CV / Lebenslauf | 2 |
| Cover Letter / Anschreiben | 1 |
| Interview Prep | 6 |

## Fixed layout constants

From `master/LaTeX/shared/common.tex`. These are locked — do not re-measure them, and
do not edit the template to make content fit. Trim the payload instead.

- Page: A4, margins top 18mm, bottom 13mm, left 16mm, right 12mm.
- **Usable text width: 182mm.**
- Date column (`\datew`): **38mm**. Entry text hangs at this indent.
- Skill label column (`\xlab`): **46.8mm**, gap (`\xgap`) 3mm.
  **Skill value column is therefore 132.2mm** — the number that decides whether a
  skills row wraps. A row wrapping to a third line is the usual cause of page-3
  overflow.
- Bullet list indent (`discitems` / `dashitems`): 44mm.

Skill values are set `\RaggedRight` with natural word spacing, so a too-long row wraps
rather than stretching. If a row still looks over-justified after rendering, the row is
too long — shorten it in the payload.

## Fit failures

When a document runs long, fix it in the payload, in this order:

1. Trim the longest skills rows (the 132.2mm column is the tightest constraint).
2. Drop the least relevant project or experience bullet.
3. Shorten bullets to one line each.

Never change margins, font size, `\datew`, `\xlab`, or any template file to win space.
Those are locked and a later run will render against the originals anyway.
