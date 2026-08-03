# LaTeX Master Templates

This folder is the active source for application document layout. The pipeline renders structured payloads into locked templates, compiles PDFs, and keeps DOCX out of the default workflow.

## Active files

- `templates/cv_en.tex` -> `<file_prefix> - CV.tex/.pdf`
- `templates/cv_de.tex` -> `<file_prefix> - Lebenslauf.tex/.pdf`
- `templates/letter_en.tex` -> `<file_prefix> - Cover Letter.tex/.pdf`
- `templates/letter_de.tex` -> `<file_prefix> - Anschreiben.tex/.pdf`
- `shared/common.tex` -> shared CV/Lebenslauf macros and layout.
- `fonts/` and `images/` -> assets copied into temporary build folders by the renderer.

`<file_prefix>` comes from `identity.toml` (`[person].file_prefix`). The contact block on every
document comes from `identity.toml` too, via `{{identity_*}}` placeholders in the templates.

## Legacy references

Any root-level `CV_*`/`Cover_Letter_*` files here are visual references from the original
Word-to-LaTeX migration, personal to one install and never committed. Do not use their
hard-coded text as a factual source or application master.

## Render

Use the deterministic renderer from the workspace root:

```bash
python scripts/render_latex_application.py payload.json "2026/Company/YYYY-MM-DD - Role"
python scripts/latex_healthcheck.py "2026/Company/YYYY-MM-DD - Role"
```

The renderer requires `xelatex` or `latexmk` on PATH for PDF generation, or `LATEX_ENGINE` set to the full path of the engine. MiKTeX's `latexmk` can require Perl; `xelatex` alone is enough. Check with:

```bash
python scripts/check_latex_toolchain.py
```

It writes compiler artifacts only to a temporary build directory and copies finished `.tex/.pdf` files into the target application folder.
