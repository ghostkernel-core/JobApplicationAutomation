"""Find the chat id to put in .env, by watching for a real message.

    python -m watcher.whoami

Then send any message to the bot — in a private chat, or in a group the bot has
been added to. The id is printed as soon as it arrives.

Reading the id off a Telegram Web URL is where this usually goes wrong: web
shows an internal id, and whether it needs a `-100` prefix depends on the chat
type. A message the bot actually received is unambiguous.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from telegram import Bot

from .config import ENV_PATH, load_env
from .logsetup import force_utf8


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
    seen: set[int] = set()

    while loop.time() < deadline:
        updates = await bot.get_updates(offset=offset, timeout=20)
        for update in updates:
            offset = update.update_id + 1
            source = (update.message or update.channel_post
                      or update.my_chat_member or update.edited_message)
            chat = getattr(source, "chat", None)
            if chat is None or chat.id in seen:
                continue
            seen.add(chat.id)
            who = chat.title or chat.username or chat.full_name or "?"
            print(f"  TELEGRAM_CHAT_ID={chat.id}    ({chat.type} — {who})")
        if seen:
            print(f"\nPut that line in {ENV_PATH}.")
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
