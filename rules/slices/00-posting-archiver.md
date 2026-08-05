# Slice: Posting Archiver

## Capture rules

- Use pasted posting text directly when provided.
- Try `webfetch` static capture first.
- Use `scripts/save_singlefile.sh` for self-contained HTML when static capture insufficient.
- If all automated capture fails, ask for pasted text or manual SingleFile capture. Do not use Chrome MCP.

## Folder and naming rules

Folder: `<YYYY>/<Company>/YYYY-MM-DD - <Role>/`.

Create it with the scaffold script, never with `mkdir` and a hand-built path:

```
python scripts/scaffold.py "<Company>" "<Role>"     # [--date YYYY-MM-DD], default today
```

It prints the absolute folder path on stdout — that string is the folder path you pass
on and report, so capture it rather than reassembling one from the parts. Building the
path in the shell instead is how a run once landed in `2026\kausable GmbH${TODAY} - ML
Product Engineer`: inside double quotes `\$` is an escaped dollar, so the separator was
eaten and the date never expanded, `mkdir -p` succeeded on the mangled name, and the
whole application was written somewhere nobody could find. The guard hook now blocks
that shape outright.

Company name priority: career-site URL domain > `<title>` tag > "About us" section > specific contracting entity. Ask if ambiguous.

Role subfolder: `YYYY-MM-DD - <Role>` where `<Role>` is posting title or user's override.

The script strips the illegal chars for you (`\ / : * ? " < > |`, keeping spaces and `&`),
so pass the company and role as they read and let it do the cleaning.

Save posting `.html` inside folder. Descriptive title from page; fallback `<Company> - <Role>.html`.

Return: company, role, folder path, posting path, capture caveats.
