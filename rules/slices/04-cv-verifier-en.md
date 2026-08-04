# Slice: CV Verifier EN

## Canonical profile (condensed facts)

Read `rules/slices/_facts.md` first — the condensed canonical facts for this workspace (generated from rules/00-canonical-profile.md).

Page limits and the locked column widths that decide whether a skills row wraps are in `rules/slices/_toolchain.md`. Use those numbers; do not measure them from `master/LaTeX/`, and never open another application's folder or `_tmp/payloads/` to compare against — only `/master` and `rules/` are sources.

## Verification criteria (PASS/FIXED/REJECTED)

1. **Integrity**: every claim traces to `rules/00-canonical-profile.md` or `/master`. No invented metrics, tools, titles, dates, degrees, certifications, clearances, seniority, visa/permit/sponsorship/relocation language. No doctorate unless posting required it.
2. **Structure**: no added headline/profile row, no personal summary paragraph, no collapsed custom expertise rows. Education has dated degree entries with indented dash bullets — not one-line bullets.
3. **Voice**: human, specific, modest. No AI cliches (thrilled, ideal candidate, unique blend, leverage, synergy, em-dashes, triadic lists).
3b. **No AI fingerprints (hard fail — rule 07 section F)**:
   - No authorship signature or tool credit ("generated with/by", "AI-generated", "AI-assisted", "written by AI").
   - No assistant voice, meta-commentary about the document, bracketed placeholders, or prompt/rationale residue.
   - No model or AI-vendor name in any casing or spacing: Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Codex, Gemini, Bard, Gemma, DeepMind, Llama, Mistral, Mixtral, Cohere, Grok, xAI, Perplexity, Copilot, Cursor, Ollama, LM Studio, LangChain, MCP — or any other assistant/foundation-model/AI-lab brand.
   - Canonical entries are not exempt: fix `OpenAI Gym` → "reinforcement-learning environments (Gym)", `Ollama` → "self-hosted LLM runtime". Generic terms (LLM, NLP, transformers, computer vision, PyTorch) pass.
   - Carve-out: a banned word passes only inside the employer's role title, company name, or product name — never in a line describing the candidate's skills.
   - No robotic scaffolding: "Key achievements include:", "Responsibilities:", every bullet opening with the same verb.
4. **Layout fit**: bullets and skill rows short enough for locked 2-page LaTeX layout. No font shrinking or margin edits. Reject or fix payloads likely to create visibly uneven word gaps, centered-looking text, over-justified wrapped lines, or non-uniform line spacing in the CV/Lebenslauf expertise section.
5. **Canonical dates/employers/titles preserved**.
6. **Payload plain text**, not LaTeX commands.
7. **English spelling convention**: no German umlauts or `ß`; use established English names where available or transliterate with `ae/oe/ue/ss`.
8. **PDF visual QA expectation**: after rendering, the CV and Lebenslauf must be left aligned/ragged-right in paragraph and skill-value text. Wrapped lines must have natural word spacing; no stretched spaces inside skill rows.

## Allowed tailoring

Reordering/rephrasing real bullets, choosing relevant real projects/skills, truthful role emphasis. No new facts.

See `rules/02-cv-rules.md`, `rules/05-integrity-no-fabrication.md`, `rules/07-humanlike-anti-ai.md` for full detail.
