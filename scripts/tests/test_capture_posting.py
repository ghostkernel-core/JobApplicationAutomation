"""Offline tests for the posting-capture ladder.

The browser halves are not exercised here — they need a real network and a real
Chromium, and there is a live workflow for that. What is tested is the part that
made the AXA build stop to ask: the decision of whether a captured file is the
posting, and what happens next when it is not.

That decision is the whole point of the script. A capture method that fails
loudly needs no cleverness; the one that exits 0 with `403 Forbidden` in it is
why `capture()` reads the file instead of trusting the return code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capture_posting as cap  # noqa: E402

#: The real thing, trimmed: what SingleFile wrote for careers.axa.com with
#: `--browser-wait-until=load`, exit code 0, 640 bytes on disk.
FORBIDDEN = (
    "<html><!-- Page saved with SingleFile --><meta charset=utf-8>"
    "<title>403 Forbidden</title></head><body>"
    "<center><h1>403 Forbidden</h1></center>"
)

POSTING = (
    "<html><head><title>AI Engineer Expert</title>"
    "<style>.a{color:red}</style></head><body>"
    "<script>window.x=1</script>"
    "<h1>AI Engineer Expert</h1><p>" + ("Design and deploy AI systems. " * 60) +
    "</p></body></html>"
)


def _writes(markup: str, ok: bool = True, detail: str = "fine"):
    """A capture method that always produces `markup`."""
    def method(url: str, dest: Path, timeout: int) -> tuple[bool, str]:
        if ok:
            dest.write_text(markup, encoding="utf-8")
        return ok, detail
    return method


def _records(calls: list[str], name: str, inner):
    def method(url: str, dest: Path, timeout: int) -> tuple[bool, str]:
        calls.append(name)
        return inner(url, dest, timeout)
    return method


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "out" / "Company - Role.html"


def test_visible_text_ignores_scripts_and_styles():
    text = cap.visible_text(POSTING)
    assert "window.x" not in text
    assert "color:red" not in text
    assert text.startswith("AI Engineer Expert")


def test_an_error_page_measures_far_under_the_floor():
    assert len(cap.visible_text(FORBIDDEN)) < cap.MIN_CHARS


def test_a_real_posting_clears_the_floor():
    assert len(cap.visible_text(POSTING)) >= cap.MIN_CHARS


def test_singlefile_wins_when_it_gets_the_page(monkeypatch, dest):
    calls: list[str] = []
    monkeypatch.setattr(cap, "capture_singlefile",
                        _records(calls, "singlefile", _writes(POSTING)))
    monkeypatch.setattr(cap, "capture_rendered",
                        _records(calls, "playwright", _writes(POSTING)))

    assert cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS,
                       renderer="auto") == 0
    # The second method is not merely unused, it is never started: rendering a
    # page we already have is 20 seconds of nothing.
    assert calls == ["singlefile"]
    assert "AI Engineer Expert" in dest.read_text(encoding="utf-8")


def test_a_403_that_exits_zero_falls_through_to_the_browser(monkeypatch, dest):
    """The failure this script exists for. SingleFile succeeds; the file is junk."""
    monkeypatch.setattr(cap, "capture_singlefile", _writes(FORBIDDEN))
    monkeypatch.setattr(cap, "capture_rendered", _writes(POSTING))

    assert cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS,
                       renderer="auto") == 0
    saved = dest.read_text(encoding="utf-8")
    assert "AI Engineer Expert" in saved
    assert "403" not in saved


def test_a_hard_singlefile_failure_falls_through_too(monkeypatch, dest):
    monkeypatch.setattr(cap, "capture_singlefile",
                        _writes("", ok=False, detail="Execution context not found"))
    monkeypatch.setattr(cap, "capture_rendered", _writes(POSTING))

    assert cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS,
                       renderer="auto") == 0
    assert dest.exists()


def test_nothing_is_written_when_both_methods_fail(monkeypatch, dest):
    """A rejected capture must not land at the destination.

    Each method writes to scratch and is copied over only once it passes, so a
    failed run leaves the previous archive — or no archive — untouched rather
    than a 640-byte 403 that later steps would parse as the posting.
    """
    monkeypatch.setattr(cap, "capture_singlefile", _writes(FORBIDDEN))
    monkeypatch.setattr(cap, "capture_rendered", _writes(FORBIDDEN))

    assert cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS,
                       renderer="auto") == 1
    assert not dest.exists()


def test_both_reasons_are_reported_when_both_fail(monkeypatch, dest, capsys):
    monkeypatch.setattr(cap, "capture_singlefile",
                        _writes("", ok=False, detail="Execution context not found"))
    monkeypatch.setattr(cap, "capture_rendered",
                        _writes("", ok=False, detail="chromium is not installed"))

    cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS, renderer="auto")
    err = capsys.readouterr().err
    assert "Execution context not found" in err
    assert "chromium is not installed" in err


def test_the_rejected_method_is_still_reported_on_success(monkeypatch, dest, capsys):
    """A caveat worth passing on: the archive is text-only, not a faithful copy."""
    monkeypatch.setattr(cap, "capture_singlefile", _writes(FORBIDDEN))
    monkeypatch.setattr(cap, "capture_rendered", _writes(POSTING))

    cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS, renderer="auto")
    out = capsys.readouterr().out
    assert "METHOD: rendered browser" in out
    assert out.count("NOTE:") == 1


def test_forcing_a_renderer_skips_the_other(monkeypatch, dest):
    calls: list[str] = []
    monkeypatch.setattr(cap, "capture_singlefile",
                        _records(calls, "singlefile", _writes(POSTING)))
    monkeypatch.setattr(cap, "capture_rendered",
                        _records(calls, "playwright", _writes(POSTING)))

    assert cap.capture("u", dest, timeout=1, min_chars=cap.MIN_CHARS,
                       renderer="playwright") == 0
    assert calls == ["playwright"]


def test_singlefile_needs_bash(monkeypatch, tmp_path):
    monkeypatch.setattr(cap.shutil, "which", lambda name: None)
    ok, detail = cap.capture_singlefile("u", tmp_path / "x.html", 1)
    assert not ok
    assert "bash" in detail
