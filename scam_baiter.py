#!/usr/bin/env python3
"""Telegram Scambaiter suggestion tool.

Reads chats from Telegram folder "Scammers", finds unanswered conversations,
and generates suggested replies via Hugging Face Inference API.
"""

from __future__ import annotations

import os
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List

from huggingface_hub import InferenceClient
from telethon import TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter
from telethon.utils import get_peer_id

SYSTEM_PROMPT = (
    "Du bist eine Scambaiting-AI. Jemand versucht dir auf Telegram zu schreiben, "
    "du sollst kreative Gespräche aufbauen um ihn so lange wie möglich hinzuhalten"
)

RESPONSE_FORMAT_INSTRUCTION = (
    'Gib die Ausgabe exakt in diesem Format zurück:\n'
    'ANALYSE: <maximal 2 kurze Stichpunkte>\n'
    'ANTWORT: <genau eine sendefertige Telegram-Nachricht auf Deutsch>'
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


def _debug_enabled() -> bool:
    return os.getenv("SCAMBAITER_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug(message: str) -> None:
    if _debug_enabled():
        print(f"[DEBUG] {message}")


async def get_scammers_folder_chat_ids(client: TelegramClient) -> set[int]:
    result = await client(GetDialogFiltersRequest())
    wanted = _normalize_folder_title(FOLDER_NAME)

    _debug(f"Erhaltene Filter-Anzahl: {len(result.filters)}")
    for index, item in enumerate(result.filters, start=1):
        if isinstance(item, DialogFilter):
            title = getattr(item.title, "text", str(item.title))
            _debug(
                f"Filter #{index}: title='{title}', "
                f"include_peers={len(item.include_peers)}, pinned_peers={len(item.pinned_peers)}, "
                f"exclude_peers={len(item.exclude_peers)}"
            )
        else:
            _debug(f"Filter #{index}: Typ {type(item).__name__} (übersprungen)")

    for item in result.filters:
        if not isinstance(item, DialogFilter):
            continue

        title = getattr(item.title, "text", str(item.title))
        if _normalize_folder_title(title) != wanted:
            continue

        chat_ids: set[int] = set()
        for peer in item.include_peers:
            peer_id = get_peer_id(peer)
            chat_ids.add(peer_id)
            _debug(f"Folder-include peer_id: {peer_id}")

        _debug(f"Gematchter Folder '{title}' mit {len(chat_ids)} include_peers")
        return chat_ids

    raise ValueError(f'Telegram-Ordner "{FOLDER_NAME}" wurde nicht gefunden.')


async def collect_unanswered_chats(
    client: TelegramClient,
    folder_chat_ids: set[int],
) -> List[ChatContext]:
    me = await client.get_me()
    my_id = me.id
    contexts: List[ChatContext] = []

    _debug(f"Folder-Chat-IDs: {sorted(folder_chat_ids)}")

    async for dialog in client.iter_dialogs():
        _debug(
            f"Dialog: title='{dialog.title}', dialog.id={dialog.id}, unread_count={dialog.unread_count}, "
            f"has_message={dialog.message is not None}"
        )

        if dialog.id not in folder_chat_ids:
            _debug(f" -> übersprungen (nicht im Folder): {dialog.id}")
            continue

        if dialog.message is None:
            _debug(" -> übersprungen (keine letzte Nachricht)")
            continue

        last_sender_id = getattr(dialog.message, "sender_id", None)
        if last_sender_id == my_id:
            _debug(" -> übersprungen (letzte Nachricht von mir)")
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
            _debug(f" -> aufgenommen: {dialog.title} ({dialog.id}), lines={len(lines)}")
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
        "Die Antwort soll glaubwürdig, freundlich und scambaiting-geeignet sein.\n"
        f"{RESPONSE_FORMAT_INSTRUCTION}\n\n"
        f"Chatverlauf:\n{chat_history}"
    )


def _build_text_generation_prompt(context: ChatContext) -> str:
    return (
        f"System: {SYSTEM_PROMPT}\n\n"
        "Du bekommst genau einen einzelnen Telegram-Chat. "
        "Nutze ausschließlich diesen Verlauf und kein externes Wissen über andere Chats.\n"
        "Nutze für die Ausgabe zwingend dieses Schema mit klarer Trennung:\n"
        f"{RESPONSE_FORMAT_INSTRUCTION}\n\n"
        f"{build_user_prompt(context)}\n\n"
        "Antwort:"
    )


def _strip_think_segments(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = cleaned.replace("<think>", "").replace("</think>", "")
    return cleaned.strip()


def _extract_final_reply(text: str) -> str:
    cleaned = _strip_think_segments(text)

    match = re.search(r"ANTWORT\s*:\s*(.+)", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if match:
        reply = match.group(1).strip()
        reply = re.split(r"\n(?:ANALYSE|HINWEIS|NOTE)\s*:", reply, maxsplit=1, flags=re.IGNORECASE)[0]
        return reply.strip()

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    filtered = [
        line for line in lines
        if not re.match(r"^(ANALYSE|HINWEIS|NOTE|GEDANKE|THOUGHT)\s*:", line, flags=re.IGNORECASE)
    ]
    return "\n".join(filtered).strip()


def _apply_suggestion_callback(
    raw_text: str,
    callback: Callable[[str], str] | None = None,
) -> str:
    processor = callback or _extract_final_reply
    return processor(raw_text)


def generate_suggestion(
    hf_client: InferenceClient,
    model: str,
    context: ChatContext,
    suggestion_callback: Callable[[str], str] | None = None,
) -> str:
    try:
        completion = hf_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{RESPONSE_FORMAT_INSTRUCTION}"},
                {"role": "user", "content": build_user_prompt(context)},
            ],
        )
        return _apply_suggestion_callback(completion.choices[0].message.content, suggestion_callback)
    except Exception as exc:
        _debug(f"Chat-Completions fehlgeschlagen ({type(exc).__name__}): fallback auf text_generation")
        text = hf_client.text_generation(
            model=model,
            prompt=_build_text_generation_prompt(context),
            max_new_tokens=180,
            temperature=0.9,
            return_full_text=False,
        )
        return _apply_suggestion_callback(text, suggestion_callback)


async def _send_message_with_optional_delete(client: TelegramClient, context: ChatContext, message: str) -> None:
    sent = await client.send_message(context.chat_id, message)
    print(f"[SEND] Nachricht an {context.title} gesendet (msg_id={sent.id}).")

    delete_after_seconds = _env_int("SCAMBAITER_DELETE_OWN_AFTER_SECONDS", default=0)
    if delete_after_seconds <= 0:
        return

    print(f"[SEND] Lösche gesendete Nachricht in {delete_after_seconds} Sekunden wieder.")
    await asyncio.sleep(delete_after_seconds)
    await client.delete_messages(context.chat_id, [sent.id])
    print(f"[SEND] Nachricht {sent.id} in {context.title} gelöscht.")


async def maybe_send_suggestion(client: TelegramClient, context: ChatContext, suggestion: str) -> None:
    if not _env_flag("SCAMBAITER_SEND"):
        return

    if os.getenv("SCAMBAITER_SEND_CONFIRM") != "SEND":
        print(
            "[WARN] SCAMBAITER_SEND ist aktiv, aber SCAMBAITER_SEND_CONFIRM != 'SEND'. "
            "Sende daher nicht automatisch."
        )
        return

    await _send_message_with_optional_delete(client, context, suggestion)


async def maybe_interactive_console_reply(client: TelegramClient, context: ChatContext, suggestion: str) -> bool:
    if not _env_flag("SCAMBAITER_INTERACTIVE", default=True):
        return False

    if not os.isatty(0):
        print("[WARN] SCAMBAITER_INTERACTIVE ist aktiv, aber kein TTY verfügbar. Überspringe Interaktiv-Modus.")
        return False

    print("Aktion: [Enter]=nicht senden | s=Vorschlag senden | e=editieren+senden")
    choice = input("> ").strip().lower()
    if choice == "s":
        await _send_message_with_optional_delete(client, context, suggestion)
        return True

    if choice == "e":
        print("Gib deine Nachricht ein (leere Zeile = Abbruch):")
        custom = input("> ").strip()
        if not custom:
            print("[INFO] Abgebrochen, nichts gesendet.")
            return True
        await _send_message_with_optional_delete(client, context, custom)
        return True

    print("[INFO] Nicht gesendet.")
    return True


async def run(suggestion_callback: Callable[[str], str] | None = None) -> None:
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
        if _debug_enabled():
            print("[DEBUG] Tipp: SCAMBAITER_DEBUG=1 aktiv lassen und prüfen, ob Dialog-IDs zu Folder-IDs passen.")
        return

    print(f"Gefundene unbeantwortete Chats: {len(contexts)}\n")
    for index, context in enumerate(contexts, start=1):
        _debug(f"Verarbeite Chat isoliert: {context.title} (ID: {context.chat_id})")
        suggestion = generate_suggestion(
            hf_client,
            model=hf_model,
            context=context,
            suggestion_callback=suggestion_callback,
        )
        print(f"=== Vorschlag {index}: {context.title} (ID: {context.chat_id}) ===")
        print(suggestion)
        print()
        handled_interactively = await maybe_interactive_console_reply(client, context, suggestion)
        if not handled_interactively:
            await maybe_send_suggestion(client, context, suggestion)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Umgebungsvariable fehlt: {name}")
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
