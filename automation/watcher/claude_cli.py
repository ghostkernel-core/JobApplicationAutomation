"""Thin wrapper around headless `claude -p`.

Every LLM call in the watcher goes through here, which means one auth path
(whatever the interactive CLI already uses) and no duplicated pipeline logic.

Two details that matter:

* The prompt is written to **stdin**, never to argv. Job descriptions run to
  several thousand characters and Windows caps a command line at ~32k; passing
  a batch of postings as an argument would truncate or fail outright.
* Non-build calls run from a neutral working directory. Claude Code loads
  CLAUDE.md from the cwd and its parents, so running the matcher inside the
  workspace would prepend the entire application-pipeline instruction set to
  every scoring call — irrelevant context, paid for on every batch.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("watcher.claude")


class ClaudeError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def resolve_bin(name: str) -> str:
    """Absolute path to the CLI.

    On Windows the npm shim is `claude.CMD`, and CreateProcess only finds it if
    the extension is spelled out — bare "claude" raises FileNotFoundError even
    though the command works fine in a shell. `shutil.which` applies PATHEXT.
    """
    if Path(name).exists():
        return str(Path(name).resolve())
    found = shutil.which(name)
    if not found:
        raise ClaudeError(
            f"{name!r} not found on PATH — the watcher needs the Claude Code CLI"
        )
    return found


def run(prompt: str, model: str | None = None, timeout: int = 180,
        cwd: str | Path | None = None, allowed_tools: str | None = "",
        claude_bin: str = "claude") -> str:
    """Run a one-shot prompt and return the assistant's text.

    `allowed_tools=""` (the default) forbids all tools, which is what pure
    reasoning calls want. Pass None to leave the CLI's own defaults alone.
    """
    cmd: list[str] = [resolve_bin(claude_bin), "-p", "--model", model or "haiku"]
    if allowed_tools is not None:
        cmd += ["--allowed-tools", allowed_tools]

    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd or tempfile.gettempdir()),
        )
    except FileNotFoundError as exc:
        raise ClaudeError(
            f"{claude_bin!r} not found on PATH — the watcher needs the Claude Code CLI"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"timed out after {timeout}s") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        raise ClaudeError(f"exit {completed.returncode}: {detail}")

    return (completed.stdout or "").strip()


# Models sometimes wrap JSON in prose or a fenced block despite instructions.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull a JSON object out of a model response.

    Tries the whole response, then a fenced block, then the outermost braces.
    Raises ClaudeError rather than returning None so the caller can retry.
    """
    candidates: list[str] = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ClaudeError(f"no JSON in response: {text[:300]!r}")


def run_json(prompt: str, retries: int = 1, **kwargs: Any) -> Any:
    """Run a prompt that must return JSON, retrying once on malformed output."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return extract_json(run(prompt, **kwargs))
        except ClaudeError as exc:
            last = exc
            if attempt < retries:
                log.warning("retrying after malformed response: %s", exc)
    raise last  # type: ignore[misc]
