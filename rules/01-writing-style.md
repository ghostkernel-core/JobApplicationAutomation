# Rule 01 — Writing Style & Voice ("write like me")

Goal: every cover letter (and the CV's prose lines) must read like **the candidate wrote it
themselves** — a competent non-native English writer who is warm, sincere, specific, and a
little informal. It must NOT read like polished AI copy.

This profile was reverse-engineered from the candidate's real letters (buynomics, Otto, Zoi,
adjoe, understand.ai) and the master documents. Match it.

## Voice in one line
Earnest, motivated engineer who has clearly read about the company, explains their fit
plainly, and is honest about being early-career — confident but not slick.

## Structural habits (cover letters)
1. Sender block: name, street, ZIP city, phone, email (the candidate's real details from `identity.toml`).
2. Company block: company name, (contact person if known), street, ZIP city.
3. `<city>, <date>` line, where `<city>` is `identity.toml` `[person].city`.
4. Subject line: **"Application for <exact role title>"**.
5. Salutation: `Dear <Name>,` if a person is known, else `Dear Hiring Manager,` or `Dear <Company> Team,`.
6. 3–4 body paragraphs (see `rules/03-cover-letter-rules.md`).
7. Close: `Best regards,` (or occasionally `Your Sincerely,`) + full name.
8. `Enclosure: Curriculum Vitae`.

## Sentence & tone signatures (reproduce these)
- First person, active, direct: "I am excited to apply for…", "In my current role as…", "I look forward to…".
- Names the company's mission/product **specifically** and ties it to the candidate's values: "I am drawn to your mission to…", "Your emphasis on … resonates with my values."
- Connects the candidate's concrete projects to their needs, listing the real tech stack (Python, Docker, CI/CD, AWS, Kafka…).
- Occasional long, slightly run-on sentences joined by "and"/"which"/"as well as". Don't over-correct these — they're part of the voice.
- Mild non-native touches are AUTHENTIC and should remain at low density: e.g. "I am confident that I am well fitted for this role", "to develop online adaptive machine learning algorithms", "customer trusts and internal gains". Keep grammar clear, but do **not** sand it into perfect native idiom.
- Honest, humble notes when relevant: the candidate has openly written about switching from academia to industry and "lots of new opportunities to learn". Sincerity > bravado.
- Light personal/human moments are on-brand (e.g. the Zoi P.S. pointing out a typo in their posting). Use sparingly and only when genuine.

## Hard "do not" list (these are AI tells — avoid)
- ❌ "I am thrilled/delighted to express my keen interest"
- ❌ "I believe my unique blend of skills makes me the ideal candidate"
- ❌ "leverage my synergies", "passionate about leveraging cutting-edge"
- ❌ Em-dashes used as a stylistic crutch ( — ). The candidate uses commas, "and", and full stops.
- ❌ Three-item parallel triads in every sentence ("collaboration, innovation, and excellence").
- ❌ Identical opener/closer across companies. Each letter's first paragraph must reference *this* company specifically.
- ❌ Over-formatting: no bullet-point cover letters unless listing a few concrete projects (the candidate does this once, mid-letter, like the buynomics letter).
- ❌ Buzzword stacking with no substance. Every claim ties to a real project/skill from `rules/00-canonical-profile.md`.
- ❌ Any trace of how the document was produced: "generated with/by", "AI-generated", "AI-assisted", assistant voice ("Certainly!", "I hope this helps"), meta-commentary ("This letter highlights…"), or bracketed placeholders.
- ❌ Any model or AI-vendor name — Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Gemini, Llama, Mistral, Copilot, Ollama, LangChain, MCP, and the like. Write LLM work vendor-neutrally. Generic terms (LLM, NLP, computer vision, PyTorch) are fine. Full list and the employer-proper-noun carve-out: `rules/07-humanlike-anti-ai.md` section F.
- ❌ Robotic scaffolding: "Key achievements include:", "Responsibilities:", "In conclusion,", or three paragraphs in a row opening with "Furthermore/Moreover/Additionally".

## Punctuation / spelling conventions
- British-leaning spelling is fine ("optimise", "specialising") — the candidate mixes US/UK; keep it consistent **within one document**.
- Dates in letters: `<city>, 30. October 2024` style or `<city>, October 29, 2024`, where `<city>` is `identity.toml` `[person].city`. Pick one per document.
- Phone shown exactly as `identity.toml` `[person].phone` (the spaced international form), or with the spaces removed in tight headers.

## Calibration test (run mentally before finishing)
- Would a hiring manager believe a real person wrote this in ~30 minutes after reading the posting? 
- Does paragraph 1 prove the candidate researched *this* company (not a template)?
- Are all skills/claims traceable to the canonical profile?
- Did I remove every phrase from the "do not" list?
If any answer is no, revise. See `rules/07-humanlike-anti-ai.md` for the full checklist.
