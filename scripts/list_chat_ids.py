#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from telethon import TelegramClient
from telethon import errors, utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import Channel, Chat, DialogFilter, User


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List chat IDs from a specific Telegram dialog folder (default: Scammers)."
    )
    parser.add_argument("--session", default="scambaiter.session", help="Telethon session filename.")
    parser.add_argument("--folder", default="Scammers", help="Exact Telegram folder name to scan.")
    parser.add_argument("--limit", type=int, default=200, help="Max number of dialogs to list (0 = no limit).")
    parser.add_argument("--filter", default="", help="Optional substring filter for title/username.")
    parser.add_argument("--find", default="", help="Alias for --filter, optimized for quick ID search.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser.parse_args()


def _folder_title(value: Any) -> str:
    title = getattr(value, "title", None)
    if isinstance(title, str):
        return title
    text = getattr(title, "text", None)
    if isinstance(text, str):
        return text
    entities = getattr(title, "entities", None)
    if isinstance(entities, list):
        return "".join(str(item) for item in entities)
    return ""


def _entity_kind(entity: Any) -> str:
    if isinstance(entity, User):
        return "bot" if bool(getattr(entity, "bot", False)) else "user"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if bool(getattr(entity, "megagroup", False)):
            return "group"
        return "channel"
    return "unknown"


async def _resolve_folder_id(client: TelegramClient, folder_name: str) -> int:
    filters_result = await client(GetDialogFiltersRequest())
    filters = getattr(filters_result, "filters", filters_result)
    for item in filters:
        if not isinstance(item, DialogFilter):
            continue
        if _folder_title(item) == folder_name:
            return int(item.id)
    raise RuntimeError(f"Telegram folder not found: {folder_name!r}")


def _extract_folder_peer_ids(folder_filter: DialogFilter) -> set[int]:
    peer_ids: set[int] = set()
    include_peers = getattr(folder_filter, "include_peers", None)
    if isinstance(include_peers, list):
        for peer in include_peers:
            try:
                peer_ids.add(int(utils.get_peer_id(peer)))
            except Exception:
                continue
    return peer_ids


async def _iter_folder_dialogs(client: TelegramClient, folder_filter: DialogFilter, limit: int) -> list[Any]:
    folder_id = int(folder_filter.id)
    try:
        return [dialog async for dialog in client.iter_dialogs(limit=limit or None, folder=folder_id)]
    except errors.FolderIdInvalidError:
        include_ids = _extract_folder_peer_ids(folder_filter)
        if not include_ids:
            raise RuntimeError(
                f"Folder id {folder_id} is not accepted by Telegram and folder has no explicit include_peers."
            )
        out: list[Any] = []
        async for dialog in client.iter_dialogs(limit=None):
            if int(dialog.id) in include_ids:
                out.append(dialog)
                if limit and len(out) >= limit:
                    break
        return out


async def _run() -> None:
    args = _parse_args()
    api_id = os.getenv("TELETHON_API_ID")
    api_hash = os.getenv("TELETHON_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELETHON_API_ID and TELETHON_API_HASH must be set")

    client = TelegramClient(args.session, int(api_id), api_hash)
    await client.start()
    try:
        folder_id = await _resolve_folder_id(client, args.folder)
        filters_result = await client(GetDialogFiltersRequest())
        filters = getattr(filters_result, "filters", filters_result)
        folder_filter = None
        for item in filters:
            if isinstance(item, DialogFilter) and int(item.id) == int(folder_id):
                folder_filter = item
                break
        if folder_filter is None:
            raise RuntimeError(f"Resolved folder id {folder_id}, but folder object could not be loaded.")
        needle = (args.find or args.filter).strip().lower()
        rows: list[dict[str, Any]] = []
        dialogs = await _iter_folder_dialogs(client, folder_filter=folder_filter, limit=args.limit)
        for dialog in dialogs:
            entity = dialog.entity
            title = (dialog.name or "").strip()
            username = str(getattr(entity, "username", "") or "").strip()
            if needle:
                hay = f"{title} {username}".lower()
                if needle not in hay:
                    continue
            rows.append(
                {
                    "chat_id": int(dialog.id),
                    "title": title,
                    "username": username or None,
                    "kind": _entity_kind(entity),
                }
            )

        if args.json:
            payload = {
                "folder": args.folder,
                "folder_id": folder_id,
                "count": len(rows),
                "dialogs": rows,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        print(f"Folder: {args.folder} (id={folder_id})")
        if not rows:
            print("No dialogs matched.")
            return
        print("chat_id        kind     username             title")
        for row in rows:
            username = row["username"] or "-"
            print(f"{row['chat_id']:>13}  {row['kind']:<7}  @{username:<19}  {row['title']}")
    finally:
        await client.disconnect()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
