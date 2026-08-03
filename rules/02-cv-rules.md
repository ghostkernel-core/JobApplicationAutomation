# CV / Lebenslauf Rules

The CV is produced from locked LaTeX templates in `master/LaTeX/templates/`, not by cloning or editing DOCX. Agents output compact structured payloads only. The orchestrator/script renders `.tex`, compiles PDFs, and runs `scripts/latex_healthcheck.py` before model QA.

## Templates

- English CV template: `master/LaTeX/templates/cv_en.tex`.
- German Lebenslauf template: `master/LaTeX/templates/cv_de.tex`.
- Shared style/macros/assets live under `master/LaTeX/shared/`.
- Template geometry, margins, fonts, colours, page style, photo/signature placement, section spacing, and skill-column alignment are locked.
- Subagents must not hand-build final `.tex` files or alter layout geometry.

## What MAY change per application

1. Bullet order and wording within existing real jobs.
2. Skills ordering inside the existing master expertise categories, putting posting-relevant real technologies first.
3. Which canonical project is emphasised first.

## What MUST NOT change

- Employers, role titles at those employers, dates, degrees, institutions, contact details, nationality, or language levels.
- Personal-data structure: do not add a headline/profile row, personal profile paragraph, summary block, or new personal-data field unless the master CV visibly uses it.
- Education structure: preserve the master degree-entry format. Each degree must render as a dated degree `entry` followed by indented dash bullets for institution, thesis, and focus; do not collapse education into one-line list bullets.
- Expertise structure: do not collapse the master expertise categories into broad custom rows; preserve the category blocks and trim/reorder supported skills within them only.
- Any metric, certification, clearance, tool, or seniority not present in `rules/00-canonical-profile.md`.
- Visa, permit, sponsorship, or relocation language.
- Template layout or font size to force content to fit.

## Length and output

- English CV and German Lebenslauf: 2 pages maximum each.
- Output names:
  - `<file_prefix> - CV.tex` and `.pdf`.
  - `<file_prefix> - Lebenslauf.tex` and `.pdf`.
  - `<file_prefix>` comes from `identity.toml` `[person].file_prefix`.
- Use `MM/YYYY` dates; English uses `Present`, German uses `aktuell`.
- English documents must avoid German umlauts and `ß`. Use established English names where available (for example a German institution whose official English name replaces "Universität" with "University"), otherwise transliterate German proper nouns with `ä -> ae`, `ö -> oe`, `ü -> ue`, `Ä -> Ae`, `Ö -> Oe`, `Ü -> Ue`, `ß -> ss`.
- Update the date line to the application date.
- All content must remain real text in the PDF, not images.

## Payload expectations

The CV writer/localizer returns a structured payload with short fields, for example:

```json
{
  "headline": "",
  "profile": "",
  "experience": [
    {"date": "09/2025 - Present", "title": "Quant Engineer - Data Science", "company": "be.storaged GmbH", "location": "Oldenburg, Germany", "bullets": ["..."]}
  ],
  "projects": [
    {"date": "03/2021 - 09/2021", "title": "IoT-Based Air Emission Monitoring and Forecasting", "bullets": ["..."]}
  ],
  "education": [
    {"date": "10/2016 - 09/2019", "degree": "M. Sc. Example Field of Study", "details": ["Example Technical University, Example City, Example Country", "Master's Thesis: Example Thesis Title on a Representative Method."]},
    {"date": "09/2011 - 02/2015", "degree": "B. Sc. Example Undergraduate Field", "details": ["Example National University, Example City, Example Country", "Focus Area: Example Focus Area"]}
  ],
  "skills": [
    {"category": "Artificial Intelligence (AI)", "items": [{"label": "Machine Learning", "value": "..."}]},
    {"category": "Project Management", "items": [{"label": "Agile Methods", "value": "..."}]},
    {"category": "Software Development", "items": [{"label": "Programming Languages", "value": "..."}]},
    {"category": "Technologies", "items": [{"label": "Platforms", "value": "..."}]},
    {"category": "Visualization and Web", "items": [{"label": "Visualization", "value": "..."}]}
  ],
  "languages": {"German": "B1+", "English": "C1+", "<native language>": "Native"},
  "date_line": "<city>, 19. June 2026"
}
```

`<city>` in `date_line` is `identity.toml` `[person].city`.

The renderer is responsible for LaTeX escaping. Agents should not add LaTeX commands except plain text values.

## Visual and deterministic gates

Before PASS:

- `scripts/render_latex_application.py` must render and compile without errors.
- `scripts/latex_healthcheck.py` must pass.
- CV/Lebenslauf page count must be <= 2.
- No unresolved placeholders such as `{{...}}`, `TODO`, `TBD`, `??`, or stale dates.
- No stale company/role text from previous applications.
- No overfull boxes severe enough to risk visible clipping.
- Skills rows must stay short enough for the locked layout and must not visually collide with values in the expertise column.

If content does not fit, shorten text. Do not shrink fonts, widen margins, or edit template geometry.
