"""`whoami` reports the thread ids too, so a forum can be configured in one pass.

`[notify.topics]` wants five thread ids, and the two ways to get them without
this were reading them out of a Telegram Web URL or forwarding messages to a
third-party bot. The module's own docstring already argues against the first one
for chat ids, and the second means handing your messages to someone else's bot to
configure your own.

Two things had to change for it to answer the whole question. It deduped on the
chat id, so a forum stopped reporting after its first topic — the key is now the
(chat, thread) pair. And it returned as soon as it had anything, which for a
forum means four more runs; it now waits out the timeout, because there is no way
to know how many topics are still coming.

The distinction it has to get right is General versus a topic. Telegram sets
`message_thread_id` on an ordinary reply in a plain group as well, so the field
that actually means "this is a topic" is `is_topic_message` — the same field
`run_watcher` uses to decide where /status and /restart are answered.
"""

from __future__ import annotations

import asyncio

import pytest

from watcher import whoami
from watcher.whoami import thread_of, topic_name


class _Message:
    """Enough of `telegram.Message` for the two helpers under test."""

    def __init__(self, *, thread=None, is_topic=False, reply_to=None,
                 chat=None) -> None:
        self.message_thread_id = thread
        self.is_topic_message = is_topic
        self.reply_to_message = reply_to
        self.chat = chat


class _Created:
    def __init__(self, name: str) -> None:
        self.name = name


class _TopicStart:
    def __init__(self, name: str) -> None:
        self.forum_topic_created = _Created(name)


# --------------------------------------------------------------------------
# General vs a topic
# --------------------------------------------------------------------------

def test_a_topic_message_reports_its_thread() -> None:
    assert thread_of(_Message(thread=25, is_topic=True)) == 25


def test_general_has_no_thread() -> None:
    """The General topic of a forum. This is where the commands are answered."""
    assert thread_of(_Message(is_topic=False)) is None


def test_a_plain_reply_is_not_a_topic() -> None:
    """The trap. A reply in a non-forum group carries a thread id anyway.

    Reporting that as a topic id would hand the user a number that routes
    nothing, in a chat that has no topics at all.
    """
    assert thread_of(_Message(thread=41, is_topic=False)) is None


def test_a_missing_thread_id_is_not_invented() -> None:
    assert thread_of(_Message(thread=None, is_topic=True)) is None


def test_an_update_without_the_fields_at_all_is_survivable() -> None:
    """`my_chat_member` and `channel_post` have neither field."""
    assert thread_of(object()) is None


# --------------------------------------------------------------------------
# the topic's name, when Telegram sends it
# --------------------------------------------------------------------------

def test_the_topic_name_is_used_when_it_is_there() -> None:
    message = _Message(thread=25, is_topic=True,
                       reply_to=_TopicStart("New Posting"))
    assert topic_name(message) == "New Posting"


def test_a_missing_name_is_empty_rather_than_an_error() -> None:
    """It rides on a service message that is not always included.

    The id is the half that matters; the name is a convenience, and a missing
    one must not cost the line it was going to decorate.
    """
    assert topic_name(_Message(thread=25, is_topic=True)) == ""
    assert topic_name(_Message(thread=25, is_topic=True,
                               reply_to=_Message())) == ""
    assert topic_name(object()) == ""


# --------------------------------------------------------------------------
# the listening pass itself
# --------------------------------------------------------------------------

class _Chat:
    def __init__(self, chat_id, *, type="supergroup", title=None,
                 username=None, full_name=None, is_forum=False) -> None:
        self.id, self.type = chat_id, type
        self.title, self.username, self.full_name = title, username, full_name
        self.is_forum = is_forum


class _Update:
    def __init__(self, update_id, message=None, **kinds) -> None:
        self.update_id = update_id
        self.message = message
        self.channel_post = kinds.get("channel_post")
        self.my_chat_member = kinds.get("my_chat_member")
        self.edited_message = kinds.get("edited_message")


class _Clock:
    """A loop-like object whose clock only moves when the test says so.

    Real time would mean a test that either waits out a timeout or races it.
    `watch` reads the clock once for the deadline and once per iteration, so a
    scripted list of readings decides exactly how many polls happen.
    """

    def __init__(self, *readings) -> None:
        self._readings = list(readings)

    def time(self) -> float:
        return self._readings.pop(0) if len(self._readings) > 1 \
            else self._readings[0]


@pytest.fixture()
def bot(monkeypatch):
    """A bot whose `get_updates` hands back scripted batches."""
    state = type("S", (), {"batches": [], "polls": 0})()

    class _Bot:
        def __init__(self, token) -> None:
            state.token = token

        async def get_me(self):
            return type("Me", (), {"username": "watcher_bot"})()

        async def get_updates(self, offset=None, timeout=20):
            state.polls += 1
            state.offset = offset
            return state.batches.pop(0) if state.batches else []

    monkeypatch.setattr(whoami, "Bot", _Bot)
    monkeypatch.setattr(whoami, "load_env", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t:oken")
    return state


def run(monkeypatch, clock, timeout=60) -> int:
    """Run one `watch` pass against a scripted clock.

    Patching the name in `asyncio`'s own namespace is safe here: the runner
    machinery reaches for `asyncio.events.get_running_loop`, so only `watch`
    itself sees the stub.
    """
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: clock)
    return asyncio.run(whoami.watch(timeout))


