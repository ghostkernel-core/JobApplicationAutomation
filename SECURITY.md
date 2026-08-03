# Security Policy

This is a personally-maintained, self-hosted job-application system. It runs on one person's
machine against one person's job-board accounts and Telegram bot; there is no hosted service,
multi-tenant data, or production deployment to protect. That shapes what "security" means here:
the main risks are credential leakage and a headless agent doing something it shouldn't inside
its own working directory, not a web application attack surface.

## Reporting a vulnerability

Please use [GitHub's private security advisories](https://github.com/ghostkernel-core/JobApplicationAutomation/security/advisories/new) for this
repository rather than opening a public issue, especially for anything that involves:

- a real or plausible way to exfiltrate the Telegram bot token or other credentials,
- a way for a headless build to read or write outside its pinned workspace,
- a way for job-posting text to escape the model sandbox and affect the host system.

This is a side project maintained by one person. Expect an acknowledgement within about a
week, not a same-day response. There is no bug bounty. If you don't hear back in two weeks,
a polite follow-up on the same advisory thread is completely fine.

## Risk surface, stated plainly

**The Telegram bot token.** `automation/.env` holds a bot token that lets the watcher read
messages and send notifications on one Telegram bot. It is git-ignored and must never be
committed. A leaked token lets someone else impersonate the bot to the one chat it talks to —
it does not grant access to this machine, this repository, or any other account.

**Headless build containment is two layers, not one, and neither is a hard boundary.**
Headless builds run `claude -p --permission-mode bypassPermissions`, which is deliberately
permissive so an unattended multi-step pipeline can run to completion. Two things narrow that
back down:

1. A generated `automation/build_settings.json` (from `build_settings.template.json`) carries
   `Edit`/deny rules that still apply under `bypassPermissions`.
2. `automation/hooks/guard.py`, a blocking `PreToolUse` hook, screens every `Bash`, `Edit`,
   `Write`, `NotebookEdit`, and `Read` call. It blocks destructive commands (recursive deletes,
   registry writes, `git push`, piping a download into a shell, and similar), blocks reads and
   writes touching credential or system paths, and blocks any *write* outside the pinned
   workspace. Reads may roam outside the workspace, because the candidate's canonical profile
   legitimately cites work stored on another drive. Every decision — allowed or blocked — is
   appended to `automation/logs/builds/guard.log`.

Neither layer reaches **inside** a subprocess: once a permitted command like `python
scripts/render_latex_application.py` actually starts, it can do anything the running user
account can do. This is defence against a confused or overreaching agent, not against a
determined, hostile one — the guard's own docstring says as much. What genuinely bounds the
blast radius of a headless run is not the deny-list or the hook alone, but the combination of:
the pinned working directory a build never leaves, full NDJSON logging of every tool call, and
the fact that a build only ever starts from an explicit "yes" reply in Telegram — nothing polls
or scores its way into a build on its own.

**Job postings are untrusted third-party text that reaches a model.** The watcher fetches
posting text from external job boards and hands it to an LLM for scoring and, later, an agent
pipeline for drafting. That text is adversarial by construction: anyone who can get a posting
indexed by a board this watcher polls can put arbitrary instructions in front of the model. The
mitigations are structural, not a content filter: the candidate's facts live in
`rules/00-canonical-profile.md`, a local file the posting text cannot touch or rewrite, so a
prompt-injected posting can at most produce a bad draft — it cannot conjure a fabricated
credential the profile doesn't contain (see `rules/05-integrity-no-fabrication.md`), and
generated documents go through dedicated verifier agents before anything is rendered.

## What is and isn't treated as a vulnerability

Treated as a vulnerability: a way to make a headless build write, delete, or exfiltrate outside
its pinned workspace; a way to defeat or bypass `guard.py` from inside a build; a way to leak a
credential (bot token, `ANTHROPIC_AUTH_TOKEN`, etc.) into a rendered document or a public log;
a gitleaks or PII-scan bypass in CI that lets tracked personal data slip through.

Not treated as a new finding, because it is a known and already-documented property of this
design: "an LLM can be influenced by text in a job posting it's asked to read." That's why the
fact source is deliberately local, separate from anything the posting can reach, and why every
generated document passes through a verifier before it's finalized. If you find a way to turn
prompt injection into something that escapes the sandbox (workspace writes, credential
exposure, arbitrary command execution outside the guard's reach), that *is* in scope — please
report it.

## Hardening advice if you run your own copy

- Never commit `automation/.env`, `identity.toml`, `rules/00-canonical-profile.md`, or any
  application folder — `.gitignore` and the CI structural check both exist for this, but don't
  rely on CI alone.
- Get a fresh bot token from BotFather for your own install. Never reuse a token across
  machines or forks, and rotate it if you ever suspect it leaked.
- If you fork this publicly, set your own `PII_PATTERNS` repository secret (see
  `.github/workflows/checks.yml`) with regexes matched to your own name, email, phone, and
  address before you push any real content.
- Keep `automation/hooks/guard.py` wired up in `automation/build_settings.json` — don't disable
  the hook to "get past" a build that's blocked; the block is almost always the guard doing its
  job. If a legitimate workflow trips it, fix the rule, don't remove the hook.
- Review `automation/logs/builds/guard.log` and the NDJSON build logs periodically, especially
  after any change to prompts, agents, or the watcher's sources.
