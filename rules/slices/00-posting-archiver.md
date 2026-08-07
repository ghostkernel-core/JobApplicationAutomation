# Slice: Posting Archiver

## Capture rules

- Use pasted posting text directly when provided.
- Otherwise capture the URL with one call, and do not hand-roll the fallbacks:

```
python scripts/capture_posting.py "<url>" "<folder>/<Company> - <Role>.html"
```

  It tries SingleFile, checks that what came back is actually the posting, and
  re-renders the page in a real browser when it is not. Exit 0 means a usable
  archive is on disk; the `NOTE:` line, when present, says which method was
  rejected and why. That is a caveat to report, not a failure.

- Do not call `scripts/save_singlefile.sh` directly. On its own it cannot tell a
  posting from an error page: `careers.axa.com` answered a headless browser with
  403, SingleFile wrote those 640 bytes out with exit code 0, and the run that
  trusted it stopped to ask for text it already had a way to get.
- `webfetch` is for reading a page, not archiving one — it does not run
  JavaScript, so on a site that renders its body client-side it returns the
  title and the nav and nothing else. Use it to confirm company and role if you
  like; never treat its output as the capture.
- Only if `capture_posting.py` exits non-zero: ask for pasted text or a manual
  SingleFile capture. Do not use Chrome MCP.

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
