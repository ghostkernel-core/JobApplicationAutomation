"""One shared Playwright browser for the fragile portals.

hiring.cafe and StepStone have no public API and sit behind Cloudflare. Their
internal search endpoints return clean JSON, but only to a request that carries
a real browser's TLS fingerprint, headers, and cookies — `requests` gets a
challenge page. So the request is issued *from inside* the page via
`page.request.post()`, which reuses the context's cookies and identity.

Three things make this bearable rather than a maintenance sink:

**One persistent context, reused.** `state/browser/` holds the profile, so a
challenge solved on Monday is still solved on Friday. Launching a fresh browser
per poll would re-trigger the challenge every 30 minutes and look exactly like
the bot it is.

**A dead browser heals itself.** That cached context is a Python handle, and it
outlives the Chromium process behind it. When that process died in an idle
window between two polls, the cache stayed populated, every later `new_page()`
raised, and nothing but restarting the watcher cleared it — both portals share
the context, so one death burned two independent health budgets in lockstep and
took the two most productive sources down for seven consecutive cycles. So the
cache is checked for liveness before it is handed out, and a job whose browser
dies under it relaunches and retries once.

**Playwright is a soft dependency.** The import is lazy and failure is an
ordinary source failure, so a machine without Playwright installed runs the ATS
tier and Arbeitsagentur normally and disables these two after
`failures_before_disable` — it does not fail to boot. That is the whole reason
this module exists instead of a module-level `from playwright... import`.

These scrapers will break. That is not a defect to be designed away; it is why
both are `fragile = true`, why the failure path is a single notification and an
hourly retry rather than a retry-every-poll loop, and why nothing else in the
pipeline depends on them.
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from typing import Any

from ..config import browser_profile_dir
from .base import USER_AGENT

log = logging.getLogger("watcher.fetch.browser")

LAUNCH_TIMEOUT_MS = 60_000

#: After a launch fails, how long before another is attempted. One poll issues
#: ~50 hydration calls; without this, each of them spawns its own Chromium
#: launch attempt on a machine where the browser cannot start.
RELAUNCH_COOLDOWN_S = 60

#: How long an origin stays warm. Cookies live in the persistent profile, so
#: the navigation only has to run often enough to keep a session fresh — not
#: once per call, which is what made hiring.cafe reload its homepage 12 times
#: and burn ~18s of fixed waits every poll.
WARM_TTL_S = 600

#: origin -> monotonic timestamp of its last warm. Only the single owning
#: worker thread ever reads or writes this, so it needs no lock — which is not
#: obvious from the type, hence the note.
_WARMED: dict[str, float] = {}

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


# Playwright 1.62 exports only `Error` and `TimeoutError` from `sync_api`, so
# there is no `TargetClosedError` to catch by type — and importing from
# `playwright._impl` to get one would make this module's import hard, which the
# docstring above exists to prevent. Match on the type *name* and the message
# instead: both are stable, and a false positive costs one wasted relaunch.
_DISCONNECT_TYPES = ("TargetClosedError", "BrowserClosedError")
_DISCONNECT_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
    "browser closed",
)


def _is_disconnect(exc: BaseException) -> bool:
    """Whether an exception means the browser process is gone."""
    if type(exc).__name__ in _DISCONNECT_TYPES:
        return True
    text = str(exc).casefold()
    return any(marker in text for marker in _DISCONNECT_MARKERS)


def _context_is_dead(state: dict[str, Any]) -> bool:
    """Whether the cached context still has a browser behind it.

    Two signals, because neither is enough on its own. The `close` event fires
    when the browser goes away while nothing is being asked of it — the case
    that stranded the watcher for seven poll cycles — but Playwright's sync API
    only dispatches events when it is called into, so it can lag. The
    `is_connected()` probe catches what the flag missed.

    A persistent context may expose no `.browser` at all. In that case, reading
    its channel-backed `pages` property is a non-mutating liveness probe: an
    empty page list is fine, but an exception means the handle is unusable. A
    minimal test fake need expose neither surface and still counts as alive.
    """
    context = state.get("context")
    if context is None:
        return False
    if state.get("dead"):
        return True
    try:
        owner = getattr(context, "browser", None)
        if owner is not None:
            return not owner.is_connected()
        getattr(context, "pages", None)
        return False
    except Exception:  # noqa: BLE001 - an unusable handle is a dead one
        return True


def _open_context(state: dict[str, Any]):
    """The shared persistent context, launched on first use and after a death.

    Only ever called on the worker thread, so everything it creates belongs to
    that thread for good.
    """
    if state["context"] is not None:
        if not _context_is_dead(state):
            return state["context"]
        # The handle outlives the process it points at, so without this the
        # cache stays populated and every later `new_page()` raises — forever.
        log.warning("browser context is gone — relaunching")
        _shutdown(state)

    failed_at = state.get("launch_failed_at")
    if failed_at is not None and time.monotonic() - failed_at < RELAUNCH_COOLDOWN_S:
        raise BrowserUnavailable(state.get("launch_error") or "browser launch failed")

    try:
        return _launch(state)
    except BrowserUnavailable as exc:
        state["launch_failed_at"] = time.monotonic()
        state["launch_error"] = str(exc)
        raise


def _launch(state: dict[str, Any]):
    """Start a fresh persistent context and record it on `state`."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "playwright is not installed — run "
            "`.venv\\Scripts\\python.exe -m pip install playwright` and "
            "`.venv\\Scripts\\python.exe -m playwright install chromium`"
        ) from exc

    profile = browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
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
    state["dead"] = False
    state["launch_failed_at"] = state["launch_error"] = None

    # Set the flag the moment the browser goes away, so a death during the 30
    # minutes between polls is already known by the time the next one asks.
    # Guarded because the fakes in the tests are not full contexts.
    register = getattr(context, "on", None)
    if register is not None:
        register("close", lambda *_args: state.__setitem__("dead", True))

    log.info("browser context started at %s", profile)
    return context


