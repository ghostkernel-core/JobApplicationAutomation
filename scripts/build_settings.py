"""Generate automation/build_settings.json from its template, and refuse to
generate one that locks the build out of its own workspace.

`build_settings.json` is a generated file: the template is the source of truth
and `{{WORKSPACE_ROOT}}` is substituted with wherever this clone actually sits.
Two callers share this module rather than each carrying a copy of the
substitution — `scripts/init_workspace.py` (setup and --verify) and the watcher,
which re-syncs before every headless build so a moved clone or an edited
template applies without a restart.

The check that matters is `conflicts()`. Claude Code's permission rules cannot
express "anywhere except the workspace" — deny beats allow and there is no
negation — so it is tempting to approximate it with a broad rule like
`Edit(//C:/**)` or `Edit(~/**)`. That approximation is only true for as long as
the workspace happens to sit outside the named tree. Move the clone under the
home directory and those rules deny every write *inside* the workspace: each
build then spends its whole timeout being refused by its own settings file, with
nothing but a generic "denied by your permission settings" to explain why.

So containment stays with `automation/hooks/guard.py`, which derives the real
workspace root and can express the negation, and this module asserts the
invariant the deny list must hold to: no Edit rule may match a path inside the
workspace, `automation/` excepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TEMPLATE_PATH = ROOT / "automation" / "build_settings.template.json"
TARGET_PATH = ROOT / "automation" / "build_settings.json"

# Paths a build legitimately writes to, used to probe the deny list. Not an
# exhaustive map of the workspace — one representative path per area a run
# touches is enough to catch a rule broad enough to swallow the whole tree.
PROBES = (
    "CLAUDE.md",
    "identity.toml",
    "rules/00-canonical-profile.md",
    "2026/Acme/2026-01-01 - Data Scientist/Surname, Firstname - CV.tex",
    "_tmp/payloads/Acme 2026-01-01/cv_payload_en.json",
    "master/LaTeX/templates/cv_en.tex",
)

# `automation/` is denied on purpose: a build has no business editing the
# watcher that spawned it. Probes never reach into it.
_RULE_RE = re.compile(r"^(?P<tool>[A-Za-z]+)\((?P<pattern>.*)\)$")


def render(root: Path = ROOT) -> str:
    """The template with {{WORKSPACE_ROOT}} resolved. Raises if any token is left."""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    out = text.replace("{{WORKSPACE_ROOT}}", root.as_posix())
    remaining = [line for line in out.splitlines() if "{{" in line]
    if remaining:
        raise RuntimeError(
            "build_settings template still has unresolved tokens:\n  "
            + "\n  ".join(remaining))
    return out


def _to_regex(pattern: str, root: Path) -> re.Pattern[str]:
    """A Claude Code permission path pattern as a regex over posix paths.

    Path syntax, per the template's own comment: `//` starts an absolute path,
    `~/` the home directory, a single `/` is relative to the settings file's
    directory (automation/), and anything else is relative to it too.
    """
    if pattern.startswith("//"):
        # `//` introduces an absolute path. On POSIX that leaves the leading
        # slash of `/home/...`; on Windows the absolute form is `C:/...` with no
        # slash at all, and keeping one would stop `//C:/**` from being
        # recognised as covering `C:/Users/...` — which is precisely the rule
        # that has to be caught.
        base = pattern[1:]
        if re.match(r"^/[A-Za-z]:/", base):
            base = base[1:]
    elif pattern.startswith("~/"):
        base = (Path.home().as_posix().rstrip("/") + "/" + pattern[2:])
    elif pattern.startswith("/"):
        base = (root / "automation").as_posix() + pattern
    else:
        base = (root / "automation").as_posix() + "/" + pattern

    out: list[str] = []
    i = 0
    while i < len(base):
        char = base[i]
        if base.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    # Windows paths are case-insensitive; a rule spelled C:/Users must still be
    # recognised as covering c:/users.
    return re.compile("".join(out) + "$", re.IGNORECASE)


def conflicts(text: str, root: Path = ROOT) -> list[str]:
    """Deny rules that would block the build from writing inside the workspace.

    Returns the offending rule strings, empty when the settings file is sound.
    Only Edit rules are considered: `Write(...)` rules are inert in Claude Code,
    and `Edit(path)` is what every file-editing tool is actually matched against.
    """
    rules = json.loads(text).get("permissions", {}).get("deny", [])
    probes = [(root / rel).as_posix() for rel in PROBES]

    bad: list[str] = []
    for rule in rules:
        match = _RULE_RE.match(str(rule).strip())
        if not match or match.group("tool") != "Edit":
            continue
        compiled = _to_regex(match.group("pattern"), root)
        if any(compiled.match(probe) for probe in probes):
            bad.append(str(rule))
    return bad


def sync(root: Path = ROOT, target: Path = TARGET_PATH) -> str:
    """Write build_settings.json if it differs from the rendered template.

    Returns a one-line status for the caller to log or print. Raises RuntimeError
    if the result would deny the build write access to its own workspace — better
    to refuse to build at all than to burn a 45-minute timeout discovering it one
    denied tool call at a time.
    """
    generated = render(root)

    bad = conflicts(generated, root)
    if bad:
        raise RuntimeError(
            "these deny rules would block writes inside the workspace at "
            f"{root}:\n  " + "\n  ".join(bad)
            + "\nEdit automation/build_settings.template.json — containment "
              "outside the workspace is automation/hooks/guard.py's job, not a "
              "broad path deny's.")

    if not target.exists():
        target.write_text(generated, encoding="utf-8")
        return "created"
    if target.read_text(encoding="utf-8") == generated:
        return "up to date"
    target.write_text(generated, encoding="utf-8")
    return "regenerated from the template"
