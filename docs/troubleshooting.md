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
python start_watcher.py status     # lists every live instance, with pid and start time
python start_watcher.py stop       # stops all of them
python start_watcher.py start      # bring one back
```

`python start_watcher.py restart` does the last two in one step. (`py automation/watcherctl.py`
takes the same sub-commands — the root script forwards to it.) If you would rather look yourself:

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

## Postings stop arriving after a scoring outage

**Symptom:** polls keep finding postings — the counts in `watcher.log` look normal — but nothing
is ever pinged, for hours or days. `python -m watcher.match --replay 40` shows a run of postings
all sitting at exactly score **45**, verdict `maybe`, with the single gap
`scoring failed — judge from the posting`.

**Cause:** the scorer was briefly unreachable. That fallback verdict is what the matcher records
when it cannot get a judgement, and 45 is below `notify_threshold`, so those postings pass
silently. A cycle that degrades any batch now says so in Telegram, so this should not surprise
you twice — but a run from before that notice existed, or one you missed, leaves the postings
sitting there.

**Fix:** drop the fallback verdicts so the next cycle judges those postings for real:

```bash
py automation/watcherctl.py rescore
```

It prints which postings it re-queued, and anything that then clears `notify_threshold` pings on
the next cycle. The matcher no longer records a fallback verdict on the first failure — it holds
the posting back and retries it on later cycles, and only writes the fallback after
`[match] max_score_attempts` (default 3) genuine failures, at which point the posting really is
one it cannot read. So this command is for recovering history, not routine maintenance.

If the underlying failure is still happening, `watcher.log` now carries the real error rather
than the benign `claude.ai connectors are disabled` warning that used to mask it.

## Build refuses to start when `build_settings.json` is missing

**Symptom:** `python start_watcher.py` (or `py automation/watcherctl.py start`, which it forwards to) fails
its preflight with `automation/build_settings.json is missing. Run: python
scripts/init_workspace.py`; or `start_claude.py` prints the same as a warning (interactive
sessions do not need it).

**Cause:** `build_settings.json` is generated, not committed — a fresh clone has only
`build_settings.template.json`, and headless builds cannot run without the substituted file
(the deny-list and the build guard hook both come from it).

**Fix:** run `python scripts/init_workspace.py`, which substitutes `{{WORKSPACE_ROOT}}` in the
template and writes `automation/build_settings.json`. Safe to re-run any time — it always
regenerates from the template rather than trusting a stale copy.

## Every build fails, denied by its own permission settings

**Symptom:** a headless build runs its full timeout and produces payload JSON and briefs but no
`.tex` and no PDFs. The build log under `automation/logs/builds/` is full of
`<tool_use_error>File is in a directory that is denied by your permission settings.</tool_use_error>`,
and the model starts working around it — writing files with `cat > file << 'EOF'` heredocs,
testing with throwaway `testfile.txt`, splitting a payload across `partA.json`…`partE.json`.
`guard.log` shows nothing blocked, because the guard hook is not the one refusing.

**Cause:** a deny rule in `build_settings.json` covers the workspace itself. Deny beats allow and
permission rules have no negation, so "everywhere except the workspace" cannot be written as a
deny — and a broad rule standing in for it (`Edit(//C:/**)`, `Edit(~/**)`) is only true while the
workspace happens to sit outside that tree. Move the clone onto that drive or under your home
directory and the same rule silently denies every write *inside* it.

**Fix:** `python scripts/init_workspace.py` re-renders the file for wherever the clone now is,
and refuses to write one whose `Edit` rules match a workspace path. `--verify` checks the live
file and names any offending rule; the watcher re-checks before every build. If you added the
rule yourself, edit `automation/build_settings.template.json` — never the generated `.json` —
and keep it narrow enough that it can never contain a workspace. Containment outside the
workspace belongs to `automation/hooks/guard.py`, which derives the real root and can express it.

## A build fails with `API Error: 503` / `overloaded` / a gateway message

**Symptom:** a Telegram reply of `❌ Build failed` whose detail is an API error — most often
`API Error: 503 All accounts are temporarily unavailable, please check your inference gateway
(<host>)`. Possibly preceded by `🔁 Retrying`. The folder is gone, cleaned up.

**Cause:** the upstream API, or whatever gateway sits in front of it, was unavailable. Note the
host named in the message: if it is not an Anthropic domain, the outage is your gateway's, not
the API's, and that is where to look. This is not a fault in the pipeline — a build simply ends
whenever a turn cannot complete, and it can end on the last turn of a run that had already done
everything.

**Fix:** reply `yes` again once the upstream is back; the build starts from scratch. The watcher
already retries an upstream failure `[build] retries` times (default 1) after
`retry_delay_seconds`, so a brief blip is absorbed without you seeing it; raise `retries` in
`automation/config.toml` if your gateway is flaky, at the cost of an extra full run per failure.
A failure that is *not* upstream — a timeout, a crash, missing documents — is never retried, so
seeing no `🔁 Retrying` for one of those is correct.

## Console windows pop up in front of whatever you are doing

**Symptom:** cmd or PowerShell windows appear on screen while the watcher works — a brief flash
each poll cycle, and during a build one that sits in the foreground for the whole run.

**Cause:** the watcher runs detached under `pythonw.exe`, which has no console. Windows gives a
console program a console, and with no parent console to inherit it allocates a new one, visible.

**Fix:** already handled — every spawn in the watcher and in the build scripts passes
`CREATE_NO_WINDOW` via `scripts/no_console.py`. If you see one again, it is from a spawn that
does not use that helper; the exceptions are deliberate, being the commands `watcherctl.py`
forwards to your own terminal, where the output is the point. Playwright hides its own driver
window already and needs nothing.

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
