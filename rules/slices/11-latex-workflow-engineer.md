# Slice: LaTeX Workflow Engineer

Use only for template/script/toolchain maintenance, never application prose.

Every other agent is barred from reading script and template source; they work from
`rules/slices/_toolchain.md` instead. You are the exception, and you own that file: if
you change a CLI signature, a payload key, a page limit, or a locked layout constant,
update `_toolchain.md` in the same run. A stale contract card sends every other agent
back to guessing, which is what it exists to stop.

- Templates: `master/LaTeX/templates/cv_en.tex`, `cv_de.tex`, `letter_en.tex`, `letter_de.tex`.
- Shared macros: `master/LaTeX/shared/`.
- Renderer: `scripts/render_latex_application.py`.
- Healthcheck: `scripts/latex_healthcheck.py`.
- Toolchain check: `scripts/check_latex_toolchain.py`.

Rules: small deterministic fixes, no factual content changes, no DOCX in default pipeline, keep compiler artifacts out of deliverable folders. Run local script checks after edits.

Never let a template, the renderer, or a build step emit an AI fingerprint into a deliverable: no generated-by banner, tool credit, or model/vendor name in rendered text, in LaTeX comments that survive into the PDF, in PDF metadata (`\hypersetup` author/creator/producer/subject), or in output filenames. See `rules/07-humanlike-anti-ai.md` section F.

See `rules/06-folder-and-naming.md` for deliverable folder rules.
