"""
setup_auth.py

Standalone CLI helper to perform the *interactive* first-time login for
Telegram, so that server.py can later run non-interactively under
OpenCode's stdio transport.

Usage:
    python setup_auth.py --telegram
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import load_config


async def setup_telegram() -> None:
    cfg = load_config().telegram
    client = TelegramClient(cfg.session_path, cfg.api_id, cfg.api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print("[telegram] Session already authorized. Nothing to do.")
        await client.disconnect()
        return

    phone = cfg.phone or input("Enter your Telegram phone number (with country code): ").strip()
    await client.send_code_request(phone)
    code = input("Enter the code you received: ").strip()

    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = input("Two-factor password required, enter it: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"[telegram] Logged in as: {me.first_name} (@{me.username}). Session saved to {cfg.session_path}")
    await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time interactive auth setup for social-mcp.")
    parser.add_argument("--telegram", action="store_true", help="Set up Telegram session.")
    args = parser.parse_args()

    if not args.telegram:
        parser.print_help()
        sys.exit(1)

    asyncio.run(setup_telegram())


if __name__ == "__main__":
    main()
