#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os

from telethon import TelegramClient


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward messages from a Telegram source chat into the ScamBaiter control bot."
    )
    parser.add_argument("--source", required=True, help="Source chat username or ID to mirror.")
    parser.add_argument(
        "--target",
        type=int,
        help="Control chat ID that receives the forwards (defaults to SCAMBAITER_CONTROL_CHAT_ID).",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max number of messages to forward (0 = all).")
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds between forwards to avoid flood limits.")
    parser.add_argument("--session", default="scambaiter.session", help="Telethon session filename.")
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    api_id = os.getenv("TELETHON_API_ID")
    api_hash = os.getenv("TELETHON_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELETHON_API_ID and TELETHON_API_HASH must be set")
    target_chat = args.target or int(os.getenv("SCAMBAITER_CONTROL_CHAT_ID") or "0")
    if target_chat == 0:
        raise RuntimeError("Target chat ID is required (CLI flag or SCAMBAITER_CONTROL_CHAT_ID)")

    session_file = args.session
    client = TelegramClient(session_file, int(api_id), api_hash)
    await client.start()
    try:
        entity = await client.get_entity(args.source)
        count = 0
        async for message in client.iter_messages(entity, reverse=True, limit=args.limit or None):
            await client.forward_messages(target_chat, message)
            count += 1
            if args.delay > 0:
                await asyncio.sleep(args.delay)
        print(f"forwarded {count} messages from {args.source} to {target_chat}")
    finally:
        await client.disconnect()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
