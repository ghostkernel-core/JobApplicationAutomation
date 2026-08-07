"""The shared browser: one owning thread, and JSON fetched from inside a page.

Playwright's sync API is greenlet-based and bound to the thread that created it,
so touching a context from anywhere else raises "Cannot switch to a different
thread". The watcher dispatches each poll with `asyncio.to_thread`, which draws
from a pool, so consecutive polls land on different threads — a cached context
worked once and raised on every poll after it, until both browser-backed sources
counted their way to `failures_before_disable` and switched themselves off.

A lock does not fix that; the work still migrates. What Playwright needs is for
the objects never to move at all, which is why one dedicated thread owns them
and every entry point hands it a closure. **The thread-identity assertions below
are the point of this file** — they are what stops someone reintroducing a lock
and a cached context, which looks correct and passes any test that only checks
return values.

The second half covers the JSON path. A Cloudflare challenge is HTML with a 200,
so decoding it to nothing would read as "this search had no results" — the
failure mode that lets a broken scraper go unnoticed for a month.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from watcher.fetchers import browser


@pytest.fixture(autouse=True)
def no_leftover_worker():
    """Each test starts and ends with no browser thread of its own.

    `_WARMED` is module-level and the `inline` fixture never starts a worker to
    tear down, so it has to be cleared here or one test's warm silently
    suppresses the next one's.
    """
    browser.close()
    browser._WARMED.clear()
    yield
    browser.close()
    browser._WARMED.clear()


@pytest.fixture
def fake_context(monkeypatch):
    """A context the worker can hand out without launching Chromium."""
    context = types.SimpleNamespace(name="ctx")
    monkeypatch.setattr(browser, "_open_context", lambda _state: context)
    return context


# --------------------------------------------------------------------------
# the owning thread
# --------------------------------------------------------------------------

def test_a_job_gets_the_context_and_its_value_comes_back(fake_context) -> None:
    assert browser._submit(lambda ctx: f"ran on {ctx.name}") == "ran on ctx"


def test_an_exception_is_re_raised_in_the_calling_thread(fake_context) -> None:
    """Callers must see the same behaviour as a direct call, or every fetcher
    would need its own way of noticing a failure that happened elsewhere."""
    def boom(_ctx):
        raise ValueError("the page went away")

    with pytest.raises(ValueError, match="the page went away"):
        browser._submit(boom)


def test_every_job_runs_on_the_same_thread(fake_context) -> None:
    """The whole design. A pool, a lock, or a fresh thread per call all break
    Playwright's greenlet binding, and all three look fine from the outside."""
    threads = {browser._submit(lambda _ctx: threading.get_ident())
               for _ in range(8)}

    assert len(threads) == 1


def test_that_thread_is_not_the_caller(fake_context) -> None:
    assert browser._submit(lambda _ctx: threading.get_ident()) \
        != threading.get_ident()


def test_the_worker_survives_a_job_that_raised(fake_context) -> None:
    """One failed hydration must not cost the rest of the poll."""
    def boom(_ctx):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        browser._submit(boom)

    assert browser._submit(lambda _ctx: "still here") == "still here"


def test_the_context_is_opened_once_and_reused(monkeypatch) -> None:
    """A fresh browser per poll re-triggers the challenge every 30 minutes."""
    opened = []

    def open_context(state):
        if state.get("context") is None:
            state["context"] = types.SimpleNamespace(name=f"ctx{len(opened)}")
            opened.append(state["context"])
        return state["context"]

    monkeypatch.setattr(browser, "_open_context", open_context)

    names = {browser._submit(lambda ctx: ctx.name) for _ in range(5)}

    assert names == {"ctx0"} and len(opened) == 1


def test_a_worker_that_died_is_replaced_on_the_next_call(fake_context) -> None:
    browser._submit(lambda _ctx: None)
    browser.close()

    assert browser._submit(lambda _ctx: "restarted") == "restarted"


def test_a_caller_waiting_on_a_dead_worker_is_not_left_hanging(
        fake_context, monkeypatch) -> None:
    """Nothing would ever set the event, and a hung poll is harder to diagnose
    than a failed one."""
    monkeypatch.setattr(browser, "_LIVENESS_POLL_S", 0.01)
    browser._submit(lambda _ctx: None)

    work = browser._WORK
    deliver = work.put
    monkeypatch.setattr(work, "put", lambda _item: None)  # swallow the next job
    deliver(None)                                         # ...and stop the worker

    with pytest.raises(browser.BrowserUnavailable, match="worker thread died"):
        browser._submit(lambda _ctx: "never runs")