def test_a_plain_group_stops_as_soon_as_it_has_the_chat_id(
        bot, monkeypatch, capsys) -> None:
    """One message answers the whole question, so waiting out the timeout
    would only leave the user staring at a prompt for three minutes."""
    bot.batches.append([_Update(7, _Message(chat=_Chat(-100123, title="Jobs")))])

    assert run(monkeypatch, _Clock(0, 0, 0, 999)) == 0
    out = capsys.readouterr().out
    assert "TELEGRAM_CHAT_ID=-100123" in out
    assert "(supergroup — Jobs)" in out
    assert bot.polls == 1


def test_a_forum_keeps_listening_and_reports_every_topic(
        bot, monkeypatch, capsys) -> None:
    """The whole point of the pass: five thread ids in one sitting."""
    forum = _Chat(-100123, title="Jobs", is_forum=True)
    bot.batches += [
        [_Update(1, _Message(chat=forum))],
        [_Update(2, _Message(chat=forum, thread=25, is_topic=True,
                             reply_to=_TopicStart("New Posting"))),
         _Update(3, _Message(chat=forum, thread=31, is_topic=True))],
    ]

    assert run(monkeypatch, _Clock(0, 0, 0, 999)) == 0
    out = capsys.readouterr().out
    assert "thread id 25    (New Posting)" in out
    assert "thread id 31" in out
    assert "General — no thread id" in out
    assert "still listening" in out
    assert out.count("still listening") == 1, "said once, not per topic"
    assert "[notify.topics] in config.toml" in out


def test_the_same_topic_twice_is_reported_once(bot, monkeypatch, capsys) -> None:
    forum = _Chat(-100123, is_forum=True, title="Jobs")
    bot.batches += [
        [_Update(1, _Message(chat=forum, thread=25, is_topic=True)),
         _Update(2, _Message(chat=forum, thread=25, is_topic=True))],
    ]
    assert run(monkeypatch, _Clock(0, 0, 999)) == 0
    assert capsys.readouterr().out.count("thread id 25") == 1


def test_each_batch_is_acknowledged_so_it_is_not_re_delivered(
        bot, monkeypatch) -> None:
    chat = _Chat(-100123, is_forum=True, title="Jobs")
    bot.batches += [[_Update(41, _Message(chat=chat))], []]
    run(monkeypatch, _Clock(0, 0, 0, 999))
    assert bot.offset == 42


def test_an_update_with_no_chat_is_skipped(bot, monkeypatch, capsys) -> None:
    bot.batches.append([_Update(1), _Update(2, _Message(chat=None))])
    assert run(monkeypatch, _Clock(0, 0, 999)) == 1
    assert "Nothing arrived" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["channel_post", "my_chat_member",
                                  "edited_message"])
def test_the_other_update_kinds_answer_too(bot, monkeypatch, capsys,
                                           kind) -> None:
    """A channel never sends `message`, so keying on that alone would leave a
    channel setup with nothing to report."""
    bot.batches.append([_Update(1, **{kind: _Message(
        chat=_Chat(-100999, type="channel", title="Feed"))})])
    assert run(monkeypatch, _Clock(0, 0, 0, 999)) == 0
    assert "TELEGRAM_CHAT_ID=-100999" in capsys.readouterr().out


def test_a_chat_with_no_title_falls_back_to_a_name(bot, monkeypatch,
                                                   capsys) -> None:
    bot.batches.append([_Update(1, _Message(
        chat=_Chat(4242, type="private", full_name="Jane Doe")))])
    run(monkeypatch, _Clock(0, 0, 0, 999))
    assert "(private — Jane Doe)" in capsys.readouterr().out


def test_silence_says_what_to_try_rather_than_just_failing(
        bot, monkeypatch, capsys) -> None:
    assert run(monkeypatch, _Clock(0, 0, 999)) == 1
    out = capsys.readouterr().out
    assert "press Start" in out and "@watcher_bot" in out


def test_no_token_is_a_distinct_exit_code(bot, monkeypatch, capsys) -> None:
    """Exit 2 rather than 1: nothing was listened for, so "nothing arrived"
    would send the user off checking the wrong thing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert asyncio.run(whoami.watch(60)) == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().out


def test_the_cli_passes_the_timeout_through(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(whoami, "watch", lambda t: seen.append(t) or _done(0))
    assert whoami.main(["--timeout", "5"]) == 0
    assert seen == [5]

    assert whoami.main([]) == 0
    assert seen[-1] == 180, "the default is long enough to walk to a phone"


async def _done(value):
    return value
