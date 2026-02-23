#!/usr/bin/env python3
"""Fetch message history and profiles from Telegram into the local store.

Usage:
  # Single chat:
  PYTHONPATH=. python scripts/sync_history_cli.py --chat-id 7608193197

  # All chats in the Scammers folder:
  PYTHONPATH=. python scripts/sync_history_cli.py --folder Scammers

  # Unlimited history depth, also repair timestamps:
  PYTHONPATH=. python scripts/sync_history_cli.py --chat-id 7608193197 --limit 0

  # Custom history depth, skip profile fetch:
  PYTHONPATH=. python scripts/sync_history_cli.py --chat-id 7608193197 --limit 500 --no-profile
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scambaiter.config import load_config
from scambaiter.storage import AnalysisStore
from scambaiter.telethon_executor import TelethonExecutor


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Telegram message history into the scambaiter store."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chat-id", type=int, metavar="CHAT_ID", help="Fetch history for a single chat.")
    group.add_argument("--folder", metavar="NAME", help="Fetch history for all chats in this Telegram folder.")
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        metavar="N",
        help="Max messages to fetch per chat (default: 200). Use 0 for unlimited.",
    )
    parser.add_argument("--no-profile", action="store_true", help="Skip profile fetch.")
    return parser.parse_args()


async def _sync_one(
    executor: TelethonExecutor,
    store: AnalysisStore,
    chat_id: int,
    limit: int | None,
    skip_profile: bool,
) -> None:
    limit_label = "unlimited" if limit is None else str(limit)
    try:
        before = store.count_events(chat_id)
        count = await executor.fetch_history(chat_id, store, limit=limit)
        print(f"  {chat_id}: {count} new event(s), {before} already present")
    except Exception as exc:
        print(f"  {chat_id}: fetch_history failed — {exc}")
        return
    try:
        repaired = store.repair_timestamps_from_meta(chat_id)
        if repaired:
            print(f"  {chat_id}: {repaired} timestamp(s) repaired from forward metadata")
    except Exception as exc:
        print(f"  {chat_id}: repair_timestamps failed — {exc}")
    if not skip_profile:
        try:
            await executor.fetch_profile(chat_id, store)
            print(f"  {chat_id}: profile updated")
        except Exception as exc:
            print(f"  {chat_id}: fetch_profile failed — {exc}")


async def _run() -> None:
    args = _parse_args()
    config = load_config()

    if not config.telethon_api_id or not config.telethon_api_hash:
        print("Error: TELETHON_API_ID and TELETHON_API_HASH must be set.", file=sys.stderr)
        sys.exit(1)

    # --limit 0 means unlimited
    limit: int | None = None if args.limit == 0 else args.limit
    limit_label = "unlimited" if limit is None else str(limit)

    store = AnalysisStore(config.analysis_db_path)
    executor = TelethonExecutor(
        api_id=config.telethon_api_id,
        api_hash=config.telethon_api_hash,
        session=config.telethon_session,
    )
    await executor.start()
    try:
        if args.chat_id is not None:
            print(f"Syncing chat_id={args.chat_id} (limit={limit_label}) ...")
            await _sync_one(executor, store, args.chat_id, limit, args.no_profile)
        else:
            print(f"Resolving folder {args.folder!r} ...")
            chat_ids = await executor._resolve_folder_ids(args.folder)
            if not chat_ids:
                print(f"No chats found in folder {args.folder!r}.")
                return
            print(f"Found {len(chat_ids)} chat(s). Syncing (limit={limit_label} each) ...")
            for chat_id in sorted(chat_ids):
                await _sync_one(executor, store, chat_id, limit, args.no_profile)
        print("Done.")
    finally:
        await executor.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
