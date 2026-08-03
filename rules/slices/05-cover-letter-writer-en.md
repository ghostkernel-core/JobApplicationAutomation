# Slice: Cover Letter Writer EN

## Canonical profile (condensed facts)

Read `rules/slices/_facts.md` first — the condensed canonical facts for this workspace (generated from rules/00-canonical-profile.md).

## Letter payload structure

- `date`, `recipient` (array), `subject` (exact role title), `salutation`, `paragraphs` (3-4), `bullets` (0-3 optional), `closing`, `enclosure`.
- Return structured payload only, never `.tex`/PDF/DOCX.

## Content rules

- P1 must include one real company-specific detail from posting/research.
- Voice: warm, direct, sincere, modest, evidence-led. Allow mild non-native phrasing.
- No AI cliches: "thrilled", "ideal candidate", "unique blend", "passionate about leveraging", "leverage synergies", em-dash crutch, triadic lists.
- **No AI fingerprints (hard fail — rule 07 section F):**
  - No authorship signature or tool credit: "generated with/by", "AI-generated", "AI-assisted", "written by AI", "drafted with".
  - No assistant voice ("Certainly!", "I hope this helps", "Let me know if you would like me to…"), no meta-commentary ("This letter highlights…", "Below is a tailored letter", "Note:", "Draft v2"), no bracketed placeholders, no prompt residue.
  - No model or AI-vendor name, any casing or spacing: Claude, GPT/ChatGPT (incl. GPT-4/4o/5), Sonnet, Opus, Haiku, Anthropic, OpenAI, Codex, DALL-E, Whisper, Gemini, Bard, Gemma, DeepMind, Llama, Mistral, Mixtral, Cohere, Grok, xAI, Perplexity, Copilot, Cursor, Ollama, LM Studio, LangChain, MCP / Model Context Protocol — or any other assistant/foundation-model/AI-lab brand.
  - Write LLM work vendor-neutrally: "multi-agent LLM orchestration", "tool-calling LLM agents", "self-hosted LLM runtime". Generic terms stay as-is: LLM, NLP, computer vision, PyTorch.
  - Carve-out: banned words are allowed only inside the employer's own words — the exact role title in `subject`, the company's legal/trade name, its product name. Never to describe the candidate. If the P1 hook works without the brand, write it without.
  - No robotic scaffolding: "Key achievements include:", "In conclusion,", three paragraphs opening with "Furthermore/Moreover/Additionally", bold-lead `**Skill:** description` lines.
- No invented metrics, unsupported tools, visa/permit/sponsorship/relocation language.
- P1 references this specific company — no template openers.
- British/US spelling okay, consistent per document.
- Total length must fit 1 LaTeX page.
- Paragraphs must be concise enough to render left aligned/ragged-right without over-justified spacing or centered-looking lines in English or German letters.
- Use clear paragraph separation: 3-4 body paragraphs as separate payload entries so LaTeX renders visible double-line style gaps between paragraphs. Do not merge the letter body into one dense block.
- Payload values are plain text, not LaTeX commands.
- English output contains no German umlauts or `ß`; use established English names where available or transliterate German names with `ae/oe/ue/ss`.

See `rules/01-writing-style.md`, `rules/03-cover-letter-rules.md`, `rules/05-integrity-no-fabrication.md`, `rules/07-humanlike-anti-ai.md` for full detail.