def test_closing_without_ever_starting_is_a_no_op() -> None:
    browser.close()
    browser.close()


def test_closing_twice_is_a_no_op(fake_context) -> None:
    browser._submit(lambda _ctx: None)

    browser.close()
    browser.close()


def test_closing_shuts_the_worker_thread_down(fake_context) -> None:
    browser._submit(lambda _ctx: None)
    worker = browser._WORKER

    browser.close()

    worker.join(timeout=5)
    assert not worker.is_alive()


# --------------------------------------------------------------------------
# launching, and not being able to
# --------------------------------------------------------------------------

class FakePlaywright:
    def __init__(self, context=None, launch_error=None) -> None:
        self._context = context or types.SimpleNamespace(
            set_default_timeout=lambda _ms: None)
        self._launch_error = launch_error
        self.launch_kwargs: dict = {}
        self.stopped = False
        self.chromium = types.SimpleNamespace(
            launch_persistent_context=self._launch)

    def _launch(self, profile, **kwargs):
        self.launch_kwargs = dict(kwargs, profile=profile)
        if self._launch_error:
            raise self._launch_error
        return self._context

    def stop(self) -> None:
        self.stopped = True


def _install_playwright(monkeypatch, playwright) -> None:
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: types.SimpleNamespace(
        start=lambda: playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)


def test_the_context_launches_into_the_configured_profile(
        monkeypatch, tmp_path) -> None:
    """`state/browser/` is what keeps Monday's solved challenge solved on Friday."""
    playwright = FakePlaywright()
    _install_playwright(monkeypatch, playwright)
    profile = tmp_path / "browser"
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: profile)
    state: dict = {"playwright": None, "context": None}

    browser._open_context(state)

    assert playwright.launch_kwargs["profile"] == str(profile)
    assert profile.is_dir()
    assert playwright.launch_kwargs["headless"] is True
    assert playwright.launch_kwargs["user_agent"] == browser.USER_AGENT
    assert state["context"] is not None


def test_a_second_call_returns_the_context_already_open(
        monkeypatch, tmp_path) -> None:
    state = {"playwright": None, "context": "already here"}
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)

    assert browser._open_context(state) == "already here"


def test_a_machine_without_playwright_says_how_to_install_it(
        monkeypatch, tmp_path) -> None:
    """The import is soft on purpose: the ATS tier and Arbeitsagentur must run
    on a machine that has no browser, rather than failing to boot."""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)

    with pytest.raises(browser.BrowserUnavailable, match="pip install playwright"):
        browser._open_context({"playwright": None, "context": None})


def test_a_browser_that_will_not_start_is_reported_not_leaked(
        monkeypatch, tmp_path) -> None:
    """Leaving the driver process running would strand a Chromium per poll."""
    playwright = FakePlaywright(launch_error=RuntimeError("no display"))
    _install_playwright(monkeypatch, playwright)
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)

    with pytest.raises(browser.BrowserUnavailable, match="no display"):
        browser._open_context({"playwright": None, "context": None})

    assert playwright.stopped


def test_shutdown_closes_the_context_and_stops_the_driver() -> None:
    closed = []
    state = {
        "context": types.SimpleNamespace(close=lambda: closed.append("context")),
        "playwright": types.SimpleNamespace(stop=lambda: closed.append("driver")),
    }

    browser._shutdown(state)

    assert closed == ["context", "driver"]
    assert state["context"] is None and state["playwright"] is None


def test_shutdown_of_a_context_that_never_opened_does_nothing() -> None:
    browser._shutdown({"context": None, "playwright": None})


def test_shutdown_does_not_raise_when_the_browser_is_already_gone() -> None:
    """Interpreter exit races the browser process; a raise there is noise."""
    def boom():
        raise RuntimeError("already dead")

    browser._shutdown({"context": types.SimpleNamespace(close=boom),
                       "playwright": types.SimpleNamespace(stop=boom)})


def test_availability_is_answered_without_launching_anything() -> None:
    assert browser.available() is True


def test_availability_is_false_without_playwright(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)

    assert browser.available() is False


# --------------------------------------------------------------------------
# JSON from inside a page
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body="{}", content_type="application/json"):
        self.status = status
        self._body = body
        self.headers = {"content-type": content_type}

    def text(self) -> str:
        return self._body


class FakeRequest:
    def __init__(self, response) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url, **options):
        self.calls.append(("GET", url, options))
        return self._response

    def post(self, url, **options):
        self.calls.append(("POST", url, options))
        return self._response


