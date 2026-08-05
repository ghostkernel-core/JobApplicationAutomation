"""The evening digest fits inside Telegram's message limit, and drains.

The digest is unbounded by nature: it carries the whole mid-band plus every
high scorer the six-per-poll instant cap held back. Telegram rejects anything
over 4096 characters outright rather than trimming it, and the old formatter
built one message from the whole band with no cap.

That made the failure self-perpetuating rather than transient. The send raised
before any posting was recorded as notified, so nothing drained; the next
night's band was larger still. It had been dead for two nights at 21812
characters — 5.3x the limit — holding 94 postings.

Two properties keep it dead-simple to reason about: no message can exceed the
limit whatever the backlog, and a part that fails costs only its own rows.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from watcher.notifier import (
    DIGEST_ENTRY_MAX,
    TELEGRAM_MAX_CHARS,
    format_digest,
    format_digest_chunks,
)


def _row(index: int, *, title: str | None = None, score: int = 55,
         company: str | None = None, url: str | None = None,
         location: str | None = "Berlin", country: str | None = "Germany"):
    """A posting row. `sqlite3.Row` is a mapping to the formatter, so a dict does."""
    return {
        "id": f"posting-{index}",
        "score": score,
        "title": title if title is not None else f"Data Scientist {index}",
        "company": company if company is not None else f"Company {index} GmbH",
        "location": location,
        "country": country,
        "url": url if url is not None else f"https://example.com/jobs/{index}",
    }


def _rows(count: int):
    return [_row(i) for i in range(1, count + 1)]


# --------------------------------------------------------------------------
# packing
# --------------------------------------------------------------------------

def test_a_small_digest_stays_one_message() -> None:
    """The common case must not grow a "part 1/1" header nobody needs."""
    chunks = format_digest_chunks(_rows(5))
    assert len(chunks) == 1
    text, batch = chunks[0]
    assert len(batch) == 5
    assert "part" not in text
    assert "5 posting(s) worth a look" in text


def test_an_empty_band_produces_no_message() -> None:
    assert format_digest_chunks([]) == []
    assert format_digest("") == "" or format_digest([]) == ""


@pytest.mark.parametrize("count", [30, 94, 250])
def test_no_part_can_exceed_the_telegram_limit(count: int) -> None:
    """94 is the backlog that was stuck; the others bracket it."""
    chunks = format_digest_chunks(_rows(count))
    assert chunks, "a non-empty band must produce at least one message"
    for text, _ in chunks:
        assert len(text) <= TELEGRAM_MAX_CHARS, (
            f"{len(text)} chars in one of {len(chunks)} part(s) — "
            "Telegram rejects this outright")


def test_the_stuck_backlog_now_splits_instead_of_failing() -> None:
    """21812 characters over 94 postings was a BadRequest every night."""
    chunks = format_digest_chunks(_rows(94))
    assert len(chunks) > 1
    assert all("part" in text for text, _ in chunks)


def test_every_posting_appears_exactly_once_across_the_parts() -> None:
    """Splitting must not drop or duplicate a posting — both are silent."""
    rows = _rows(94)
    chunks = format_digest_chunks(rows)
    covered = [row["id"] for _, batch in chunks for row in batch]
    assert covered == [row["id"] for row in rows]
    assert len(set(covered)) == len(rows)


def test_the_header_counts_the_whole_band_not_the_part() -> None:
    """"12 postings, part 1/3" is the honest reading; "5 postings" is not."""
    chunks = format_digest_chunks(_rows(94))
    assert all("94 posting(s) worth a look" in text for text, _ in chunks)


# --------------------------------------------------------------------------
# numbering — the reply contract
# --------------------------------------------------------------------------

def test_numbering_restarts_in_every_part() -> None:
    """`build 3` resolves against the message it replies to.

    `store.posting_for_message` looks the reply up against the postings recorded
    for that one message id, so continuous numbering across parts would send
    "build 17" hunting for a seventeenth entry in a message that has six.
    """
    chunks = format_digest_chunks(_rows(94))
    assert len(chunks) > 1
    for text, batch in chunks:
        for position in range(1, len(batch) + 1):
            assert f"\n{position}. <b>" in f"\n{text}", (
                f"entry {position} missing from a part of {len(batch)}")
        # And the part must not number past its own length.
        assert f"\n{len(batch) + 1}. <b>" not in f"\n{text}"


def test_a_split_digest_explains_its_own_numbering() -> None:
    """Without this the user reads part 2 and replies "build 1" to part 1."""
    chunks = format_digest_chunks(_rows(94))
    assert all("count from 1 in each part" in text for text, _ in chunks)


def test_a_single_part_digest_keeps_the_plain_footer() -> None:
    text, _ = format_digest_chunks(_rows(4))[0]
    assert "count from 1 in each part" not in text
    assert "build 2" in text


# --------------------------------------------------------------------------
# pathological single entries
# --------------------------------------------------------------------------

def test_one_oversized_posting_is_truncated_rather_than_sent_whole() -> None:
    """A tracking URL or a 5000-character title cannot be split across parts.

    If a single entry were allowed through at full width it would blow the limit
    on its own and reinstate exactly the failure this fixes.
    """
    monster = _row(1, title="Senior " * 900 + "Engineer",
                   url="https://example.com/" + "q" * 3000)
    chunks = format_digest_chunks([monster])
    assert len(chunks) == 1
    text, batch = chunks[0]
    assert len(batch) == 1
    assert len(text) <= TELEGRAM_MAX_CHARS
    assert "…" in text


def test_an_oversized_entry_keeps_its_html_balanced() -> None:
    """Truncation cuts the tail; the one tag pair closes on the first line."""
    monster = _row(1, title="x" * 6000)
    text, _ = format_digest_chunks([monster])[0]
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<i>") == text.count("</i>")


def test_an_oversized_entry_does_not_starve_the_ones_after_it() -> None:
    rows = [_row(1, title="x" * 6000)] + _rows(3)
    chunks = format_digest_chunks(rows)
    covered = [row["id"] for _, batch in chunks for row in batch]
    assert len(covered) == 4
    assert all(len(text) <= TELEGRAM_MAX_CHARS for text, _ in chunks)


def test_entry_budget_leaves_room_for_the_header_and_footer() -> None:
    assert DIGEST_ENTRY_MAX < TELEGRAM_MAX_CHARS


# --------------------------------------------------------------------------
# format_digest stays the first message
# --------------------------------------------------------------------------

def test_format_digest_returns_the_first_part() -> None:
    rows = _rows(94)
    assert format_digest(rows) == format_digest_chunks(rows)[0][0]


# --------------------------------------------------------------------------
# send_digest — recording per part
# --------------------------------------------------------------------------

class _Store:
    """Stands in for `watcher.store`: records what the notifier would persist."""

    def __init__(self, rows):
        self._rows = rows
        self.recorded: list[tuple[str, int, str]] = []

    @contextlib.contextmanager
    def connect(self, path=None):
        yield None

    def unnotified_in_band(self, conn, low, high=None):
        return self._rows

    def record_notification(self, conn, posting_id, chat_id, message_id, kind):
        self.recorded.append((posting_id, message_id, kind))


class _Notifier:
    """A Notifier without the Telegram bot or the env it demands at init."""

    def __init__(self, config, fail_on=()):
        self.config = config
        self.chat_id = "chat"
        self.sent: list[str] = []
        self.topics: list[str | None] = []
        self._fail_on = set(fail_on)

    async def send(self, text: str, reply_to=None, topic=None) -> int:
        number = len(self.sent) + 1
        self.sent.append(text)
        self.topics.append(topic)
        if number in self._fail_on:
            raise RuntimeError(f"telegram rejected part {number}")
        return 1000 + number


class _Config:
    digest_threshold = 40
    notify_threshold = 70


def _run_digest(monkeypatch, rows, fail_on=()):
    from watcher import notifier as module

    store = _Store(rows)
    monkeypatch.setattr(module, "store", store)
    instance = _Notifier(_Config(), fail_on=fail_on)
    sent = asyncio.run(module.Notifier.send_digest(instance))
    return sent, store, instance


def test_each_part_records_against_its_own_message_id(monkeypatch) -> None:
    """A reply to part 2 must resolve against part 2's postings, not part 1's."""
    rows = _rows(94)
    sent, store, instance = _run_digest(monkeypatch, rows)

    assert sent == len(rows)
    assert len(instance.sent) > 1
    by_message: dict[int, int] = {}
    for _, message_id, kind in store.recorded:
        assert kind == "digest"
        by_message[message_id] = by_message.get(message_id, 0) + 1
    assert len(by_message) == len(instance.sent), (
        "every part needs its own message id")


def test_a_failed_part_leaves_its_postings_in_the_band(monkeypatch) -> None:
    """The whole point: a rejected part costs only its own rows.

    They stay unrecorded, so they are still in the band for the next digest —
    not marked sent, and not lost.
    """
    rows = _rows(94)
    sent, store, instance = _run_digest(monkeypatch, rows, fail_on=(1,))

    assert len(instance.sent) > 1, "the later parts must still be attempted"
    recorded = {posting_id for posting_id, _, _ in store.recorded}
    first_part = {row["id"] for row in format_digest_chunks(rows)[0][1]}
    assert not (recorded & first_part), "a failed part must record nothing"
    assert sent == len(rows) - len(first_part)
    assert sent > 0, "the parts that succeeded still count"


def test_an_empty_band_sends_nothing(monkeypatch) -> None:
    sent, store, instance = _run_digest(monkeypatch, [])
    assert sent == 0
    assert instance.sent == []
    assert store.recorded == []


def test_every_part_goes_to_the_postings_topic(monkeypatch) -> None:
    """A digest line is a posting the user has not seen, only a quieter one.

    Splitting one digest across parts must not scatter them across topics — and
    with no topics configured the kind resolves to None, so this costs the
    chat-id-only setup nothing.
    """
    sent, store, instance = _run_digest(monkeypatch, _rows(94))
    assert len(instance.sent) > 1
    assert set(instance.topics) == {"new_posting"}
