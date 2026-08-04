# Slice: CV Writer EN

## Canonical profile (condensed facts)

Read `rules/slices/_facts.md` first — the condensed canonical facts for this workspace (generated from rules/00-canonical-profile.md).

Page limits and the locked column widths that decide whether a skills row wraps are in `rules/slices/_toolchain.md`. Use those numbers; do not measure them from `master/LaTeX/`, and never open another application's folder or `_tmp/payloads/` to copy phrasing — only `/master` and `rules/` are sources.

## CV payload rules

- Return structured payload only (JSON or structured block), never `.tex`/PDF/DOCX.
- `headline` and `profile`: empty strings. Do not add a profile row or summary.
- Experience: real roles only. Preserve employer, title, dates, location. Tailor bullets.
- Projects: canonical projects only, ordered by relevance.
- Education: preserve master structure — dated degree `entry` with indented dash bullets for institution, thesis, focus. Do not collapse into one-line bullets.
- Skills: preserve master categories (AI, Project Management, Software Development, Technologies, Visualization and Web). Reorder/trim inside them. Do not collapse into custom rows.
- Skill labels and values must be concise enough to render without ugly wrapped-line spacing in the fixed LaTeX expertise table. Prefer shorter phrases over long comma chains in narrow skill rows.
- Languages: preserve the candidate's real languages and levels from the canonical profile, in the candidate's own order.
- Dates: `MM/YYYY`. English `Present`.
- English payload text must avoid German umlauts and `ß`; use established English names where available, otherwise transliterate `ä/ö/ü/ß` as `ae/oe/ue/ss`.
- No new personal-data fields. No invented metrics/tools/seniority.
- Layout risk: avoid long unbreakable phrases and overstuffed expertise rows that can cause visibly uneven word gaps, centered-looking text, or non-uniform wrapped-line spacing in either English CV or German Lebenslauf.

## Anti-AI style

Voice: warm, direct, sincere, modest, evidence-led. Avoid: "thrilled", "ideal candidate", "unique blend", "leverage synergies", "passionate about leveraging", em-dash crutch, triadic lists, buzzword stacking. Allow mild non-native phrasing at low density. See `rules/07-humanlike-anti-ai.md` for full checklist.

## No AI fingerprints (hard fail — rule 07 section F)

- No authorship signature or tool credit: "generated with/by", "AI-generated", "AI-assisted", "written by AI", "drafted with".
- No assistant voice ("As an AI language model", "Certainly!", "I hope this helps"), no meta-commentary about the document ("This CV highlights…", "Tailored for:", "Note:", "Draft v2"), no bracketed placeholders (`[insert X]`, `[optional]`), no prompt residue or rationale sentences.
- No model or AI-vendor name, any casing or spacing: Claude, GPT/ChatGPT (incl. GPT-4/4o/5), Sonnet, Opus, Haiku, Anthropic, OpenAI, Codex, DALL-E, Whisper, Gemini, Bard, Gemma, DeepMind, Llama, Mistral, Mixtral, Cohere, Grok, xAI, Perplexity, Copilot, Cursor, Ollama, LM Studio, LangChain, MCP / Model Context Protocol — or any other assistant/foundation-model/AI-lab brand.
- Canonical-profile entries are not exempt: render `OpenAI Gym` as "reinforcement-learning environments (Gym)" and `Ollama` as "self-hosted LLM runtime".
- Generic terms stay as-is: LLM, NLP, transformers, computer vision, CNNs, RAG, PyTorch, Scikit-learn.
- Carve-out: only the employer's own role title, legal/trade name, and product name may contain a banned word. Never use one to describe the candidate's skills, tools, or projects.
- No robotic scaffolding: "Key achievements include:", "Responsibilities:", every bullet opening with the same verb.