class FakeApiPage:
    def __init__(self, response=None, evaluated=None) -> None:
        self.request = FakeRequest(response or FakeResponse())
        self.visited: list[str] = []
        self.scripts: list[str] = []
        self.waited = 0
        self.closed = False
        self._evaluated = evaluated

    def goto(self, url, wait_until=None, timeout=None) -> None:
        self.visited.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        self.waited += ms

    def evaluate(self, script):
        self.scripts.append(script)
        return self._evaluated

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def inline(monkeypatch):
    """Run a job against a scripted page instead of a browser."""
    def run(page: FakeApiPage) -> FakeApiPage:
        context = types.SimpleNamespace(new_page=lambda: page)
        monkeypatch.setattr(browser, "_submit", lambda job: job(context))
        return page

    return run


def test_the_site_is_loaded_before_its_api_is_called(inline) -> None:
    """An XHR from a context that never visited the site has no session cookie
    and no sensible Referer, which is the fastest possible way to be challenged."""
    page = inline(FakeApiPage(FakeResponse(body='{"ok": true}')))

    browser.json_api("https://hiringcafe.com/", "https://hiringcafe.com/api")

    assert page.visited == ["https://hiringcafe.com/"]
    assert page.waited > 0


def test_a_json_response_is_decoded(inline) -> None:
    inline(FakeApiPage(FakeResponse(body='{"hits": [1, 2]}')))

    assert browser.json_api("https://x/", "https://x/api") == {"hits": [1, 2]}


def test_the_call_posts_by_default_and_carries_the_payload(inline) -> None:
    page = inline(FakeApiPage(FakeResponse(body="{}")))

    browser.json_api("https://x/", "https://x/api", {"query": "data"})

    method, url, options = page.request.calls[0]
    assert method == "POST"
    assert options["data"] == {"query": "data"}


def test_a_get_carries_no_body(inline) -> None:
    page = inline(FakeApiPage(FakeResponse(body="{}")))

    browser.json_api("https://x/", "https://x/api", method="GET")

    method, _url, options = page.request.calls[0]
    assert method == "GET"
    assert "data" not in options


def test_the_request_looks_like_it_came_from_the_page_it_came_from(
        inline) -> None:
    page = inline(FakeApiPage(FakeResponse(body="{}")))

    browser.json_api("https://hiringcafe.com/", "https://hiringcafe.com/api")

    headers = page.request.calls[0][2]["headers"]
    assert headers["Origin"] == "https://hiringcafe.com"
    assert headers["Referer"] == "https://hiringcafe.com/"


def test_extra_headers_are_merged_over_the_defaults(inline) -> None:
    page = inline(FakeApiPage(FakeResponse(body="{}")))

    browser.json_api("https://x/", "https://x/api",
                     headers={"Accept": "application/vnd.custom"})

    assert page.request.calls[0][2]["headers"]["Accept"] == "application/vnd.custom"


def test_an_error_status_is_reported_with_its_code(inline) -> None:
    inline(FakeApiPage(FakeResponse(status=404, body="gone")))

    with pytest.raises(RuntimeError, match="HTTP 404"):
        browser.json_api("https://x/", "https://x/api")


def test_a_challenge_page_is_a_failure_not_an_empty_result(inline) -> None:
    """Cloudflare answers 200 with HTML. Decoding that to nothing is how a
    broken scraper goes unnoticed."""
    inline(FakeApiPage(FakeResponse(
        body="<html>Checking your browser…</html>", content_type="text/html")))

    with pytest.raises(RuntimeError, match="expected JSON"):
        browser.json_api("https://x/", "https://x/api")


def test_a_response_with_no_content_type_is_a_failure_too(inline) -> None:
    page = FakeApiPage(FakeResponse(body="nope"))
    page.request._response.headers = {}
    inline(page)

    with pytest.raises(RuntimeError, match="no content-type"):
        browser.json_api("https://x/", "https://x/api")


def test_the_page_is_closed_after_a_failed_call(inline) -> None:
    """A page per poll that is never closed is a leak that ends in a dead box."""
    page = inline(FakeApiPage(FakeResponse(status=500)))

    with pytest.raises(RuntimeError):
        browser.json_api("https://x/", "https://x/api")

    assert page.closed


def test_a_page_that_will_not_close_does_not_fail_the_call(inline) -> None:
    page = FakeApiPage(FakeResponse(body='{"ok": true}'))
    page.close = lambda: (_ for _ in ()).throw(RuntimeError("already gone"))
    inline(page)

    assert browser.json_api("https://x/", "https://x/api") == {"ok": True}


def test_evaluate_loads_the_page_and_runs_the_script(inline) -> None:
    page = inline(FakeApiPage(evaluated={"items": []}))

    result = browser.evaluate("https://x/jobs", "() => 1")

    assert page.visited == ["https://x/jobs"]
    assert page.scripts == ["() => 1"]
    assert result == {"items": []}


