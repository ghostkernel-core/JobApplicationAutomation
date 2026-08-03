"""One shared Playwright browser for the fragile portals.

hiring.cafe and StepStone have no public API and sit behind Cloudflare. Their
internal search endpoints return clean JSON, but only to a request that carries
a real browser's TLS fingerprint, headers, and cookies — `requests` gets a
challenge page. So the request is issued *from inside* the page via
`page.request.post()`, which reuses the context's cookies and identity.

Two things make this bearable rather than a maintenance sink:

**One persistent context, reused.** `state/browser/` holds the profile, so a
challenge solved on Monday is still solved on Friday. Launching a fresh browser
per poll would re-trigger the challenge every 30 minutes and look exactly like
the bot it is.

**Playwright is a soft dependency.** The import is lazy and failure is an
ordinary source failure, so a machine without Playwright installed runs the ATS
tier and Arbeitsagentur normally and disables these two after
`failures_before_disable` — it does not fail to boot. That is the whole reason
this module exists instead of a module-level `from playwright... import`.

These scrapers will break. That is not a defect to be designed away; it is why
both are `fragile = true`, why the failure path is a single notification rather
than a retry loop, and why nothing else in the pipeline depends on them.
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
from typing import Any

from ..config import BROWSER_PROFILE_DIR
from .base import USER_AGENT

log = logging.getLogger("watcher.fetch.browser")

LAUNCH_TIMEOUT_MS = 60_000

# Playwright's sync API is greenlet-based and bound to the thread that created
# it: touching a context from any other thread raises "Cannot switch to a
# different thread". The watcher dispatches each poll with
# `asyncio.to_thread(poll_once, ...)`, which draws from the default executor
# pool, so consecutive polls generally land on *different* threads. A cached
# context therefore worked on the first poll and raised on every one after it,
# until both browser-backed sources hit failures_before_disable and switched
# themselves off.
#
# A lock does not fix this. Serialising access still lets the work migrate
# between threads; what Playwright requires is that it never move at all. So
# the browser lives on one dedicated thread that owns it for the life of the
# process, and every public function here hands that thread a closure and waits
# for the result. One worker also means one operation at a time, which is what
# the old lock was for.
_WORK: queue.Queue | None = None
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()

# How often a waiting caller re-checks that the worker is still alive. Only
# affects how fast a dead worker is noticed, not throughput.
_LIVENESS_POLL_S = 0.5


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or the browser would not start."""


