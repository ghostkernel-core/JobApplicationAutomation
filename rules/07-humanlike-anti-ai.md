# Rule 07 — Human-Like / Anti-AI-Detection Checklist

Used by the CV verifier (03), cover-letter verifier (05), and final verifier (08). The
output must read as written by the candidate, not by a model. Two failure modes to catch:
(a) sounding like generic AI, (b) sounding like someone other than the candidate.

## A. Kill the AI tells
Scan and remove / rewrite:
- Opening clichés: "I am thrilled/delighted/excited to express my keen interest", "I am
  writing to express my strong interest".
- "ideal candidate", "unique blend", "proven track record" (used as filler), "I am
  passionate about leveraging", "synergy", "spearheaded", "tapestry", "testament to".
- Triadic everything ("X, Y, and Z" in sentence after sentence). Vary list lengths.
- Em-dash overuse `—`. The candidate rarely uses them; prefer commas / "and" / full stops.
- Uniform paragraph lengths and identical sentence cadence. Real writing varies.
- Perfectly balanced "Not only… but also…" constructions in every letter.
- Hedging filler: "In today's fast-paced world", "In the ever-evolving landscape of".
- A closing that could be pasted into any application. The close should reference the role/company.

## B. Sound like the candidate (see rules/01-writing-style.md)
- Allow mild non-native phrasing at low density — do not native-ise it away.
- Warm, sincere, sometimes a longer run-on joined by "and/which". Keep some.
- Concrete project nouns and real tech stack, not abstractions.
- One genuine company-specific detail in paragraph 1.
- British/US spelling may mix between documents but stay consistent within one document.

## C. Cross-document consistency (per application)
- Name spelling, address, phone, email identical across CV, Lebenslauf, letter, Anschreiben.
- Role title in the letter subject == posting title; CV role focus comes from bullet/skills/project emphasis, not from adding a new headline field.
- Same date (today) on all four documents.
- EN and DE versions state the same facts (no claim in one that's missing/contradicted in the other).
- Dates/employers match the canonical profile exactly.

## D. Integrity gate (hard fail)
- Any metric/skill/title not supported by `rules/00-canonical-profile.md` → REJECT, send
  back to the writer agent. (See `rules/05-integrity-no-fabrication.md`.)

## E. Mechanical
- Spell-check EN and DE. German: correct umlauts (ä ö ü ß), capitalised nouns, "Sie" form.
- One page for each letter; ~2 pages for each CV.
- PDF renders correctly (open it / convert and check page count, no overflow, no broken table).

## F. No AI fingerprints (hard fail)
Application documents (CV, Lebenslauf, cover letter, Anschreiben) must carry no trace of how
they were produced. Any hit here is a hard fail, not a style note.

### F1. No authorship signatures
Never emit — in body text, headers, footers, LaTeX comments, PDF metadata, or filenames:
- "generated with/by", "written by AI", "AI-generated", "AI-assisted", "drafted with",
  "created using", or any tool/credit line.
- Assistant voice: "As an AI language model", "Certainly!", "Here is the letter you
  requested", "I hope this helps", "Let me know if you would like me to…".
- Meta-commentary about the document: "This cover letter highlights…", "Below is a tailored
  CV…", "Note:", "Draft v2", "Tailored for: …".
- Instruction residue: prompt fragments, bracketed placeholders (`[insert X]`, `[optional]`),
  or rationale sentences explaining why a bullet was chosen.

### F2. No model or AI-vendor names (absolute)
These must not appear anywhere in an application document — any casing, spacing, or
hyphenation, and not buried inside a longer phrase:

Anthropic, Claude, Sonnet, Opus, Haiku (as a model name), OpenAI, ChatGPT, GPT (incl.
GPT-3/3.5/4/4o/5), Codex, DALL-E, Whisper, o1/o3, Gemini, Bard, Gemma, DeepMind, Llama,
Mistral, Mixtral, Cohere, Grok, xAI, Perplexity, Copilot, Cursor, Ollama, LM Studio,
LangChain, MCP / Model Context Protocol — plus any other chat-assistant, foundation-model,
or AI-lab brand or model-family name not listed here.

Rewrite vendor-neutrally instead. Canonical-profile entries included:
- `OpenAI Gym` → "reinforcement-learning environments (Gym)"
- `Ollama`, LM Studio → "self-hosted LLM runtime", "local LLM serving"
- Claude / GPT / Gemini / Llama → "LLM", "large language models"
- Claude + MCP agent work → "multi-agent LLM orchestration", "tool-calling LLM agents"

Generic technique and library names are unaffected and stay as they are: LLM, NLP,
transformers, PyTorch, Scikit-learn, computer vision, CNNs, RAG, prompt engineering.

### F3. Narrow carve-out — the employer's own proper nouns
Words the employer owns are quoted, not claimed, and stay verbatim:
- the official role title in the letter subject line (e.g. a posting titled "GPT Engineer"),
- the company's legal or trade name,
- the company's own product name.

Nothing describing the candidate — their skills, tools, projects, or experience — may use a banned
name under this carve-out. If a company hook works without the brand, write it without.

### F4. Robotic scaffolding
Reject mechanical structure that reads as machine output: "Key achievements include:",
"Responsibilities:", "In conclusion,", "Furthermore,/Moreover,/Additionally," opening three
paragraphs in a row, "As mentioned above", every bullet starting with the same verb, and
bold-lead `**Skill:** description` patterns inside letter prose.

### F5. Interview Prep exemption
Interview Prep is private and never sent — it is exempt from F2 and F3, and may name models
and vendors freely as do-not-claim guidance or prep context. F1 and F4 still apply to it.

## Pass criteria
A document passes only when A–F are all clean. If the verifier rewrites anything, it must
re-run A–C and F on the new text. Record a one-line verdict per document in the run summary.