def test_evaluate_closes_the_page_when_the_script_throws(inline) -> None:
    page = FakeApiPage()
    page.evaluate = lambda _script: (_ for _ in ()).throw(RuntimeError("bad JS"))
    inline(page)

    with pytest.raises(RuntimeError, match="bad JS"):
        browser.evaluate("https://x/jobs", "() => boom")

    assert page.closed


def test_page_text_waits_for_the_selector_it_was_given(inline) -> None:
    page = FakeApiPage()
    page.inner_text = lambda _sel: "x" * 500
    seen: list[str] = []
    page.wait_for_selector = lambda sel, timeout=None: seen.append(sel)
    inline(page)

    browser.page_text("https://x/job/1", wait_selector="div.ad")

    assert seen == ["div.ad"]


def test_page_text_carries_on_when_the_selector_never_appears(inline) -> None:
    """Selector drift is expected; the body read is the thing that matters."""
    page = FakeApiPage()
    page.inner_text = lambda _sel: "x" * 500
    page.wait_for_selector = lambda sel, timeout=None: (
        _ for _ in ()).throw(RuntimeError("timeout"))
    inline(page)

    assert len(browser.page_text("https://x/job/1", wait_selector="div.ad")) == 500


# --------------------------------------------------------------------------
# surviving a browser that died
# --------------------------------------------------------------------------

class TargetClosedError(Exception):
    """Named for what Playwright raises.

    `playwright.sync_api` exports only `Error` and `TimeoutError`, so there is
    no real type to import and catch — the module matches on the name and the
    message, and this stands in for the real thing.
    """


class FakeBrowserProcess:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class FakeContext:
    def __init__(self, name: str = "ctx", process=None) -> None:
        self.name = name
        self.browser = process
        self.handlers: dict = {}
        self.closed = False

    def set_default_timeout(self, _ms) -> None:
        pass

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    def close(self) -> None:
        self.closed = True


def _relaunching_state(monkeypatch, first, *rest):
    """A worker state holding `first`, with `rest` queued for relaunches."""
    pending = list(rest)

    def open_context(state):
        if state.get("context") is None:
            state["context"] = pending.pop(0)
        return state["context"]

    monkeypatch.setattr(browser, "_open_context", open_context)
    return {"playwright": None, "context": first, "dead": False}, pending


def test_a_dead_context_is_torn_down_and_relaunched(monkeypatch, tmp_path) -> None:
    """The handle outlives the process, so a populated cache proves nothing."""
    dead = FakeContext("dead", FakeBrowserProcess(connected=False))
    fresh = FakeContext("fresh", FakeBrowserProcess(connected=True))
    _install_playwright(monkeypatch, FakePlaywright(context=fresh))
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)
    state = {"playwright": None, "context": dead}

    assert browser._open_context(state) is fresh
    assert dead.closed


def test_a_live_context_is_still_reused(monkeypatch, tmp_path) -> None:
    """The invariant the liveness check must not cost: relaunching per poll
    would re-trigger the Cloudflare challenge every 30 minutes, which is the
    entire reason the profile is persistent."""
    live = FakeContext("live", FakeBrowserProcess(connected=True))
    playwright = FakePlaywright()
    _install_playwright(monkeypatch, playwright)
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)
    state = {"playwright": None, "context": live}

    assert browser._open_context(state) is live
    assert not live.closed
    assert playwright.launch_kwargs == {}


def test_a_context_that_exposes_no_browser_counts_as_alive() -> None:
    """A persistent context need not expose `.browser`. Reading that as death
    would relaunch on every poll — the failure this fix must not introduce."""
    assert browser._context_is_dead({"context": FakeContext("persistent")}) is False


def test_a_browser_that_went_away_while_idle_is_noticed(
        monkeypatch, tmp_path) -> None:
    """The actual outage: Chromium died between two polls, with nothing in
    flight to raise and 30 minutes before anyone asked again."""
    context = FakeContext("ctx", FakeBrowserProcess(connected=True))
    _install_playwright(monkeypatch, FakePlaywright(context=context))
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)
    state: dict = {"playwright": None, "context": None}
    browser._open_context(state)

    context.handlers["close"]()

    assert browser._context_is_dead(state) is True


