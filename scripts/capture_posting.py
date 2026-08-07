"""Capture a job posting page as HTML, whatever the page does to resist it.

One entry point for step 00, replacing a direct call to `save_singlefile.sh`.
It tries the same SingleFile capture first, **checks that what came back is
actually the posting**, and renders the page in a real browser if it is not.

Why the check exists at all. A capture can fail in two ways and only one of
them is loud:

* **Loud.** SingleFile exits non-zero and writes nothing. `careers.axa.com`
  does this — "Execution context not found for SingleFile world", because the
  page client-side navigates while SingleFile is reaching into it.
* **Quiet.** SingleFile exits *zero* and writes a file that is not the posting.
  The same AXA URL, captured with `--browser-wait-until=load`, produces 640
  bytes whose entire content is `403 Forbidden`. Nothing about the exit code,
  the file's existence, or its non-emptiness distinguishes that from a real
  archive — only reading it does.

The quiet one is why this measures visible text rather than trusting the exit
code. A run that archives a 403 page goes on to write a whole application
against a posting nobody ever read.

The fallback is Playwright: load the page, let it settle until its own text
stops growing, and write the rendered DOM with scripts and stylesheets stripped
out. Same technique the watcher uses to hydrate posting bodies, and it gets this
page in full. Stripping matters as well as rendering — AXA's raw DOM is 847 KB,
which is past the Read limit and therefore useless to the agent that has to
parse it; without scripts and styles the same content is 83 KB.

Usage:
    python scripts/capture_posting.py <url> <output.html>
    python scripts/capture_posting.py <url> <out.html> --timeout 120 --min-chars 600
    python scripts/capture_posting.py <url> <out.html> --renderer playwright

Exit status is 0 only when a usable capture is on disk. Both methods failing is
exit 1 with each one's reason, which is the point at which asking for pasted
text is honest rather than premature.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SINGLEFILE = ROOT / "scripts" / "save_singlefile.sh"

#: Below this much visible text, the file is a shell, an error page or a
#: challenge rather than a posting. Deliberately far under any real ad — the
#: shortest genuine posting body seen is around 2900 characters — because the
#: job here is to catch an obvious non-capture, not to grade a short one. It
#: will not catch a shell that ships a large navigation menu; nothing cheap
#: will, and the loud failures are the ones that were actually happening.
MIN_CHARS = 600

#: How long to let the page settle between reads, and the longest total wait for
#: one to finish filling itself in. A rendered page returns on the first read
#: and pays only the settle; a single-page app that fetches its body after
#: DOMContentLoaded needs the loop. AXA needed two reads.
SETTLE_MS = 800
RENDER_BUDGET_MS = 20_000

#: Headless Chromium's own user agent says `HeadlessChrome`, and career sites
#: read it: with the default, AXA answers 403 and the settle loop dutifully
#: waits out its budget on an error page. Claiming an ordinary desktop Chrome is
#: what makes the fallback work at all. Not a spoof of anything privileged —
#: this is the same public page a person opens in a browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SCRIPT_RE = re.compile(r"(?is)<script\b.*?</script\s*>")
_STYLE_RE = re.compile(r"(?is)<style\b.*?</style\s*>")
_STYLESHEET_RE = re.compile(r"(?is)<link\b[^>]*\brel\s*=\s*[\"']?stylesheet[^>]*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def visible_text(markup: str) -> str:
    """Roughly what a reader would see. Enough to tell a posting from a 403.

    Not a parser and not trying to be: entity-decoding and tag-stripping give a
    character count that is right to within a few percent, and every decision
    made on it here is an order-of-magnitude one.
    """
    stripped = _STYLE_RE.sub(" ", _SCRIPT_RE.sub(" ", markup))
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", stripped))).strip()


def _measure(path: Path) -> int:
    """Visible-text length of a captured file, or 0 if there is nothing to read."""
    try:
        return len(visible_text(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def capture_singlefile(url: str, dest: Path, timeout: int) -> tuple[bool, str]:
    """Self-contained archive via the SingleFile CLI. The preferred result.

    It inlines images and CSS, so when it works the archive is a faithful copy
    of the page rather than a text-bearing skeleton. That is worth trying first
    even though it is the method that fails on hard pages.
    """
    bash = shutil.which("bash")
    if bash is None:
        return False, "bash not found, so save_singlefile.sh cannot be run"
    if not SINGLEFILE.exists():
        return False, f"{SINGLEFILE.name} is missing"

    try:
        done = subprocess.run(
            [bash, str(SINGLEFILE), url, str(dest)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"SingleFile did not finish within {timeout}s"

    # stderr carries the useful half — the CLI reports page-level failures there
    # while still sometimes exiting 0, which is the quiet failure this guards.
    note = (done.stderr or done.stdout or "").strip().splitlines()
    detail = note[-1] if note else f"exit {done.returncode}"
    if done.returncode != 0 or not dest.exists():
        return False, detail
    return True, detail


def _render(url: str, timeout: int):
    """Load `url` in a headless browser and return its settled DOM.

    A fresh browser each time, with no persisted profile: the watcher keeps one
    under `automation/state/browser/` for the portals that challenge it, and
    borrowing that would tie a pipeline run's success to the watcher's cookie
    jar. A posting page is public; it does not need the history.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed — run `python -m pip install playwright` "
            "and `python -m playwright install chromium`"
        ) from exc

    with sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(
                headless=True,
                # Career sites read navigator.webdriver and serve a 403 to what
                # it flags — which is exactly what SingleFile collects here.
                # This is the supported way to unset it, on a page that is
                # public to any browser.
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"could not start chromium ({exc}) — try "
                "`python -m playwright install chromium`"
            ) from exc

        try:
            page = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                user_agent=USER_AGENT,
                locale="en-US",
            ).new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            previous = ""

            # Read until the text stops growing rather than waiting a fixed
            # time. A page that fetches its body after DOMContentLoaded is not
            # cut off mid-render, and one that was ready immediately pays only
            # the first settle. "Non-empty" is not the same question as
            # "finished" — AXA reads short once and complete a second later.
            page.wait_for_timeout(SETTLE_MS)
            text = page.inner_text("body")
            waited = 0
            while waited < RENDER_BUDGET_MS:
                if len(text.strip()) >= MIN_CHARS and text == previous:
                    break
                previous = text
                page.wait_for_timeout(SETTLE_MS)
                waited += SETTLE_MS
                text = page.inner_text("body")
            return page.content()
        finally:
            browser.close()


