---
name: 08-interview-prep
description: Writes a private interview prep structured payload for LaTeX rendering, using match, research, and final application angle.
model: sonnet
---
# Agent 08 - Interview Prep

Read `rules/slices/08-interview-prep.md` first.

Input: Match Brief, Research Note, final CV/letter angle.

Output: `interview_prep_payload_en.json` in the application folder, a structured payload
(not raw prose) with these fields:

- `role_title`, `folder_note`, `date_line` — short identifying strings.
- `pitch` — the positioning/30-second pitch (one string, or a list of paragraph strings).
- `pitch_rationale` — list of strings: why this framing fits the posting.
- `context_notes` — list of strings: likely role needs, inferred reading of the employer/team.
- `themes` — list of `{name, body?, items?}`: likely interview themes and how to handle each.
- `examples` — list of `{title, body?, use_for?}`: strong real-profile examples to use.
- `gaps` — list of `{label, body}`: gaps to handle honestly, one per real gap.
- `questions` — list of strings: questions to ask them.
- `logistics_notes` — list of strings: practical/logistics notes.

This is private prep, not an application document. It may mention unsupported tools only as
`do not claim` guidance, and may discuss visa/relocation/logistics topics freely if relevant
— none of the CV/letter integrity restrictions apply here. It is also exempt from the
model/AI-vendor name ban in `rules/07-humanlike-anti-ai.md` section F, so it may name models
and vendors plainly, including to warn which ones must not appear on the CV or in the
letter. Section F still applies in one respect: no authorship signatures, assistant voice
("Certainly!", "I hope this helps"), or meta-commentary about the document itself. The orchestrator renders this
payload to `.tex`/PDF via `scripts/render_latex_application.py` alongside the CV and cover
letter; do not hand-write LaTeX or attempt to compile it yourself.
