# Troubleshooting

## LaTeX engine not found / `LATEX_ENGINE`

**Symptom:** `python scripts/check_latex_toolchain.py` reports
`ERROR: no latexmk or xelatex found`, or rendering fails at the compile step.

**Cause:** neither `latexmk` nor `xelatex` is on `PATH`, and `LATEX_ENGINE` is not set.

**Fix:** install MiKTeX or TeX Live and add its `bin` folder to `PATH` (on Windows,
`C:\Program Files\MiKTeX\miktex\bin\x64` or `C:\texlive\<year>\bin\windows`), or set the
`LATEX_ENGINE` environment variable to the full path of `xelatex.exe`/`latexmk.exe`. Re-run
`python scripts/check_latex_toolchain.py`; it lists every candidate it found and whether it
actually runs, not just whether it exists on disk.

## MiKTeX's `latexmk` wants Perl

**Symptom:** `latexmk` fails to run, or complains it cannot find a Perl interpreter.

**Cause:** MiKTeX's `latexmk` is a Perl wrapper script; a MiKTeX install without Perl on `PATH`
cannot run it.

**Fix:** `xelatex` alone is sufficient for this pipeline — set `LATEX_ENGINE` to `xelatex.exe`
directly instead of installing Perl.

## `identity.toml` still has `FILL IN`, or fails to load with a BOM error

**Symptom:** `python scripts/init_workspace.py --verify` fails at the `[identity]` check, or
`start_claude.py`/rendering scripts fail to parse `identity.toml`.

**Cause:** either a `FILL IN` placeholder was never replaced, or the file was saved with a UTF-8
byte-order mark (BOM) — common when a text editor's "UTF-8" preset actually means "UTF-8 with
BOM" — which Python's `tomllib` does not strip automatically.

**Fix:** open `identity.toml` and replace every `FILL IN` value (run the `12-workspace-init`
agent if you have not yet, or edit it by hand). If the file loads with an encoding error, re-save
it as UTF-8 *without* a BOM.

## Missing photo or signature

**Symptom:** `python scripts/init_workspace.py --verify` warns `photo.jpg missing` or
`signature.png missing` under `[master/LaTeX/images]`; a compiled CV or letter is missing the
photo or the signature.

**Cause:** these two image files are git-ignored and personal — a fresh clone never has them.

**Fix:** put `photo.jpg` and `signature.png` into `master/LaTeX/images/` yourself, as noted in
[Getting Started](./getting-started.md). This is a warning, not a hard failure, because not every
CV convention requires a photo.

## Watcher virtual environment problems

**Symptom:** `automation/.venv/Scripts/python.exe` (or `bin/python` on POSIX) does not exist, or
running the watcher with the global Python fails with a dependency conflict.

**Cause:** the watcher pins `python-telegram-bot==22.8`, which conflicts with a typical global
install that may already pin `python-telegram-bot <21`. The watcher deliberately needs its own
interpreter, never the global one.

**Fix:**

```bash
python -m venv automation/.venv
automation/.venv/Scripts/pip install -r automation/requirements.txt   # bin/ on POSIX
```

Always invoke watcher commands (`run_watcher.py`, `python -m watcher.*`) with this interpreter,
not the global one.

## Playwright chromium missing

**Symptom:** the `hiringcafe` and `stepstone` portal fetchers disable themselves after repeated
failures, or a log mentions a missing browser executable.

**Cause:** Playwright's Chromium binary is a separate ~150 MB download that
`pip install -r automation/requirements.txt` does not fetch on its own; this is a **soft
dependency** by design.

**Fix:**

```bash
automation/.venv/Scripts/playwright install chromium   # bin/ on POSIX
```

Without it, the `[[ats]]` tier and the `arbeitsagentur` portal (a plain JSON API) run normally;
only the two browser-based fetchers — `hiringcafe` and `stepstone`, both already marked
`fragile` — degrade, and each disables itself after `failures_before_disable` consecutive
failures rather than retry-spamming. `python -m watcher.health --reset portal:stepstone`
re-enables one once the dependency is installed.

