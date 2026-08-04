# Slice: German Translator / Localizer

Page limits, payload keys, and the locked layout constants are in
`rules/slices/_toolchain.md`. Use those numbers rather than measuring them from
`master/LaTeX/`, and never open another application's folder or `_tmp/payloads/` — only
`/master` and `rules/` are sources.

## German document rules

- Lebenslauf: tabellarisch, reverse-chronological, max 2 pages. Sections: PERSÖNLICHE DATEN, BERUFSERFAHRUNG, PROJEKTE, AUSBILDUNG, TECHNOLOGIE-STACKS, SPRACHKENNTNISSE.
- Anschreiben: DIN-formal, 1 page max. `Sie` form. `Sehr geehrte Frau <Name> / Sehr geehrter Herr <Name> / Sehr geehrte Damen und Herren`. Bold subject without `Betreff:`. Closing: `Mit freundlichen Grüßen`. No DOCX.
- Use proper German umlauts (ä ö ü ß). Never ASCII transliterations (Nationalitaet, fuer, ueber, Gruessen, Strasse, Duesseldorf).
- Dates: `MM/YYYY`. Use `aktuell` for Present. Date line: `<city>, 19. Juni 2026` style, where `<city>` is `identity.toml` `[person].city`.
- German level: B1+. Do not claim B2/C1/native/verhandlungssicher.
- Keep proper nouns, company names, technology names unchanged — except banned AI-vendor names, which must never be reintroduced during localization.
- **No AI fingerprints (hard fail — rule 07 section F):** carry the English payload's vendor-neutral wording into German. No model or AI-vendor name in any casing: Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Gemini, Llama, Mistral, Copilot, Ollama, LM Studio, LangChain, MCP, or any other assistant/foundation-model brand. German equivalents stay vendor-neutral: `selbst gehostete LLM-Laufzeit`, `Multi-Agenten-LLM-Orchestrierung`, `Reinforcement-Learning-Umgebungen (Gym)`. Generic terms (LLM, NLP, Computer Vision, PyTorch) unchanged. Only the employer's official role title, company name, and product name may keep a banned word. No authorship signature, assistant voice, meta-commentary, or bracketed placeholder in either German payload.
- `(m/w/d)` in role titles when quoting German postings.
- No salary/availability unless posting requests it.

## Payload structure rules

- Return compact `cv_payload_de` and `letter_payload_de` only — never `.tex`/PDF/DOCX.
- Preserve CV structure from verified English payload: no added Profil/headline row, no personal summary, no collapsed custom expertise rows.
- Preserve education structure: dated degree entries with indented dash bullets for institution, thesis/focus. Not one-line bullets.
- Keep German skill rows and letter paragraphs concise enough to avoid stretched word gaps, centered-looking text, or non-uniform wrapped-line spacing after LaTeX rendering.
- Plain text payload values, not LaTeX commands.

## Integrity

Same facts as English. No invented metrics/tools/seniority. No visa/permit/sponsorship. No doctorate by default.

See `rules/04-german-market-rules.md`, `rules/05-integrity-no-fabrication.md`.
