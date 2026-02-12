#!/usr/bin/env python3
"""Telegram Scambaiter suggestion tool.

Reads chats from Telegram folder "Scammers", finds unanswered conversations,
and generates suggested replies via Hugging Face Inference API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import List

from huggingface_hub import InferenceClient
from telethon import TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

SYSTEM_PROMPT = (
    "Du bist eine Scambaiting-AI. Jemand versucht dir auf Telegram zu schreiben, "
    "du sollst kreative Gespräche aufbauen um ihn so lange wie möglich hinzuhalten"
)

FOLDER_NAME = "Scammers"
HISTORY_LIMIT = 20


@dataclass
class ChatContext:
    chat_id: int
    title: str
    lines: List[str]


def _normalize_folder_title(value: str) -> str:
    return value.strip().lower()


async def get_scammers_folder_chat_ids(client: TelegramClient) -> set[int]:
    result = await client(GetDialogFiltersRequest())
    wanted = _normalize_folder_title(FOLDER_NAME)

    for item in result.filters:
        if not isinstance(item, DialogFilter):
            continue
        title = getattr(item.title, "text", str(item.title))
        if _normalize_folder_title(title) != wanted:
            continue

        chat_ids: set[int] = set()
        for peer in item.include_peers:
            entity = await client.get_entity(peer)
            chat_ids.add(entity.id)
        return chat_ids

    raise ValueError(f'Telegram-Ordner "{FOLDER_NAME}" wurde nicht gefunden.')


async def collect_unanswered_chats(
    client: TelegramClient,
    folder_chat_ids: set[int],
) -> List[ChatContext]:
    me = await client.get_me()
    my_id = me.id
    contexts: List[ChatContext] = []

    async for dialog in client.iter_dialogs():
        if dialog.id not in folder_chat_ids:
            continue

        if dialog.message is None:
            continue

        last_sender_id = getattr(dialog.message, "sender_id", None)
        if last_sender_id == my_id:
            continue

        if dialog.unread_count == 0:
            continue

        messages = await client.get_messages(dialog.entity, limit=HISTORY_LIMIT)
        ordered = list(reversed(messages))
        lines = []
        for message in ordered:
            sender = "Ich" if message.sender_id == my_id else dialog.title
            timestamp = _fmt_dt(message.date)
            text = message.message or ""
            if not text.strip():
                continue
            lines.append(f"[{timestamp}] {sender}: {text}")

        if lines:
            contexts.append(ChatContext(chat_id=dialog.id, title=dialog.title, lines=lines))

    return contexts


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    return dt.strftime("%Y-%m-%d %H:%M")


def build_user_prompt(context: ChatContext) -> str:
    chat_history = "\n".join(context.lines)
    return (
        f"Konversation mit {context.title} (Telegram Chat-ID: {context.chat_id})\n"
        "Schlage genau eine nächste Antwort auf Deutsch vor. "
        "Die Antwort soll glaubwürdig, freundlich und scambaiting-geeignet sein.\n\n"
        f"Chatverlauf:\n{chat_history}"
    )


def generate_suggestion(hf_client: InferenceClient, model: str, context: ChatContext) -> str:
    completion = hf_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(context)},
        ],
    )
    return completion.choices[0].message.content.strip()


async def run() -> None:
    api_id = int(_require_env("TELEGRAM_API_ID"))
    api_hash = _require_env("TELEGRAM_API_HASH")

    session_name = os.getenv("TELEGRAM_SESSION", "scambaiter")
    client = TelegramClient(session_name, api_id, api_hash)

    hf_token = _require_env("HF_TOKEN")
    hf_model = _require_env("HF_MODEL")
    hf_base_url = os.getenv("HF_BASE_URL")
    hf_client = InferenceClient(api_key=hf_token, base_url=hf_base_url)

    await client.start()

    folder_chat_ids = await get_scammers_folder_chat_ids(client)
    contexts = await collect_unanswered_chats(client, folder_chat_ids=folder_chat_ids)

    if not contexts:
        print("Keine unbeantworteten Chats im Ordner gefunden.")
        return

    print(f"Gefundene unbeantwortete Chats: {len(contexts)}\n")
    for index, context in enumerate(contexts, start=1):
        suggestion = generate_suggestion(hf_client, model=hf_model, context=context)
        print(f"=== Vorschlag {index}: {context.title} (ID: {context.chat_id}) ===")
        print(suggestion)
        print()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Umgebungsvariable fehlt: {name}")
    return value


def main() -> None:
    import asyncio

    asyncio.run(run())


if __name__ == "__main__":
    main()
