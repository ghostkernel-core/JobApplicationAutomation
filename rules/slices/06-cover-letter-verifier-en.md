# Slice: Cover Letter Verifier EN

## Verification criteria (PASS/FIXED/REJECTED)

1. **Integrity**: every claim traces to `rules/00-canonical-profile.md` or `/master`. No invented metrics, tools, titles, seniority, visa/permit/sponsorship/relocation.
2. **Company-specific**: P1 has a real company-specific hook from posting/research, not a template opener.
3. **Voice**: sounds like the candidate (warm, direct, sincere, lightly non-native). No AI cliches: "thrilled", "ideal candidate", "unique blend", "passionate about leveraging", "leverage synergies", em-dash crutch, triadic lists, buzzword stacking.
3b. **No AI fingerprints (hard fail — rule 07 section F)**:
   - No authorship signature or tool credit ("generated with/by", "AI-generated", "AI-assisted", "written by AI").
   - No assistant voice ("Certainly!", "I hope this helps"), no meta-commentary ("This letter highlights…", "Below is a tailored letter"), no bracketed placeholders, no prompt/rationale residue.
   - No model or AI-vendor name in any casing or spacing: Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Codex, Gemini, Bard, Gemma, DeepMind, Llama, Mistral, Mixtral, Cohere, Grok, xAI, Perplexity, Copilot, Cursor, Ollama, LM Studio, LangChain, MCP — or any other assistant/foundation-model/AI-lab brand. Generic terms (LLM, NLP, computer vision, PyTorch) pass.
   - Carve-out: a banned word passes only inside the employer's own words — exact role title in `subject`, company legal/trade name, product name. Reject it anywhere it describes the candidate, and reject a P1 hook naming a vendor brand it could have avoided.
   - No robotic scaffolding: "Key achievements include:", "In conclusion,", three paragraphs opening with "Furthermore/Moreover/Additionally", bold-lead `**Skill:** description` lines.
4. **One-page fit**: text length likely to fit locked 1-page LaTeX template. No font/margin edits. Reject or fix text likely to render with visibly uneven word gaps, centered-looking lines, or over-justified paragraph spacing in either English or German letter.
5. **No placeholders**: no empty bullets, `{{...}}`, `TODO`, stale dates, mixed companies.
6. **Payload plain text**, not LaTeX commands.
7. **Subject line**: exact role title from posting.
8. **English spelling convention**: no German umlauts or `ß`; use established English names where available or transliterate with `ae/oe/ue/ss`.
9. **PDF visual QA expectation**: final letters must be left aligned/ragged-right except the intentional sender-address block. Body, recipient, subject, salutation, closing, and enclosure must not appear centered or fully justified.
10. **Paragraph spacing**: body text must render as separate paragraphs with visible double-line style gaps, not as one cramped block. The one-page limit still applies.

## Canonical facts (for integrity check)

Read `rules/slices/_facts.md` first — the condensed canonical facts for this workspace (generated from rules/00-canonical-profile.md).

See `rules/03-cover-letter-rules.md`, `rules/05-integrity-no-fabrication.md`, `rules/07-humanlike-anti-ai.md`.
