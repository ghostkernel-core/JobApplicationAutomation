"""Messages land in the forum topic their kind belongs to — or in General.

The chat is now optionally a forum: new postings in one topic, approvals filed
in a second, build progress in a third, failures and completions in their own.
The whole feature is additive, and the property that makes it safe is the one
worth pinning hardest: with no `[notify.topics]` configured, every send has to
be byte-for-byte the send it was before — same chat, no thread, and crucially
the same `reply_to_message_id`.

That last field is where the two behaviours actually collide. Telegram rejects
a `sendMessage` outright when `reply_to_message_id` names a message in a
different topic — not by dropping the threading, but by failing the whole send.
So the reply has to be dropped exactly when a thread is chosen, and never
otherwise. Get that backwards in either direction and one of the two setups
loses its messages entirely.
"""

from __future__ import annotations

import asyncio

import pytest

from watcher import notifier as notifier_module
from watcher.config import TOPIC_KINDS, Config


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _config(**topics) -> Config:
    """A Config whose only interesting content is its `[notify.topics]` table."""
    return Config(notify={"topics": dict(topics)})


def test_no_topics_configured_means_no_routing(monkeypatch) -> None:
    """The default, and the shape of every existing install."""
    monkeypatch.delenv("WATCHER_NOTIFY_TOPICS_NEW_POSTING", raising=False)
    config = Config()
    assert config.topics == {}
    assert config.topics_enabled is False
    for kind in TOPIC_KINDS:
        assert config.topic_for(kind) is None


def test_a_configured_kind_resolves_to_its_thread() -> None:
    config = _config(new_posting=12, completed_build=44)
    assert config.topic_for("new_posting") == 12
    assert config.topic_for("completed_build") == 44
    assert config.topics_enabled is True


def test_an_unconfigured_kind_falls_back_to_general() -> None:
    """Partial configuration is legitimate — one topic is a valid setup."""
    config = _config(new_posting=12)
    assert config.topic_for("failed_build") is None
    assert config.topic_for(None) is None


def test_zero_means_unset_rather_than_thread_zero() -> None:
    """config.toml ships every key at 0, so 0 has to read as "not configured"."""
    config = _config(new_posting=0, targeted_build=0)
    assert config.topics == {}
    assert config.topics_enabled is False


def test_an_unusable_thread_id_is_ignored_not_raised() -> None:
    """A typo in config.toml must not take the watcher down.

    Losing the routing for one kind costs a message its topic; raising here
    costs every notification the watcher would have sent.
    """
    config = _config(new_posting="not a number", failed_build=9)
    assert config.topics == {"failed_build": 9}


def test_an_unknown_key_is_not_a_topic() -> None:
    """Only the five kinds route. A stray key is config noise, not a thread."""
    config = _config(new_posting=12, made_up_kind=99)
    assert config.topics == {"new_posting": 12}
    assert config.topic_for("made_up_kind") is None


def test_an_env_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("WATCHER_NOTIFY_TOPICS_NEW_POSTING", "77")
    assert _config(new_posting=12).topic_for("new_posting") == 77


# --------------------------------------------------------------------------
# Notifier.send
# --------------------------------------------------------------------------

class _Message:
    message_id = 5150


