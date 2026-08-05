"""Find the chat id — and the topic thread ids — by watching for real messages.

    python -m watcher.whoami

Then send any message to the bot: in a private chat, or in a group the bot has
been added to. The id is printed as soon as it arrives.

If the group is a forum, keep going and post once in each topic you want the
watcher to use. Every topic prints its `[notify.topics]` thread id the first
time it hears from it, so one pass collects the whole section.

Reading these off a Telegram Web URL is where this usually goes wrong: web shows
an internal id, and whether it needs a `-100` prefix depends on the chat type. A
message the bot actually received is unambiguous.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from telegram import Bot

from .config import ENV_PATH, load_env
from .logsetup import force_utf8


def thread_of(source) -> int | None:
    """The forum thread a message arrived in, or None for General.

    Telegram sets `message_thread_id` on a reply inside a plain group too, so
    `is_topic_message` is the field that actually distinguishes a topic. It is
    unset in General and in a non-forum chat alike — which is the same pair
    `run_watcher` answers /status and /restart from, and for the same reason.
    """
    if not getattr(source, "is_topic_message", False):
        return None
    return getattr(source, "message_thread_id", None)


def topic_name(source) -> str:
    """The topic's title, when Telegram bothered to include it.

    It rides along on the topic-creation service message a topic's own messages
    reply to, which is present often but not always. Worth printing when it is
    there and not worth chasing when it is not — the id is the load-bearing half.
    """
    replied = getattr(source, "reply_to_message", None)
    created = getattr(replied, "forum_topic_created", None)
    return getattr(created, "name", "") or ""


async def watch(timeout: int) -> int:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(f"TELEGRAM_BOT_TOKEN is not set in {ENV_PATH}.")
        return 2

    bot = Bot(token)
    me = await bot.get_me()
    print(f"Listening as @{me.username}. Send it a message now "
          f"(waiting up to {timeout}s)…\n")

    offset = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: set[tuple[int, int | None]] = set()
    chats: set[int] = set()
    forum = False

    while loop.time() < deadline:
        updates = await bot.get_updates(offset=offset, timeout=20)
        for update in updates:
            offset = update.update_id + 1
            source = (update.message or update.channel_post
                      or update.my_chat_member or update.edited_message)
            chat = getattr(source, "chat", None)
            if chat is None:
                continue
            thread = thread_of(source)
            # Keyed on the pair, so a forum keeps reporting as each new topic
            # is heard from instead of stopping at the chat it belongs to.
            if (chat.id, thread) in seen:
                continue
            seen.add((chat.id, thread))

            if chat.id not in chats:
                chats.add(chat.id)
                who = chat.title or chat.username or chat.full_name or "?"
                print(f"  TELEGRAM_CHAT_ID={chat.id}    ({chat.type} — {who})")
            first_forum = getattr(chat, "is_forum", False) and not forum
            forum = forum or getattr(chat, "is_forum", False)

            if thread is not None:
                name = topic_name(source)
                print(f"      thread id {thread}"
                      f"{f'    ({name})' if name else ''}")
            elif forum:
                print("      General — no thread id; /status and /restart are "
                      "answered here")
            if first_forum:
                print("      (a forum — post once in each topic you want to "
                      "use; still listening)")

        if seen and not forum:
            print(f"\nPut that line in {ENV_PATH}.")
            return 0

    if seen:
        # A forum runs to the deadline on purpose: there is no way to know how
        # many topics are still coming, and stopping at the first would send
        # the user round again for every one of the five.
        print(f"\nPut the chat id in {ENV_PATH}, and the thread ids in\n"
              "[notify.topics] in config.toml — see automation/README.md.")
        return 0

    print("Nothing arrived.\n"
          "  · private chat: open @%s and press Start\n"
          "  · group: add the bot as a member, then post a message\n"
          "  · channel: add the bot as an admin, then post" % me.username)
    return 1


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=int, default=180,
                        help="how long to wait for a message, in seconds")
    args = parser.parse_args(argv)
    return asyncio.run(watch(args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
