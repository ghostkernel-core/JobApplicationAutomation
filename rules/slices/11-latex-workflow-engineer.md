# Slice: LaTeX Workflow Engineer

Use only for template/script/toolchain maintenance, never application prose.

- Templates: `master/LaTeX/templates/cv_en.tex`, `cv_de.tex`, `letter_en.tex`, `letter_de.tex`.
- Shared macros: `master/LaTeX/shared/`.
- Renderer: `scripts/render_latex_application.py`.
- Healthcheck: `scripts/latex_healthcheck.py`.
- Toolchain check: `scripts/check_latex_toolchain.py`.

Rules: small deterministic fixes, no factual content changes, no DOCX in default pipeline, keep compiler artifacts out of deliverable folders. Run local script checks after edits.

Never let a template, the renderer, or a build step emit an AI fingerprint into a deliverable: no generated-by banner, tool credit, or model/vendor name in rendered text, in LaTeX comments that survive into the PDF, in PDF metadata (`\hypersetup` author/creator/producer/subject), or in output filenames. See `rules/07-humanlike-anti-ai.md` section F.

See `rules/06-folder-and-naming.md` for deliverable folder rules.
