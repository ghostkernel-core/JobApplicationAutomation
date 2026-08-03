# Slice: Interview Prep

No application rule context needed. Use the Match Brief, Research Note, and final CV/letter
angle provided by the orchestrator to write a private prep payload. Output:
`interview_prep_payload_en.json`.

Payload fields: `role_title`, `folder_note`, `date_line`, `pitch` (string or list of
paragraphs), `pitch_rationale` (list), `context_notes` (list), `themes` (list of
`{name, body?, items?}`), `examples` (list of `{title, body?, use_for?}`), `gaps` (list of
`{label, body}`), `questions` (list), `logistics_notes` (list).

Include: positioning/pitch, likely interview themes, strong examples from real profile, gaps
to handle honestly, questions to ask. This is private — may mention unsupported tools as "do
not claim" guidance, and may discuss visa/relocation/logistics topics freely. None of the
CV/letter integrity restrictions (no-tool-names, no-visa-language) apply to this payload —
the render/healthcheck pipeline and downstream QA agents exempt it from those checks. It is
also exempt from the model/AI-vendor name ban in rule 07 section F, so it may name models
and vendors plainly, including to warn which ones must not appear on the CV or in the letter.

Section F still applies in one respect: no authorship signatures ("generated with",
"AI-generated"), no assistant voice ("Certainly!", "I hope this helps"), and no
meta-commentary about the document itself.

Rendering to `.tex`/PDF happens deterministically via `scripts/render_latex_application.py`
(same as CV/letter) — write structured JSON only, not LaTeX or Markdown prose.
