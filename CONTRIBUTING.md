# Contributing

Thanks for looking at this. A few things worth knowing before you send a PR.

## This repo holds machinery, never personal data

The repository is person-neutral by design: `identity.toml`, `rules/00-canonical-profile.md`,
`automation/.env`, `automation/sources.toml`, application folders (`<YYYY>/...`), tracker
workbooks, and the photo/signature images are all git-ignored on purpose. A PR must never add a
real name, email, phone number, street address, postcode, city tied to a person, LinkedIn URL,
or an application folder. `.github/workflows/checks.yml` runs a structural check for known
private paths, `gitleaks`, and a `PII_PATTERNS`-driven scan — any of these will fail CI, and a
failure here is not a false positive to argue around, it's the system working.

If your change genuinely needs an example value (a sample identity block, a sample profile
entry), use obviously fake placeholders, not a real person's details, including your own.

## Setting up a dev copy

```bash
python scripts/init_workspace.py
```

This materializes the git-ignored personal files from their `.example` counterparts
(`identity.toml`, `rules/00-canonical-profile.md`, `automation/sources.toml`,
`automation/.env`, `rules/slices/_facts.md`), generates `automation/build_settings.json` from
its template, and creates the runtime directories nothing checks in. Fill the generated
`identity.toml` and `rules/00-canonical-profile.md` with dummy values — you don't need a real
profile to test rendering or the watcher's mechanics. Then confirm the workspace is coherent:

```bash
python scripts/init_workspace.py --verify
```

## What's welcome

- **New watcher fetchers** under `automation/watcher/fetchers/`, following the shape in
  `automation/watcher/fetchers/base.py` (session/`get_json`/`get_text` helpers, `country_of`,
  `is_remote`, `first`). A new job board is one of the most useful contributions this project
  can take.
- **LaTeX template fixes** in `master/LaTeX/templates/` and `master/LaTeX/shared/` — layout
  bugs, overflow edge cases, compiler compatibility.
- **Docs.**

Changes to the agent prompts (`.claude/agents/`) or the rules (`rules/`) are welcome too, but
since they encode judgment calls about tone and integrity, expect more back-and-forth on those.

## House rules that matter

- **Filters are one-sided.** Anything in `automation/watcher/prefilter.py` (and the geo/role
  helpers it calls) may reject only what it positively resolved. A location that fails to parse
  to a country, a title with no stated seniority — these must pass through to the scorer, never
  get silently dropped. A filter that treats "I couldn't read this" the same as "this doesn't
  match" turns every unfamiliar board format into invisible data loss. If you touch a filter,
  keep this asymmetry.
- **Deterministic Python owns rendering; models don't debug the compiler.** LaTeX/PDF defects
  get fixed in the payload, the template, or the rendering script — never by asking a model to
  puzzle over a LaTeX error. Run `scripts/latex_healthcheck.py` before assuming anything needs a
  model at all.
- **No fabrication.** `rules/05-integrity-no-fabrication.md` is strict: every factual claim in a
  generated application document must be directly supported by the canonical profile. If you're
  changing an agent that writes or verifies application content, this rule is the one that
  outranks "make it sound better."

## Testing your change

Run whichever of these are relevant to your change, and paste the output in your PR:

```bash
python scripts/check_latex_toolchain.py
python scripts/latex_healthcheck.py <application-folder>
python -m watcher.poll --dry-run
python scripts/init_workspace.py --verify
```

If you touched `automation/hooks/guard.py`, run its self-test directly — it's invoked as
`python guard.py --self-test` and also exercised by `init_workspace.py --verify`:

```bash
python automation/hooks/guard.py --self-test
```

## Commit style

Plain, human, imperative subject lines (`fix hybrid postings tripping the remote filter`, not
`Fix: Hybrid Postings Tripping Remote Filter` or a changelog-style body). Explain the why in the
body if it isn't obvious from the diff.