## Telegram `getUpdates` conflict from a shared token

**Symptom:** the watcher's log fills with `telegram.error.Conflict: terminated by other
getUpdates request`.

**Cause:** two processes are long-polling the same bot token — usually a leftover watcher
process from an earlier session, or (rarely) two installs configured to share one bot.

**Fix:** find and stop the other process:

```bash
py automation/watcherctl.py status     # lists every live instance, with pid and start time
py automation/watcherctl.py stop       # stops all of them
py automation/watcherctl.py start      # bring one back
```

`restart` does the last two in one step. If you would rather look yourself:

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like '*run_watcher*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate
```

A venv's `pythonw.exe` is a launcher stub that re-executes the real interpreter as a child, so
one healthy watcher shows up here as two rows — a parent with one thread and a child with
several. Two *unrelated* rows, with different start times, are the actual conflict.

If two installs are genuinely meant to run independently, give each its own bot token from
BotFather — see [Privacy and Security](./privacy-and-security.md).

## No postings surviving the prefilter

**Symptom:** `python -m watcher.poll --dry-run` (or a real poll) returns nothing, or far fewer
postings than expected.

**Cause:** almost always `[defaults] title_allow` in `automation/sources.toml` is narrower than
intended — it is the single most load-bearing filter, and a posting must match one of its
keywords to survive at all.

**Fix:**

```bash
python -m watcher.poll --dry-run --show-filtered   # see what the prefilter actually dropped, and why
python -m watcher.prefilter --explain              # every rule, fully expanded, against stored postings
```

Broaden `title_allow` with the role phrasings you would actually apply to. Remember every other
filter in `sources.toml` is one-sided (see [The Watcher](./watcher.md)) — a posting is dropped
only for a title mismatch, a resolved location outside your allow-list, or a denied seniority
band, never for something the filter simply failed to read.

## Build refuses to start when `build_settings.json` is missing

**Symptom:** `start_watcher.py` exits with
`automation/build_settings.json is missing. Run: python scripts/init_workspace.py`; or
`start_claude.py` prints the same as a warning (interactive sessions do not need it).

**Cause:** `build_settings.json` is generated, not committed — a fresh clone has only
`build_settings.template.json`, and headless builds cannot run without the substituted file
(the deny-list and the build guard hook both come from it).

**Fix:** run `python scripts/init_workspace.py`, which substitutes `{{WORKSPACE_ROOT}}` in the
template and writes `automation/build_settings.json`. Safe to re-run any time — it always
regenerates from the template rather than trusting a stale copy.

## `init_workspace.py --verify` failures

**Symptom:** any of the five checks under `--verify` reports `FAIL` or `WARN`.

**Cause and fix, per check:**

| Check | Meaning | Fix |
|---|---|---|
| `[identity]` FAIL | `identity.toml` does not load or is not fully filled in | see "`identity.toml` still has FILL IN" above |
| `[build_settings.json]` FAIL | file missing, or still contains an unresolved `{{` token | run `python scripts/init_workspace.py` |
| `[build guard interpreter]` WARN | the watcher venv interpreter path in `build_settings.json` does not exist yet | create `automation/.venv` (see "Watcher virtual environment problems" above) |
| `[guard self-test]` not all-pass | `automation/hooks/guard.py --self-test` failed one of its fixture cases | this indicates a real bug in the guard logic — do not weaken a rule to make the test pass; investigate the specific failing case it prints |
| `[rules/slices/_facts.md]` FAIL | still the generated stub, never written from the real profile | run the `12-workspace-init` agent, or write it by hand once `rules/00-canonical-profile.md` is real |
| `[master/LaTeX/images]` WARN | `photo.jpg` or `signature.png` missing | see "Missing photo or signature" above (warning only, not blocking) |

`--verify` never writes anything; re-run it after each fix until every check passes before your
first real run.
