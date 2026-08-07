# Slice: Final Verifier

Script interfaces and layout constants are in `rules/slices/_toolchain.md`. Do not read
script or template source to work out an argument — and never open another application's
folder or `_tmp/payloads/` for reference. The Match Brief and Research Note are not in
the deliverable folder by this point: `clean_deliverable.py` moves them to
`_tmp/payloads/`. Do not go looking for them.

## Start here

Clean the folder, then run the one-pass QA:

```
python scripts/clean_deliverable.py --folder "<folder>"
python scripts/qa_application.py "<folder>" --require-prep --json
```

The QA report already contains the healthcheck result, page counts against the limits,
the full AI-fingerprint scan, each PDF's metadata, the folder inventory, and rasterised
page images. Its findings are deterministic — a reported hit is a defect, not a judgement
to re-check. Do not re-run `latex_healthcheck.py`, re-grep extracted text, or call
`pdf_to_images.py` yourself. Read the report, then look at the page images it lists.

The report's `style` and `ats` sections are measurements, not gates: neither can change
the QA verdict and neither should change yours. `style` belongs to the proofreader (step
08), who has already worked it; `ats` is keyword coverage of the CV, useful context and
never a reason to reject a truthful document or to add a word to it.

If the run reports that Interview Prep was still rendering, verify the application
documents alone and say so in the verdict. A complete application without the study aid
passes; drop `--require-prep` in that case.

## Check list

1. **Deliverables**: application `.tex`+`.pdf` pairs (2 or 4) + Interview Prep `.tex`+`.pdf` + posting `.html`.
2. **Folder cleanliness**: all intermediate payloads, API captures, raw text extracts, logs, temporary JSON, and build artifacts must be out of the deliverable folder before PASS. Run `python scripts/clean_deliverable.py --folder "<folder>"` — that is the sanctioned way, and `rm` is blocked by the headless guard. It reports anything it has no rule for under "left in place"; judge those yourself. The final folder must not contain `application_payload.json`, `cv_payload_*.json`, `letter_payload_*.json`, `interview_prep_payload_en.json`, `posting.txt`, `detail.json`, `list.json`, `.aux`, `.log`, `.out`, or compiler working directories.
3. **Page limits**: CV/Lebenslauf ≤2 pages; letters ≤1 page each. Interview Prep has no strict page cap (reference material, not judged on length).
4. **No defects**: no placeholders (`{{...}}`, `TODO`, `TBD`, `??`), stale dates, mixed company names, old role text, empty bullets — applies to all documents including Interview Prep.
5. **Integrity (CV/letter only)**: no unsupported claims/tools, invented metrics, visa/permit/sponsorship/relocation language, doctorate unless required. All facts trace to `rules/00-canonical-profile.md`. **Exempt: Interview Prep** — it is private prep, not an application document, and is expected to name unclaimed tools, gaps, and visa/logistics topics deliberately as do-not-claim guidance. Do not flag that content.
5b. **No AI fingerprints (CV/Lebenslauf/letter/Anschreiben — rule 07 section F, hard fail)**: the QA report's `fingerprints` array is the authoritative scan, and it already exempts Interview Prep from the vendor category alone (authorship signatures are still banned there). Every hit it reports is a defect. PDF metadata is now in the report too — each document's `metadata` object (`/Producer`, `/Creator`, `/Author`, `/Title`, `/Subject`, `/Keywords`) is scanned for authorship signatures, assistant voice, and vendor names, and any hit appears in `fingerprints` with `"field": "metadata"`. So read the field rather than re-deriving it. Add only the two things a regex cannot settle: clear a banned word that sits inside the employer's own role title, company name, or product name, and confirm no such string sits in a filename.
6. **German quality**: correct umlauts (ä ö ü ß), no ASCII transliterations, `Sie` form, `Mit freundlichen Grüßen`.
7. **Cross-document consistency**: same facts in EN/DE, same name/address/phone/email, same date on all docs.
8. **PDF quality**: text extractable, consistent across EN/DE documents. Visually inspect all PDFs by reading the `images` the QA report lists per document (already rendered into `_tmp/pdf_pages/<stem>/`, never into the deliverable folder; never use PyMuPDF/`fitz`) — check for left alignment/ragged-right body text, natural word spacing, uniform wrapped-line gaps, and no centered-looking or over-justified paragraphs. Cover letters may keep the sender-address block right aligned; all other letter text must be left aligned.
9. **Letter spacing**: English and German cover letters must have visible paragraph separation/double-line style gaps between body paragraphs, not dense single-block text.

## PASS / FIXED / REJECTED

Return verdict with concise findings. If LaTeX/PDF defects survive healthcheck, tell orchestrator to fix locally — do not debug LaTeX structure here.

See `rules/05-integrity-no-fabrication.md`, `rules/06-folder-and-naming.md`, `rules/07-humanlike-anti-ai.md`, `rules/04-german-market-rules.md`.
