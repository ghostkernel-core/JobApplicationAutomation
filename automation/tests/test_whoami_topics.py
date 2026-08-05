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

from watcher.whoami import thread_of, topic_name


class _Message:
    """Enough of `telegram.Message` for the two helpers under test."""

    def __init__(self, *, thread=None, is_topic=False, reply_to=None) -> None:
        self.message_thread_id = thread
        self.is_topic_message = is_topic
        self.reply_to_message = reply_to


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
