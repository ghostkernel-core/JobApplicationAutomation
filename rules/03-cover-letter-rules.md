# Cover Letter / Anschreiben Rules

Cover letters are produced from locked LaTeX templates in `master/LaTeX/templates/`, not by cloning or editing DOCX. Agents output compact structured payloads only. The deterministic renderer creates `.tex`, compiles PDFs, and runs `scripts/latex_healthcheck.py` before model QA.

## Templates

- English template: `master/LaTeX/templates/letter_en.tex`.
- German template: `master/LaTeX/templates/letter_de.tex`.
- Shared style/macros/assets live under `master/LaTeX/shared/`.
- Template margins, font, sender block, recipient block, subject placement, signature, and spacing are locked.
- Subagents must not hand-build final `.tex` files or change layout geometry.

## Length and output

- English cover letter and German Anschreiben: 1 page maximum each.
- Target length: roughly 300-400 words for English; concise German equivalent.
- Output names:
  - `<file_prefix> - Cover Letter.tex` and `.pdf`.
  - `<file_prefix> - Anschreiben.tex` and `.pdf`.
  - `<file_prefix>` comes from `identity.toml` `[person].file_prefix`.
- If content overflows, cut text. Do not shrink fonts, change margins, or reduce spacing.

## Required English structure

- English documents must avoid German umlauts and `ß`. Use established English names where available, otherwise transliterate German proper nouns with `ä -> ae`, `ö -> oe`, `ü -> ue`, `Ä -> Ae`, `Ö -> Oe`, `Ü -> Ue`, `ß -> ss`.

1. Sender block: the candidate's contact details from `identity.toml`.
2. Recipient block: company, contact person if reliable, address if known.
3. Date line: `<city>, <application date>`, where `<city>` is `identity.toml` `[person].city`.
4. Subject: `Application for <exact role title from posting>`.
5. Salutation: `Dear <Name>,` or `Dear Hiring Manager,`.
6. Body: 3-4 paragraphs.
7. Optional 2-3 short bullets only when they improve relevance and fit one page.
8. Sign-off: `Best regards,` + full name.
9. Enclosure: `Curriculum Vitae` if included by the template/payload.

## Required German structure

- Formal DIN-style Anschreiben, using `Sie`, never `du`.
- Subject line without `Betreff:` label, e.g. `Bewerbung als <Rolle>`.
- Salutation: `Sehr geehrte Frau <Name>,`, `Sehr geehrter Herr <Name>,`, or `Sehr geehrte Damen und Herren,`.
- Closing: `Mit freundlichen Grüßen`. XeLaTeX templates support Unicode; do not use ASCII transliteration such as `Gruessen`.
- Enclosure: `Anlagen: Lebenslauf`.

## Content rules

- Every factual claim must trace to `rules/00-canonical-profile.md` or `/master`.
- No invented metrics, unsupported tools, inflated seniority, certifications, visa/permit/sponsorship/relocation language.
- P1 must include one company-specific detail from the posting or research note.
- Match the posting's role title exactly in the subject line.
- Voice: warm, direct, sincere, lightly non-native, and evidence-led. Avoid AI cliches.
- German/EU tone: concrete, modest, technically credible, not exaggerated.

## Payload expectations

The writer/localizer returns structured text, for example:

```json
{
  "date": "<city>, 19. June 2026",
  "recipient": ["Hiring Manager", "Company GmbH", "Street 1", "12345 City"],
  "subject": "Application for Data Scientist",
  "salutation": "Dear Hiring Manager,",
  "paragraphs": ["...", "...", "..."],
  "bullets": ["optional short bullet"],
  "closing": "Best regards,",
  "enclosure": "Curriculum Vitae"
}
```

The renderer handles LaTeX escaping. Agents should return plain text, not LaTeX commands.

## Deterministic gates

Before PASS:

- Render and compile must succeed.
- Page count must be 1.
- No unresolved placeholders, empty bullets, stale dates, mixed company names, or old role titles.
- No language-level mismatch; German remains B1+.
- PDF text extraction must show the expected company, role, salutation, and date.
