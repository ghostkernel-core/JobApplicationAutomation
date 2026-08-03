# Slice: Posting Archiver

## Capture rules

- Use pasted posting text directly when provided.
- Try `webfetch` static capture first.
- Use `scripts/save_singlefile.sh` for self-contained HTML when static capture insufficient.
- If all automated capture fails, ask for pasted text or manual SingleFile capture. Do not use Chrome MCP.

## Folder and naming rules

Folder: `<YYYY>/<Company>/YYYY-MM-DD - <Role>/`.

Company name priority: career-site URL domain > `<title>` tag > "About us" section > specific contracting entity. Ask if ambiguous.

Role subfolder: `YYYY-MM-DD - <Role>` where `<Role>` is posting title or user's override.

Strip illegal chars: `\ / : * ? " < > |`. Keep spaces and `&`.

Save posting `.html` inside folder. Descriptive title from page; fallback `<Company> - <Role>.html`.

Return: company, role, folder path, posting path, capture caveats.
