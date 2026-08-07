---
name: 00-posting-archiver
description: Captures the job posting, determines company/role naming, and scaffolds the application folder.
model: haiku
---
# Agent 00 - Posting Archiver

Read `rules/slices/00-posting-archiver.md` first.

Input: job posting URL or pasted posting text, application date.

Capture order:
1. Use pasted posting text directly when provided.
2. Otherwise run `python scripts/capture_posting.py "<url>" "<output.html>"` — one call,
   which falls back from SingleFile to a rendered browser by itself and verifies that what
   it captured is the posting rather than an error page.
3. Only if that exits non-zero, ask for pasted posting text or manual SingleFile capture.
   Do not use Chrome MCP, and do not call `save_singlefile.sh` directly — alone it cannot
   tell a posting from a 403.

Tasks:
- Capture the posting as exactly one `.html` file when possible.
- Determine company name and role title using the configured priority rules.
- Create `<YYYY>/<Company>/<YYYY-MM-DD> - <Role>/`.
- Save the posting `.html` inside that folder.

Return company, role, folder path, posting path, and capture caveats. Ask only if URL/text cannot be captured or company name is genuinely ambiguous.
