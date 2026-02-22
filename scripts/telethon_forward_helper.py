#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon import errors, utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scambaiter.telethon_lookup import resolve_unique_dialog

SCAMMERS_FOLDER = "Scammers"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward messages from a Telegram source chat into the ScamBaiter control bot."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source", help="Source chat username or ID to mirror.")
    source_group.add_argument(
        "--source-query",
        help="Resolve source chat from Scammers folder by fuzzy match against title/username.",
    )
    parser.add_argument(
        "--target",
        help=(
            "Control target that receives the forwards (chat id or username). "
            "Default: SCAMBAITER_CONTROL_TARGET, then SCAMBAITER_CONTROL_BOT_USERNAME, "
            "then @ScamBaiterControlBot."
        ),
    )
    parser.add_argument(
        "--allow-self-target",
        action="store_true",
        help="Allow forwarding to your own account (Saved Messages). Disabled by default for safety.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max number of messages to forward (0 = all).")
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds between forwards to avoid flood limits.")
    parser.add_argument("--session", default="scambaiter.session", help="Telethon session filename.")
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


async def _resolve_folder_filter(client: TelegramClient, folder_name: str) -> DialogFilter:
    filters_result = await client(GetDialogFiltersRequest())
    filters = getattr(filters_result, "filters", filters_result)
    folder_id: int | None = None
    for item in filters:
        if isinstance(item, DialogFilter) and _folder_title(item) == folder_name:
            folder_id = int(item.id)
            break
    if folder_id is None:
        raise RuntimeError(f"Telegram folder not found: {folder_name!r}")
    for item in filters:
        if isinstance(item, DialogFilter) and int(item.id) == folder_id:
            return item
    raise RuntimeError(f"Resolved folder id {folder_id}, but folder object could not be loaded.")


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


async def _iter_folder_dialogs(client: TelegramClient, folder_filter: DialogFilter) -> list[Any]:
    folder_id = int(folder_filter.id)
    try:
        return [dialog async for dialog in client.iter_dialogs(limit=None, folder=folder_id)]
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
        return out


async def _resolve_source_from_query(client: TelegramClient, query: str) -> tuple[Any, str]:
    folder_filter = await _resolve_folder_filter(client, SCAMMERS_FOLDER)
    dialogs = await _iter_folder_dialogs(client, folder_filter=folder_filter)
    entities_by_chat_id: dict[int, Any] = {}
    rows: list[dict[str, Any]] = []
    for dialog in dialogs:
        entity = dialog.entity
        chat_id = int(dialog.id)
        entities_by_chat_id[chat_id] = entity
        rows.append(
            {
                "chat_id": chat_id,
                "title": (dialog.name or "").strip(),
                "username": str(getattr(entity, "username", "") or "").strip() or None,
            }
        )

    status, matches = resolve_unique_dialog(rows=rows, query=query)
    if status == "single":
        row = matches[0]
        chat_id = int(row["chat_id"])
        username = row.get("username") or "-"
        label = f"{chat_id} @{username} {row.get('title') or ''}".strip()
        entity = entities_by_chat_id.get(chat_id)
        if entity is None:
            raise RuntimeError(f"Resolved chat_id {chat_id}, but entity lookup failed.")
        return entity, label
    if status == "none":
        raise RuntimeError(f"No matches in folder {SCAMMERS_FOLDER!r} for query {query!r}.")

    lines = [f"Multiple matches in folder {SCAMMERS_FOLDER!r} for query {query!r}:"]
    for row in matches[:10]:
        username = row.get("username") or "-"
        lines.append(f"  {row['chat_id']}  @{username}  {row['title']}")
    lines.append("Refine --source-query or use explicit --source <chat_id>.")
    raise RuntimeError("\n".join(lines))


async def _run() -> None:
    args = _parse_args()
    api_id = os.getenv("TELETHON_API_ID")
    api_hash = os.getenv("TELETHON_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELETHON_API_ID and TELETHON_API_HASH must be set")
    target_ref = (
        args.target
        or os.getenv("SCAMBAITER_CONTROL_TARGET")
        or os.getenv("SCAMBAITER_CONTROL_BOT_USERNAME")
        or "@ScamBaiterControlBot"
    )
    if not str(target_ref).strip():
        raise RuntimeError("Target is required (CLI --target, env SCAMBAITER_CONTROL_TARGET, or bot username).")

    session_file = args.session
    client = TelegramClient(session_file, int(api_id), api_hash)
    await client.start()
    try:
        me = await client.get_me()
        my_user_id = int(getattr(me, "id", 0) or 0)
        target_lookup: Any
        if str(target_ref).strip().isdigit():
            target_lookup = int(str(target_ref).strip())
        else:
            target_lookup = str(target_ref).strip()
        target_entity = await client.get_entity(target_lookup)
        target_entity_id = int(getattr(target_entity, "id", 0) or 0)
        if target_entity_id == my_user_id and not args.allow_self_target:
            raise RuntimeError(
                "Refusing to forward to your own account (Saved Messages). "
                "Use --target <ScamBaiterControlChatId> or pass --allow-self-target explicitly."
            )

        source_ref = args.source
        source_label = str(source_ref or "")
        if args.source_query:
            entity, source_label = await _resolve_source_from_query(client, args.source_query)
            print(f"resolved source: {source_label}")
        else:
            assert source_ref is not None
            if str(source_ref).strip().isdigit():
                entity = await client.get_entity(int(str(source_ref).strip()))
            else:
                entity = await client.get_entity(source_ref)
        count = 0
        async for message in client.iter_messages(entity, reverse=True, limit=args.limit or None):
            await client.forward_messages(target_entity, message)
            count += 1
            if args.delay > 0:
                await asyncio.sleep(args.delay)
        print(f"forwarded {count} messages from {source_label} to {target_ref}")
    finally:
        await client.disconnect()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
