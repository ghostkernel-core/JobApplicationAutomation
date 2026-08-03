# Getting Started

This is a linear checklist. Follow it in order; each step assumes the previous one is done.

## 1. Prerequisites

- **Python 3.11+** (the config loaders use `tomllib`).
- **[Claude Code](https://claude.com/claude-code)** CLI on `PATH` — install with
  `npm install -g @anthropic-ai/claude-code`.
- **A LaTeX toolchain**: `xelatex` or `latexmk` on `PATH`, or the `LATEX_ENGINE` environment
  variable pointing at the binary. MiKTeX and TeX Live both work; MiKTeX's `latexmk` can require
  Perl, but `xelatex` alone is enough for this pipeline.
- **`openpyxl`**, for the tracker workbook, and **`pypdf`**, which the healthcheck uses to read
  compiled PDFs back as text (`python -m pip install openpyxl pypdf` if they are not already
  present in your interpreter).
- **`pypdfium2`** (optional), only if you want `scripts/pdf_to_images.py` to rasterise a page for
  visual inspection. Without it the script falls back to `pdftoppm`, which ships with MiKTeX and
  Poppler.
- **A Telegram bot**, created via [BotFather](https://t.me/BotFather) — needed only for the
  watcher, not for the manual pipeline.

Verify the LaTeX toolchain before doing anything else:

```bash
python scripts/check_latex_toolchain.py
```

If it fails, install MiKTeX or TeX Live and add its `bin` folder to `PATH`, or set
`LATEX_ENGINE` to the full path of `xelatex.exe`/`latexmk.exe`.

## 2. Clone and scaffold

```bash
git clone <repo>
cd <repo>
python scripts/init_workspace.py
```

The repository is person-neutral: nothing in it identifies you. This step generates
`automation/build_settings.json` from its template, creates your personal `identity.toml`,
`rules/00-canonical-profile.md`, `automation/sources.toml`, and `automation/.env` from their
committed `.example` stubs (only if they do not already exist), writes a placeholder
`rules/slices/_facts.md`, and creates the runtime directories (`automation/state`,
`automation/logs`, `_tmp`). It prints a checklist of what is still outstanding — the rest of
this page is that checklist, explained.

## 3. Personalize the workspace with the `12-workspace-init` agent

Open Claude Code in the workspace and run the **`12-workspace-init`** agent. It interviews you
and reads your existing CV (you give it the file path), then drafts, one file at a time, each
presented for your line-by-line confirmation before it moves on:

- `identity.toml` — the printed contact block (name, file-name prefix, email, phone, address,
  nationality in English and German).
- `rules/00-canonical-profile.md` — your full fact source: work history, education, skills,
  languages, and an explicit **"may not be claimed"** list (certifications you do not hold,
  degrees started but not finished, tools only touched privately, seniority you have not held,
  work authorization you cannot support). The agent asks about this list rather than guessing.
- `automation/sources.toml` — `title_allow`/`title_deny` set for *your* field, and an initial
  `[[ats]]` board list for companies you name.
- `rules/slices/_facts.md` — a compressed slice of the profile, generated from it.

The agent is forbidden from inventing anything. Anything it cannot establish is left as a
literal `FILL IN — <what is missing>` marker and reported back to you at the end.

## 4. Do the manual steps the agent cannot do

1. **Photo and signature.** Put `photo.jpg` and `signature.png` into `master/LaTeX/images/`.
   Both are git-ignored — they never leave your machine.
2. **Watcher virtual environment and Playwright.** The watcher needs its own interpreter; its
   pins conflict with a typical global Python install.

   ```bash
   python -m venv automation/.venv
   automation/.venv/Scripts/pip install -r automation/requirements.txt   # Scripts/ on Windows
   automation/.venv/Scripts/playwright install chromium                  # bin/ on POSIX; ~150 MB, once
   ```

   On POSIX, replace `automation/.venv/Scripts/` with `automation/.venv/bin/` in both commands.
3. **Telegram bot token.** Talk to [BotFather](https://t.me/BotFather), create a bot, and put a
   **new** token into `automation/.env`:

   ```
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_CHAT_ID=<your chat id>
   ```

   Never put these values in a TOML file, and never reuse a token across two installs — two
   processes long-polling the same token fight over `getUpdates`. Send your new bot any message,
   then run `python -m watcher.whoami` (from `automation/`, with its own interpreter) to print
   the chat id.
4. **Interval offset.** If another install of this project polls the same job boards, offset
   `interval_minutes` in `automation/config.toml` so the two do not hammer them in lockstep.

## 5. Verify

```bash
python scripts/init_workspace.py --verify
```

This checks that `identity.toml` loads and is fully filled in, that
`automation/build_settings.json` has no unresolved template tokens, that the build guard's
interpreter path exists, that the guard's self-test passes, that `rules/slices/_facts.md` is no
longer the stub, and whether `photo.jpg`/`signature.png` are present. Fix anything it reports
before your first run — see [Troubleshooting](./troubleshooting.md) if a check keeps failing.

## 6. Run your first application end to end

Open an interactive Claude Code session in the workspace:

```bash
python start_claude.py
```

This preflights your identity and canonical profile, then opens `claude` in this directory.
Paste a job posting URL (or the full posting text), optionally with a note such as
`apply as Data Scientist` or `add German`. The 11-step pipeline defined in `CLAUDE.md` runs from
there — see [The Pipeline](./the-pipeline.md) for what each step does — and the finished CV,
cover letter, and interview prep land in
`<YYYY>/<Company>/<YYYY-MM-DD> - <Role>/`.

Once that works, set up the always-on watcher so postings reach you without you having to go
looking for them — see [The Watcher](./watcher.md).