def test_a_job_that_hits_a_dead_browser_relaunches_and_retries(monkeypatch) -> None:
    """Every job here is a read, so running one twice is safe."""
    state, _pending = _relaunching_state(
        monkeypatch, FakeContext("first"), FakeContext("second"))
    seen: list[str] = []

    def job(ctx):
        seen.append(ctx.name)
        if ctx.name == "first":
            raise TargetClosedError(
                "Target page, context or browser has been closed")
        return f"ran on {ctx.name}"

    assert browser._run_job(state, job) == "ran on second"
    assert seen == ["first", "second"]


def test_an_ordinary_failure_does_not_relaunch(monkeypatch) -> None:
    """Retrying everything would double the cost of every real error."""
    state, pending = _relaunching_state(
        monkeypatch, FakeContext("first"), FakeContext("second"))
    seen: list[str] = []

    def job(ctx):
        seen.append(ctx.name)
        raise RuntimeError("HTTP 500 from the search endpoint")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        browser._run_job(state, job)

    assert seen == ["first"]
    assert len(pending) == 1  # nothing was relaunched


def test_a_retry_that_fails_again_surfaces_the_second_error(monkeypatch) -> None:
    """One retry, not a loop."""
    state, _pending = _relaunching_state(
        monkeypatch, FakeContext("first"), FakeContext("second"))
    seen: list[str] = []

    def job(ctx):
        seen.append(ctx.name)
        if ctx.name == "first":
            raise TargetClosedError("target closed")
        raise RuntimeError("still broken")

    with pytest.raises(RuntimeError, match="still broken"):
        browser._run_job(state, job)

    assert seen == ["first", "second"]


def test_a_launch_that_just_failed_is_not_attempted_again(
        monkeypatch, tmp_path) -> None:
    """One poll issues ~50 hydration calls. Without the cooldown, a machine
    whose browser cannot start pays 50 launch attempts for one failure."""
    _install_playwright(monkeypatch,
                        FakePlaywright(launch_error=RuntimeError("no display")))
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)
    state: dict = {"playwright": None, "context": None}

    with pytest.raises(browser.BrowserUnavailable, match="no display"):
        browser._open_context(state)

    second = FakePlaywright(launch_error=RuntimeError("no display"))
    _install_playwright(monkeypatch, second)

    with pytest.raises(browser.BrowserUnavailable, match="no display"):
        browser._open_context(state)

    assert second.launch_kwargs == {}


def test_the_launch_cooldown_expires(monkeypatch, tmp_path) -> None:
    """A cooldown, not a latch — a browser that comes back must be picked up."""
    _install_playwright(monkeypatch,
                        FakePlaywright(launch_error=RuntimeError("no display")))
    monkeypatch.setattr(browser, "browser_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(browser, "RELAUNCH_COOLDOWN_S", 0)
    state: dict = {"playwright": None, "context": None}

    with pytest.raises(browser.BrowserUnavailable):
        browser._open_context(state)

    recovered = FakeContext("recovered")
    _install_playwright(monkeypatch, FakePlaywright(context=recovered))

    assert browser._open_context(state) is recovered


def test_the_site_is_not_re_warmed_inside_the_ttl(inline) -> None:
    """hiring.cafe issues 12 calls a poll and paid a page load plus a fixed
    1500ms wait on every one of them."""
    page = inline(FakeApiPage(FakeResponse(body="{}")))

    browser.json_api("https://x/", "https://x/api")
    browser.json_api("https://x/", "https://x/api")

    assert page.visited == ["https://x/"]


def test_an_expired_warm_is_done_again(inline, monkeypatch) -> None:
    monkeypatch.setattr(browser, "WARM_TTL_S", 0)
    page = inline(FakeApiPage(FakeResponse(body="{}")))

    browser.json_api("https://x/", "https://x/api")
    browser.json_api("https://x/", "https://x/api")

    assert page.visited == ["https://x/", "https://x/"]


def test_a_relaunched_context_is_warmed_again(inline) -> None:
    """Whatever was warm belonged to the context that just went away."""
    page = inline(FakeApiPage(FakeResponse(body="{}")))
    browser.json_api("https://x/", "https://x/api")

    browser._shutdown({"context": None, "playwright": None})
    browser.json_api("https://x/", "https://x/api")

    assert page.visited == ["https://x/", "https://x/"]


def test_a_dead_browser_does_not_park_the_source() -> None:
    """`poll.py` parks a source only on `StructuralError`. A browser outage is
    infrastructure, not a shape change, so it must stay eligible for the
    cooldown probe that brings the source back without anyone noticing. Load-
    bearing and otherwise untested, which is how a refactor would lose it."""
    from watcher.fetchers.base import StructuralError

    assert issubclass(browser.BrowserUnavailable, RuntimeError)
    assert not issubclass(browser.BrowserUnavailable, StructuralError)