def _shutdown(state: dict[str, Any]) -> None:
    context, playwright = state["context"], state["playwright"]
    state["context"] = state["playwright"] = None
    state["dead"] = False
    # Whatever was warm belonged to the context being torn down.
    _WARMED.clear()
    for closer in (getattr(context, "close", None), getattr(playwright, "stop", None)):
        if closer is None:
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


def _run_job(state: dict[str, Any], func):
    """Run one job, relaunching and retrying once if the browser died under it.

    The liveness check in `_open_context` runs before a job starts, so it can
    do nothing for a browser that dies *during* one. Every job in this module
    is a read — `goto`, `evaluate`, and a search `GET`/`POST` — so running one
    twice changes nothing on the far side, which is what makes a retry safe
    here and would not make it safe somewhere that writes.

    Only a context that was *already cached* is retried. If one launched for
    this very job reports itself closed, relaunching into the same fault would
    just double the cost of every real error.
    """
    cached = state.get("context")
    context = _open_context(state)
    try:
        return func(context)
    except Exception as exc:  # noqa: BLE001
        if cached is None or context is not cached or not _is_disconnect(exc):
            raise
        log.warning("browser died mid-job (%s) — relaunching and retrying once", exc)
        _shutdown(state)
        return func(_open_context(state))


def _serve(work: queue.Queue) -> None:
    """The worker loop. Owns the Playwright objects from birth to shutdown."""
    state: dict[str, Any] = {"playwright": None, "context": None, "dead": False}
    try:
        while True:
            job = work.get()
            if job is None:  # shutdown sentinel
                return
            func, box, done = job
            try:
                box["value"] = _run_job(state, func)
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
    """Load the real site so the context holds its cookies — at most once per
    `WARM_TTL_S`.

    An XHR to a search endpoint from a context that has never visited the site
    has no session cookie and no Referer that makes sense, which is the fastest
    possible way to be challenged. That argues for warming, not for warming on
    every single call: the cookies live in the persistent profile and the
    request sets `Origin`/`Referer` explicitly, so a second warm inside the TTL
    buys a page load and a fixed 1500ms wait and nothing else. hiring.cafe
    issues 12 calls a poll and paid that 12 times. StepStone's `evaluate` path
    has never warmed at all and has never been challenged for it, which is the
    evidence that per-call warming was over-cautious rather than load-bearing.
    """
    now = time.monotonic()
    last = _WARMED.get(url)
    if last is not None and now - last < WARM_TTL_S:
        return
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    _WARMED[url] = now


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


#: How long to let a page settle after DOMContentLoaded before reading it, and
#: the interval between re-reads for one that was not ready by then.
SETTLE_MS = 800
#: Below this a page counts as an unrendered shell rather than a short posting.
#: The shortest real ad body seen is around 2900 characters, and the longest
#: search-tile teaser 476, so anything under a few hundred is markup and chrome.
SHELL_CHARS = 200
#: Longest total wait for a single-page app to fill itself in. Measured against
#: the pages that were failing: the slowest needed 3.2s, so this is roughly
#: double the worst observed case and still bounded well inside `timeout`.
RENDER_BUDGET_MS = 8000


def page_text(url: str, wait_selector: str | None = None, timeout: int = 30) -> str:
    """Rendered visible text of a page. Used for hydrating a posting body.

    Workday, BambooHR and HiBob serve an empty shell and fill it from an XHR
    well after DOMContentLoaded, so a single fixed settle lands on a blank page
    and returns *nothing at all* — 15 of the first 85 hiring.cafe postings were
    scored on the search-tile teaser for exactly this reason, and their scores
    came in below the notify cut as a result. hiring.cafe surfaces employers'
    own ATS pages, so it is the source that meets these worst.

    Re-reading until the text stops growing is what fixes them, rather than a
    longer fixed wait: a page that is already rendered returns on the first
    read and pays nothing, and one still assembling itself is not cut off
    mid-render. That second part matters — one page read 906 characters partway
    through and 12106 a second later, so "non-empty" is not the same question
    as "finished".
    """
    def job(context):
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=timeout * 1000)
                except Exception:  # noqa: BLE001 - selector drift is expected
                    pass
            page.wait_for_timeout(SETTLE_MS)
            text = page.inner_text("body")
            if len(text.strip()) >= SHELL_CHARS:
                return text

            waited = 0
            while waited < RENDER_BUDGET_MS:
                page.wait_for_timeout(SETTLE_MS)
                waited += SETTLE_MS
                previous, text = text, page.inner_text("body")
                # Two equal reads mean the XHR has landed and the page is done.
                # A page that never gets there — a login wall, a posting taken
                # down — runs out the budget and returns whatever it had, which
                # leaves the caller exactly where it is today.
                if len(text.strip()) >= SHELL_CHARS and text == previous:
                    break
            return text
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