def _open_context(state: dict[str, Any]):
    """The shared persistent context, launched on first use.

    Only ever called on the worker thread, so everything it creates belongs to
    that thread for good.
    """
    if state["context"] is not None:
        return state["context"]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "playwright is not installed — run "
            "`.venv\\Scripts\\python.exe -m pip install playwright` and "
            "`.venv\\Scripts\\python.exe -m playwright install chromium`"
        ) from exc

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=True,
            user_agent=USER_AGENT,
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1440, "height": 900},
            args=[
                # Cloudflare reads navigator.webdriver; this is the supported
                # way to unset it and is not a defeat of any access control —
                # the endpoints below are the same ones the public site calls.
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
    except Exception as exc:  # noqa: BLE001
        playwright.stop()
        raise BrowserUnavailable(f"could not launch chromium: {exc}") from exc

    context.set_default_timeout(LAUNCH_TIMEOUT_MS)
    state["playwright"] = playwright
    state["context"] = context
    log.info("browser context started at %s", BROWSER_PROFILE_DIR)
    return context


def _shutdown(state: dict[str, Any]) -> None:
    context, playwright = state["context"], state["playwright"]
    state["context"] = state["playwright"] = None
    for closer in (getattr(context, "close", None), getattr(playwright, "stop", None)):
        if closer is None:
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


def _serve(work: queue.Queue) -> None:
    """The worker loop. Owns the Playwright objects from birth to shutdown."""
    state: dict[str, Any] = {"playwright": None, "context": None}
    try:
        while True:
            job = work.get()
            if job is None:  # shutdown sentinel
                return
            func, box, done = job
            try:
                box["value"] = func(_open_context(state))
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                box["error"] = exc
            finally:
                done.set()
    finally:
        _shutdown(state)


def _submit(func):
    """Run `func(context)` on the browser thread and return its result here.

    Exceptions are re-raised in the calling thread, so callers see the same
    behaviour as a direct call.
    """
    global _WORK, _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORK = queue.Queue()
            _WORKER = threading.Thread(target=_serve, args=(_WORK,),
                                       name="playwright-owner", daemon=True)
            _WORKER.start()
        work, worker = _WORK, _WORKER

    box: dict[str, Any] = {}
    done = threading.Event()
    work.put((func, box, done))

    # Wait, but not forever: if the worker dies mid-job nothing would ever set
    # the event, and a hung poll is harder to diagnose than a failed one.
    while not done.wait(_LIVENESS_POLL_S):
        if not worker.is_alive():
            raise BrowserUnavailable("browser worker thread died")

    if "error" in box:
        raise box["error"]
    return box["value"]


def close() -> None:
    """Shut the browser down. Idempotent; registered with atexit."""
    global _WORK, _WORKER
    with _WORKER_LOCK:
        work, worker = _WORK, _WORKER
        _WORK = _WORKER = None
    if work is None or worker is None or not worker.is_alive():
        return
    work.put(None)
    # The context teardown talks to a real browser process; give it room, but
    # never block interpreter exit on it. The thread is a daemon either way.
    worker.join(timeout=20)


atexit.register(close)


def _warm(page, url: str) -> None:
    """Load the real site once so the context holds its cookies.

    An XHR to a search endpoint from a context that has never visited the site
    has no session cookie and no Referer that makes sense, which is the fastest
    possible way to be challenged.
    """
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)


def json_api(
    site_url: str,
    api_url: str,
    payload: dict[str, Any] | None = None,
    *,
    method: str = "POST",
    timeout: int = 30,
    headers: dict[str, str] | None = None,
) -> Any:
    """Call a site's own JSON endpoint from inside a page on that site.

    Raises on anything that is not JSON — a Cloudflare challenge returns HTML
    with a 200, and silently decoding that to nothing would look exactly like
    "this search had no results", which is the failure mode that lets a broken
    scraper go unnoticed for a month.
    """
    def job(context):
        page = context.new_page()
        try:
            _warm(page, site_url)
            request_headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": site_url.rstrip("/"),
                "Referer": site_url,
                **(headers or {}),
            }
            options: dict[str, Any] = {
                "headers": request_headers,
                "timeout": timeout * 1000,
            }
            if payload is not None:
                options["data"] = payload
            if method.upper() == "GET":
                response = page.request.get(api_url, **options)
            else:
                response = page.request.post(api_url, **options)

            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status} from {api_url}")
            body = response.text()
            content_type = (response.headers or {}).get("content-type", "")
            if "json" not in content_type.lower():
                snippet = body[:120].replace("\n", " ")
                raise RuntimeError(
                    f"expected JSON from {api_url}, got "
                    f"{content_type or 'no content-type'}: {snippet}"
                )
            import json as _json

            return _json.loads(body)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    return _submit(job)


def evaluate(url: str, script: str, timeout: int = 30) -> Any:
    """Load a page and evaluate one JS expression against it.

    Exists so callers never have to reach for the browser thread or the context
    themselves — every entry point here must go through `_submit`, or the
    Playwright objects get touched from the wrong thread.
    """
    def job(context):
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            return page.evaluate(script)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    return _submit(job)


def page_text(url: str, wait_selector: str | None = None, timeout: int = 30) -> str:
    """Rendered visible text of a page. Used for hydrating a posting body."""
    def job(context):
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=timeout * 1000)
                except Exception:  # noqa: BLE001 - selector drift is expected
                    pass
            page.wait_for_timeout(800)
            return page.inner_text("body")
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    return _submit(job)


def available() -> bool:
    """Whether the browser tier can run at all, without launching it."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True
