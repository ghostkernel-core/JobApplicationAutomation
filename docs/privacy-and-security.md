# Privacy and Security

This repository is designed to be public while everything about the person using it stays
private. This page covers the three mechanisms that make that true.

## The top-level-whitelist `.gitignore` model

`.gitignore` at the repository root is a **whitelist**, not a blacklist: its first rule is `/*`,
which ignores everything at the workspace root, and every line after that re-admits one specific
path with a leading `!`. The practical effect is that new folders are private by default — an
application year, a company folder, an export, a scan of a personal document, a recruiter's CV
never becomes trackable merely because it was created under a new name. You do not need to
remember to ignore anything new; only what is explicitly re-admitted is ever tracked.

Committed: `scripts/`, `rules/` (the rule files and the `.example` stubs), `master/` (templates,
shared macros, the openly licensed fallback font, image assets other than your own photo/
signature), `automation/` (the watcher's code, `config.toml`, `sources.toml.example`), `docs/`,
`.claude/agents/`, `.github/`, `CLAUDE.md`, `README.md`, and the two root launchers
(`start_claude.py`, `start_watcher.py`).

Never committed: `identity.toml`, `rules/00-canonical-profile.md`, `rules/slices/_facts*.md`,
`automation/.env`, `automation/sources.toml`, `automation/profile_kb.md`,
`automation/decisions.jsonl`, `automation/state/`, `automation/logs/`, `automation/build_settings.json`
(machine-specific, regenerated from a committed template), every application year folder and
tracker workbook, the photo/signature image assets, and the proprietary Helvetica Neue font
files.

A CI job (`.github/workflows/checks.yml`) enforces the boundary structurally on every push and
pull request: it fails if any of these ever end up tracked, independent of whether `.gitignore`
would have caught it locally.

## The headless-build sandbox

A watcher-spawned build runs unattended — there is nobody to answer a permission prompt — so it
runs under `claude -p --permission-mode bypassPermissions --settings build_settings.json`. That
mode is a lot of latitude, narrowed by two independent layers, because neither is sufficient on
its own:

1. **A generated deny-list**, `automation/build_settings.json` (generated from
   `build_settings.template.json` by `scripts/init_workspace.py`, with `{{WORKSPACE_ROOT}}`
   substituted for your actual path). Explicit `permissions.deny` rules still apply under
   `bypassPermissions`, so this catches the obvious cases even if the hook below is
   misconfigured — destructive shell verbs, reading `.claude`/`.ssh`/`.aws` directories anywhere
   on the machine, reading `automation/.env`, editing inside `automation/` itself, and
   `git push`. It cannot express "anywhere except the workspace": deny beats allow and there is
   no negation, so it names specific targets rather than trying to be exhaustive.
2. **`automation/hooks/guard.py`**, a blocking `PreToolUse` hook wired up in the same settings
   file, which *can* express "anywhere except the workspace" because it runs arbitrary Python.
   Every `Bash` command is screened against destructive-command patterns, credential paths, and
   system paths, and against writing outside the workspace (reading elsewhere is allowed — the
   canonical profile may cite work stored on another drive). Every `Edit`/`Write`/`NotebookEdit`
   target must resolve inside the workspace. Every `Read` is blocked only from credential paths.
   Exit code 2 blocks the tool call and its stderr becomes the reason the model sees. Every
   decision — allowed or blocked — is appended to `automation/logs/builds/guard.log`, and the
   hook ships with a `--self-test` fixture table (`python automation/hooks/guard.py --self-test`)
   that `scripts/init_workspace.py --verify` also runs.

**Its honest limits.** This is defence against a confused agent, not a hostile one, and neither
layer reaches *inside* a subprocess: once a permitted `python scripts/render_latex_application.py`
process starts, it can write anywhere your account can, because Claude Code's OS-level sandbox
does not run on native Windows. What actually bounds the blast radius is the pinned working
directory, the full NDJSON build log plus `guard.log`, and the fact that a build only ever starts
from an explicit Telegram reply — never from anything reachable on the command line. Moving
builds into WSL2 would buy real process-level enforcement, at the cost of reinstalling the LaTeX
and Python toolchain there.

## Secret handling

Both watcher secrets — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — live only in
`automation/.env`, created from `automation/.env.example`, and are read as environment variables.
They must never be moved into `config.toml` or `sources.toml`, both of which are ordinary TOML
files with no secret-handling of their own.

Never reuse a Telegram bot token across two installs. Telegram's `getUpdates` long-polling is
exclusive per token: two processes polling the same token fight over it and both start throwing
`telegram.error.Conflict`. Every install needs its own bot, created fresh via BotFather.

## The CI PII scan

`.github/workflows/checks.yml` runs three checks on every push and pull request: a structural
check that no forbidden path (see above) is tracked, `gitleaks` for generic secret patterns, and
an optional personal-data scan driven by a repository secret named `PII_PATTERNS` — a
newline-separated list of extended regular expressions (`grep -E` syntax) matching your own
name, address, and phone number in whatever forms they might appear.

If you fork or clone this repository for your own use, set that secret yourself. The patterns
live in the secret rather than in the repository so they are never public themselves, and the
workflow step is written to report only *which file matched*, never the matched text or the
pattern that matched it — so even a passing CI log never becomes a second place your personal
data could leak from.