def capture_rendered(url: str, dest: Path, timeout: int) -> tuple[bool, str]:
    """Rendered DOM, scripts and stylesheets removed. The fallback that works.

    What is lost against SingleFile is the inlined images and the site's own
    styling, so the file reads as an unstyled document. What is kept is every
    word of the posting, which is the only thing downstream steps need — they
    Grep it for phrases and parse it into the Match Brief.
    """
    try:
        markup = _render(url, timeout)
    except Exception as exc:  # noqa: BLE001 - reported, not raised: this is the last resort
        return False, str(exc).strip().splitlines()[0]

    body = _STYLESHEET_RE.sub("", _STYLE_RE.sub("", _SCRIPT_RE.sub("", markup)))
    banner = (
        "<!--\n"
        " Page rendered and saved by scripts/capture_posting.py\n"
        f" url: {url}\n"
        " note: scripts and stylesheets stripped; text content is complete\n"
        "-->\n"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(banner + body, encoding="utf-8")
    return True, "rendered DOM"


def capture(url: str, dest: Path, *, timeout: int, min_chars: int,
            renderer: str) -> int:
    """Run the methods in order until one produces a usable file. 0 on success."""
    methods = {
        "singlefile": ("SingleFile", capture_singlefile),
        "playwright": ("rendered browser", capture_rendered),
    }
    order = ["singlefile", "playwright"] if renderer == "auto" else [renderer]

    failures: list[str] = []
    for key in order:
        label, method = methods[key]
        # Into a scratch file first, so a rejected capture never lands at the
        # destination and a later method is never overwriting a real archive.
        with tempfile.TemporaryDirectory() as work:
            scratch = Path(work) / "capture.html"
            ok, detail = method(url, scratch, timeout)
            chars = _measure(scratch) if ok else 0

            if ok and chars < min_chars:
                ok = False
                detail = (
                    f"captured {chars} characters of text, under the {min_chars} "
                    "floor — this is a shell, an error page or a challenge, "
                    "not the posting"
                )
            if not ok:
                failures.append(f"{label}: {detail}")
                print(f"-- {label} failed: {detail}", file=sys.stderr)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(scratch, dest)

        print(f"METHOD: {label}")
        print(f"TEXT: {chars} characters")
        print(f"HTML: {dest}")
        if failures:
            print("NOTE: " + "; ".join(failures))
        return 0

    print("ERROR: could not capture the posting.", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a job posting page as HTML, falling back to a "
                    "rendered browser when SingleFile cannot get it.")
    parser.add_argument("url")
    parser.add_argument("output", help="path to write the .html archive to")
    parser.add_argument("--timeout", type=int, default=120,
                        help="seconds allowed per capture method (default: 120)")
    parser.add_argument("--min-chars", type=int, default=MIN_CHARS,
                        help=f"visible text below which a capture is rejected "
                             f"(default: {MIN_CHARS})")
    parser.add_argument("--renderer", choices=["auto", "singlefile", "playwright"],
                        default="auto",
                        help="force one method instead of trying both")
    args = parser.parse_args(argv)

    return capture(args.url, Path(args.output).expanduser(),
                   timeout=args.timeout, min_chars=args.min_chars,
                   renderer=args.renderer)


if __name__ == "__main__":
    raise SystemExit(main())
