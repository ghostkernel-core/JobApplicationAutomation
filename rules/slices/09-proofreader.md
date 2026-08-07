# Slice: Proofreader

Script interfaces and layout constants are in `rules/slices/_toolchain.md`. Do not read
script or template source to work out an argument — and never open another application's
folder or `_tmp/payloads/` for reference.

## Start here

Run the one-pass QA and work from its report:

```
python scripts/qa_application.py "<folder>" --json
```

It already does, once, everything this step used to do by hand: the LaTeX healthcheck,
page counts against the limits, PDF text extraction, PDF metadata, the full
AI-fingerprint scan, the AI-style scan, the folder inventory, and rasterisation of every
page to `_tmp/pdf_pages/<stem>/`.

Its findings are deterministic — treat a reported fingerprint hit, page overflow, or
unexpected file as a defect to fix, not a judgement to re-check. Do not re-run
`latex_healthcheck.py`, re-grep the PDF text, or call `pdf_to_images.py` yourself; that
work is done and repeating it is the single biggest waste in this step.

Two sections of the report are yours to judge rather than to obey:

- `style` — AI tells found by regex and by count: `hits` (clichés, "not only X but also",
  three paragraphs opening Furthermore/Moreover/Additionally), `metrics` (em-dash density
  per 1,000 characters, flagged as `em_dash_overuse`), and `bullet_runs` (three or more
  consecutive `\item`s opening with the same word). These never change the verdict, and
  they are the one place in the report where context decides: "robustness of fault
  detection" is ordinary engineering English, "robust solutions" is the tell. Rewrite what
  reads as a tell and leave what reads as the candidate. Do not grep for these yourself —
  the list is in `scripts/qa_application.py` and it is already applied.
- `ats` — keyword coverage of the rendered CV against the Match Brief's keyword list and
  against the posting. Report-only, and not something to fix by stuffing words in: a
  missing keyword is worth acting on only when the CV can carry it truthfully and the
  phrasing improves. Never add a skill, tool, or claim to raise it.

What is left for you is what needs a reader: language, tone, register, and whether the
rendered page actually looks right. Read the `images` the report lists for each document.

## Check list

- EN/DE spelling, grammar, punctuation, tone, consistency.
- German formal register: `Sie`, capitalized nouns, correct umlauts (ä ö ü ß), no ASCII transliterations (Nationalitaet, fuer, Strasse, Gruessen, Duesseldorf).
- No empty bullets, placeholders (`{{...}}`, `TODO`, `TBD`), stale dates, mixed company names, old role text.
- Page limits: CV ≤2, letters ≤1.
- Cross-document name/address/phone/email/date consistency.
- No unsupported claims, invented metrics, language-level inflation (German stays B1+) — CV/letter only.
- Voice sounds like the candidate: warm, direct, sincere. Kill AI cliches (thrilled, ideal candidate, unique blend, synergy, em-dash crutch, triadic lists). The report's `style` section has already found the ones a regex can find — start there, then judge what it cannot see: uniform paragraph lengths, a closing that would fit any application, and a sentence rhythm that never varies.
- No AI fingerprints in CV/Lebenslauf/letter/Anschreiben (rule 07 section F, hard fail). The QA report's `fingerprints` array is the authoritative scan — it covers authorship signatures, assistant voice, meta-commentary, placeholders, robotic scaffolding, and model/AI-vendor names, and it already exempts Interview Prep from the vendor category only. Fix every hit it reports. Add only what a regex cannot see: a banned word is acceptable inside the employer's role title, company name, or product name, so clear those; and judge patterns of style the scan cannot catch, such as every bullet opening with the same verb.
- Visual PDF layout in every document — read the `images` the QA report lists for each document (already rendered into `_tmp/pdf_pages/<stem>/`, never into the deliverable folder; never use PyMuPDF/`fitz`): body text and CV skill values are left aligned/ragged-right, not centered or fully justified; wrapped lines have natural word spacing and uniform line gaps; no large stretched spaces like over-justified text in narrow columns.
- Cover letters in EN and DE must show visible paragraph separation/double-line style gaps between body paragraphs while still fitting on one page.
- Interview Prep PDF: proofread spelling/grammar/layout the same as any document, but it is
  private prep, not an application document — do not flag its deliberate mentions of
  unclaimed tools, gaps, or visa/relocation/logistics topics as unsupported claims or
  inflation. Also exempt from the model/AI-vendor name ban; it may name models freely.
  Authorship signatures and assistant voice are still not allowed in it.

## Fix approach

Make only minimal safe text fixes via payload/template text. If fix risks layout geometry or requires new facts, return `ESCALATED`. No DOCX.

See `rules/01-writing-style.md`, `rules/04-german-market-rules.md`, `rules/07-humanlike-anti-ai.md`.
