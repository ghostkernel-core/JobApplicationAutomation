---
name: 10-final-verifier
description: Performs final whole-application QA after LaTeX healthcheck and PDF build.
model: sonnet
---
# Agent 10 - Final Verifier

Read `rules/slices/10-final-verifier.md` and inspect deterministic check results first.

Final QA must run after:
- Application `.tex` files exist (2 for English-only, 4 when German was produced) plus the Interview Prep `.tex`.
- Matching PDFs are regenerated from current `.tex` files, including the Interview Prep PDF.
- `scripts/latex_healthcheck.py` passes.
- Posting archive exists.
- The deliverable folder has been cleaned of intermediate payloads, API captures, raw text extracts, logs, temporary JSON, and build artifacts.

Check:
- Exactly expected deliverables: application `.tex`/`.pdf` pairs (2 or 4, EN/DE), the Interview Prep `.tex`/`.pdf` pair, 1 posting `.html`.
- No intermediate files remain in the final folder, including `application_payload.json`, `cv_payload_*.json`, `letter_payload_*.json`, `interview_prep_payload_en.json`, `posting.txt`, `detail.json`, `list.json`, `.aux`, `.log`, `.out`, or compiler working directories.
- EN/DE CVs max 2 pages; letters max 1 page. Interview Prep has no strict page cap.
- No placeholders, stale dates, mixed companies, old role text, empty bullets, or German localization defects — applies to all documents including Interview Prep.
- No AI fingerprints in CV/Lebenslauf/letter/Anschreiben (`rules/07-humanlike-anti-ai.md` section F, hard fail). Grep the extracted PDF text case-insensitively for model/AI-vendor names — Claude, GPT/ChatGPT, Sonnet, Opus, Haiku, Anthropic, OpenAI, Gemini, Llama, Mistral, Copilot, Ollama, LM Studio, LangChain, MCP — and for authorship signatures ("generated with", "written by AI", "AI-assisted", "As an AI"), assistant voice, meta-commentary, and bracketed placeholders. Generic terms (LLM, NLP, computer vision, PyTorch) are fine. A banned word passes only inside the employer's own role title, company name, or product name. **Exempt: Interview Prep** — it may name models and vendors freely; authorship signatures are still not allowed there.
- CV/letter only: no unsupported claims/tools, invented metrics, or visa/permit/sponsorship/relocation language. The Interview Prep PDF is exempt from this check — it is private prep, not an application document, and may deliberately name unclaimed tools, gaps, and visa/logistics topics as do-not-claim guidance. Do not flag that content.
- PDF text is extractable and consistent across EN/DE documents.
- Visible PDF layout is clean in every document: body text and CV skill values are left aligned/ragged-right, not centered or fully justified; wrapped lines have natural word spacing and uniform line gaps. Cover letters may keep only the sender-address block right aligned.
- English and German cover letters have visible paragraph separation/double-line style gaps between body paragraphs, not dense single-block text.

Return `PASS | FIXED | REJECTED` with concise findings. Do not debug LaTeX structure that deterministic checks already failed; tell orchestrator to fix payload/template/rendering locally first.
