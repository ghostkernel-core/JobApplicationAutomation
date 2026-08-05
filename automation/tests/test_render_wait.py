"""Reading a page that has not finished rendering itself.

`page_text` settled for a fixed moment after DOMContentLoaded and read the body
once. That is right for a server-rendered ad and wrong for Workday, BambooHR and
HiBob, which serve an empty shell and fill it from an XHR a second or three
later — the read landed on the blank page and returned *nothing*, and `hydrate`
fell back to the search-tile teaser without anything looking like a failure.
Nothing was logged, because nothing had raised.

hiring.cafe surfaces employers' own ATS pages rather than its own, so it meets
these worst: 15 of its first 85 postings were scored on a teaser, and the
resulting scores sat below the notify cut. That is the whole of "hiring.cafe
never sends me anything".

The measured shape of the problem, from the four pages that were failing:

    Rabobank   0 → 293 → 293                    (short, but genuinely short)
    Nxp        0 → 4879 → 6931 → 6931
    Qogita     0 → 0 → 4446 → 4446
    Capita     0 → 0 → 906 → 12106 → 12106

Capita is why the test is "stopped growing" and not "non-empty": a read one
tick earlier would have captured 906 characters of half-drawn page and looked
like a success.
"""

from __future__ import annotations

import pytest

from watcher.fetchers import browser
from watcher.fetchers.browser import (RENDER_BUDGET_MS, SETTLE_MS,
                                      SHELL_CHARS, page_text)


class FakePage:
    """A page whose body text follows a script, one entry per read.

    The last entry repeats for ever, which is what a finished page does.
    """

    def __init__(self, bodies: list[str]) -> None:
        self._bodies = bodies
        self.reads = 0
        self.waited = 0
        self.selectors: list[str] = []
        self.closed = False

    def goto(self, url, wait_until=None, timeout=None) -> None:
        pass

    def wait_for_selector(self, selector, timeout=None) -> None:
        self.selectors.append(selector)

    def wait_for_timeout(self, ms: int) -> None:
        self.waited += ms

    def inner_text(self, _selector: str) -> str:
        index = min(self.reads, len(self._bodies) - 1)
        self.reads += 1
        return self._bodies[index]

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def page(monkeypatch):
    """Run `page_text`'s job against a scripted page instead of a browser."""
    made: list[FakePage] = []

    def run(bodies: list[str]) -> tuple[str, FakePage]:
        fake = FakePage(bodies)
        made.append(fake)

        class Context:
            def new_page(self):
                return fake

        monkeypatch.setattr(browser, "_submit", lambda job: job(Context()))
        return page_text("https://example.com/job/1"), fake

    return run


def _text(length: int, fill: str = "x") -> str:
    return fill * length


def test_a_rendered_page_is_read_once_and_costs_nothing_extra(page) -> None:
    """The common case must not pay for the awkward one."""
    text, fake = page([_text(8391)])

    assert len(text) == 8391
    assert fake.reads == 1
    assert fake.waited == SETTLE_MS


def test_a_shell_that_fills_in_later_is_waited_for(page) -> None:
    text, fake = page(["", _text(4879), _text(6931)])

    assert len(text) == 6931
    assert fake.reads > 1


def test_a_half_drawn_page_is_not_taken_for_a_finished_one(page) -> None:
    """The Capita case: 906 characters, then 12106 a tick later."""
    text, _ = page(["", "", _text(906, "a"), _text(12106, "b")])

    assert len(text) == 12106


def test_a_short_page_that_is_simply_short_returns_promptly(page) -> None:
    """Rabobank settles at 293 characters and never grows. Believe it."""
    text, fake = page(["", _text(293)])

    assert len(text) == 293
    assert fake.waited < RENDER_BUDGET_MS


def test_a_page_that_never_renders_gives_up_inside_the_budget(page) -> None:
    """Two equal empty reads are not a finished page — that must not end it."""
    text, fake = page([""])

    assert text == ""
    assert fake.waited <= SETTLE_MS + RENDER_BUDGET_MS


def test_a_page_stuck_below_the_shell_threshold_is_not_mistaken_for_done(
        page) -> None:
    text, fake = page([_text(SHELL_CHARS - 1)])

    assert fake.reads > 1


def test_the_page_is_closed_even_though_it_never_rendered(page) -> None:
    _, fake = page([""])

    assert fake.closed