class _Bot:
    """Records what would have gone to Telegram."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return _Message()


def _notifier(config: Config) -> tuple[notifier_module.Notifier, _Bot]:
    notifier = notifier_module.Notifier.__new__(notifier_module.Notifier)
    notifier._config_override = config
    notifier.chat_id = "-1001234567890"
    notifier._bot = bot = _Bot()
    return notifier, bot


def test_without_topics_a_send_is_exactly_what_it_always_was() -> None:
    """The compatibility guarantee, asserted field by field."""
    notifier, bot = _notifier(Config())
    asyncio.run(notifier.send("hello", reply_to=42, topic="new_posting"))

    call = bot.calls[0]
    assert call["chat_id"] == "-1001234567890"
    assert call["message_thread_id"] is None
    assert call["reply_to_message_id"] == 42


def test_a_configured_topic_threads_the_message() -> None:
    notifier, bot = _notifier(_config(new_posting=12))
    asyncio.run(notifier.send("hello", topic="new_posting"))
    assert bot.calls[0]["message_thread_id"] == 12


def test_threading_drops_the_reply() -> None:
    """The collision. Telegram fails the whole send if both are set across topics.

    Every message that carries a reply also names its posting in its own text,
    so the thread is the better half of the pair to keep.
    """
    notifier, bot = _notifier(_config(processing_build=13))
    asyncio.run(notifier.send("building", reply_to=42, topic="processing_build"))

    call = bot.calls[0]
    assert call["message_thread_id"] == 13
    assert call["reply_to_message_id"] is None


def test_an_unrouted_kind_keeps_its_reply_even_when_topics_exist() -> None:
    """Half-configured forums must not lose replies on the unrouted kinds."""
    notifier, bot = _notifier(_config(new_posting=12))
    asyncio.run(notifier.send("done", reply_to=42, topic="completed_build"))

    call = bot.calls[0]
    assert call["message_thread_id"] is None
    assert call["reply_to_message_id"] == 42


def test_an_operational_notice_stays_in_general() -> None:
    """Heartbeats, source alerts and the kb proposal have no topic on purpose.

    General is where a reply reaches the watcher, and where `/status` and
    `/restart` are answered — so that is where the watcher talks about itself.
    """
    notifier, bot = _notifier(_config(**{kind: 10 + i
                                         for i, kind in enumerate(TOPIC_KINDS)}))
    asyncio.run(notifier.send_notice("💚 alive"))
    assert bot.calls[0]["message_thread_id"] is None


# --------------------------------------------------------------------------
# the approval record
# --------------------------------------------------------------------------

def test_the_targeted_record_is_silent_without_its_topic() -> None:
    """This message has no chat-id-only equivalent, so it must not invent one.

    Filing approvals is only meaningful as a topic. Sending it into a plain
    chat would add traffic to a setup this feature is supposed to leave alone.
    """
    notifier, bot = _notifier(_config(new_posting=12))
    asyncio.run(notifier.send_targeted("Acme", "Data Scientist",
                                       "https://example.invalid/1", 82, ""))
    assert bot.calls == []


def test_the_targeted_record_files_the_decision() -> None:
    notifier, bot = _notifier(_config(targeted_build=14))
    asyncio.run(notifier.send_targeted("Acme", "Data Scientist",
                                       "https://example.invalid/1", 82,
                                       "add German"))

    call = bot.calls[0]
    assert call["message_thread_id"] == 14
    # The decision is the point of the record: what was approved, with what
    # instruction, on what link.
    assert "Acme" in call["text"]
    assert "Data Scientist" in call["text"]
    assert "add German" in call["text"]
    assert "https://example.invalid/1" in call["text"]
    assert "82" in call["text"]


def test_a_failed_record_does_not_take_the_build_with_it(caplog) -> None:
    """Losing the record is a nuisance; losing the build it recorded is not."""
    notifier, bot = _notifier(_config(targeted_build=14))

    async def broken(**kwargs):
        raise RuntimeError("telegram fell over")

    bot.send_message = broken
    asyncio.run(notifier.send_targeted("Acme", "Data Scientist", "", None, ""))


# --------------------------------------------------------------------------
# the build's own reports
# --------------------------------------------------------------------------

class _Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int | None, str | None]] = []

    async def send(self, text, reply_to=None, topic=None):
        self.sent.append((text, reply_to, topic))
        return 1


def _reply(text: str, **kwargs) -> tuple[str, int | None, str | None]:
    from watcher.builder import Builder, Job

    builder = Builder.__new__(Builder)
    builder.notifier = notifier = _Notifier()
    job = Job(posting_id="p1", note="", reply_message_id=42, build_id=1)
    asyncio.run(Builder._reply(builder, job, text, **kwargs))
    return notifier.sent[0]


def test_build_progress_defaults_to_the_processing_topic() -> None:
    """Building, retrying, queued, duplicate — most of what a build says."""
    assert _reply("🛠 Building…")[2] == "processing_build"


@pytest.mark.parametrize("topic", ["completed_build", "failed_build"])
def test_the_two_ends_of_a_run_route_to_their_own_topics(topic: str) -> None:
    assert _reply("…", topic=topic)[2] == topic


def test_a_build_report_still_carries_its_reply() -> None:
    """Dropping it is `send`'s job, and only when a thread is actually chosen."""
    assert _reply("🛠 Building…")[1] == 42


def test_a_duplicate_decline_is_filed_where_the_approval_was(
        monkeypatch, tmp_path) -> None:
    """The one build message that is not progress.

    A duplicate ends the run by asking the user whether to build it anyway, so
    it belongs in Targeted — the topic their approval was in and the one they
    read for decisions. It went to Processing, which is a feed of things
    happening; a build that never started produced no other line there, and the
    whole event read as the approval having been dropped on the floor.
    """
    import datetime as dt

    from watcher import builder as builder_mod
    from watcher import dedupe, store

    path = tmp_path / "watch.db"
    store.init_db(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ensure_dirs", lambda: None)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO postings (id, loose_key, source, provider, url,
                                     canonical_url, company, title,
                                     first_seen_at, description)
               VALUES ('p1', 'Roche|ML', 'ats:roche', 'greenhouse',
                       'https://roche.test/1', 'https://roche.test/1',
                       'Roche', 'Machine Learning Engineer', ?, '')""",
            (now,))
        build = store.queue_build(conn, "p1")

    monkeypatch.setattr(builder_mod, "check_duplicate", lambda c, t, cfg: (
        dedupe.ExistingApplication(
            company="Roche", title="Data Scientist Machine Learning Engineer",
            applied_on=dt.date(2026, 6, 24),
            folder="/w/2026/Roche/2026-06-24 - Data Scientist ML Engineer",
            similarity=1.0, origin="folder"), ""))

    class _Chatty(_Notifier):
        chat_id = "-100123"
        config = Config(notify={"topics": {"targeted_build": 162}})

    worker = builder_mod.Builder(_Chatty())
    asyncio.run(worker._handle(
        builder_mod.Job("p1", "", 42, build)))

    text, _, topic = worker.notifier.sent[0]
    assert topic == "targeted_build"
    assert "Duplicate" in text
    assert "anyway" in text, "a decline the user cannot overrule is final"

    # And it is recorded as a question, in the thread it was sent to, so the
    # override can be a plain reply rather than another command.
    with store.connect() as conn:
        question = store.question_for_posting(conn, "p1")
    assert question is not None
    assert question["kind"] == store.QUESTION_DUPLICATE
    assert question["thread_id"] == 162
    assert not question["folder"], (
        "that folder holds a finished application — recording it here would "
        "point `no` and `/cancel` at it")
    assert "2026-06-24" in question["question"], (
        "which is why the match has to be named in the text instead")
