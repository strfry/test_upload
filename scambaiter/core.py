from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from huggingface_hub import InferenceClient
from telethon import TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter
from telethon.utils import get_peer_id

from scambaiter.config import AppConfig

SYSTEM_PROMPT = (
    "Du bist eine Scambaiting-AI in der Rolle einer potenziellen Scam-Zielperson. "
    "Die andere Person im Chat ist der vermutete Scammer. Du darfst niemals selbst scammen, "
    "betrügen, erpressen oder Social-Engineering gegen die andere Person betreiben. "
    "Dein einziges Ziel ist, den Scammer mit plausiblen, harmlosen Antworten möglichst lange "
    "in ein Gespräch zu verwickeln. Nutze nur den bereitgestellten Chatverlauf. "
    "Antworte mit genau einer sendefertigen Telegram-Nachricht auf Deutsch und ohne Zusatztexte. "
    "Vermeide KI-typische Ausgaben, insbesondere Emojis und den langen Gedankenstrich (—)."
)


@dataclass
class ChatContext:
    chat_id: int
    title: str
    lines: list[str]
    pending_incoming_chars: int
    last_incoming_message_id: int | None


@dataclass
class SuggestionResult:
    context: ChatContext
    suggestion: str


class ScambaiterCore:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client = TelegramClient(config.telegram_session, config.telegram_api_id, config.telegram_api_hash)
        self.hf_client = InferenceClient(api_key=config.hf_token, base_url=config.hf_base_url)
        self._my_id: int | None = None

    async def start(self) -> None:
        await self.client.start()

    async def close(self) -> None:
        await self.client.disconnect()

    def _debug(self, message: str) -> None:
        if self.config.debug_enabled:
            print(f"[DEBUG] {message}")

    async def get_folder_chat_ids(self) -> set[int]:
        result = await self.client(GetDialogFiltersRequest())
        wanted = self.config.folder_name.strip().lower()

        for item in result.filters:
            if not isinstance(item, DialogFilter):
                continue
            title = getattr(item.title, "text", str(item.title))
            if title.strip().lower() != wanted:
                continue

            chat_ids: set[int] = set()
            for peer in item.include_peers:
                chat_ids.add(get_peer_id(peer))
            return chat_ids

        raise ValueError(f'Telegram-Ordner "{self.config.folder_name}" wurde nicht gefunden.')

    async def collect_unanswered_chats(self, folder_chat_ids: set[int]) -> list[ChatContext]:
        my_id = await self._get_my_id()
        contexts: list[ChatContext] = []

        async for dialog in self.client.iter_dialogs():
            if dialog.id not in folder_chat_ids or dialog.message is None:
                continue

            if getattr(dialog.message, "sender_id", None) == my_id:
                continue

            messages = await self.client.get_messages(dialog.entity, limit=self.config.history_limit)
            context = self._build_chat_context(
                chat_id=dialog.id,
                title=dialog.title,
                my_id=my_id,
                messages=messages,
            )
            if context:
                contexts.append(context)

        self._debug(f"Unbeantwortete Chats gefunden: {len(contexts)}")
        return contexts

    def build_user_prompt(self, context: ChatContext) -> str:
        history = "\n".join(context.lines)
        return (
            f"Konversation mit {context.title} (Telegram Chat-ID: {context.chat_id})\n\n"
            f"Chatverlauf:\n{history}"
        )

    def generate_suggestion(self, context: ChatContext, suggestion_callback: Callable[[str], str] | None = None) -> str:
        completion = self.hf_client.chat.completions.create(
            model=self.config.hf_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self.build_user_prompt(context)},
            ],
        )
        raw = completion.choices[0].message.content
        return (suggestion_callback or extract_final_reply)(raw)

    async def maybe_send_suggestion(self, context: ChatContext, suggestion: str) -> bool:
        if not self.config.send_enabled:
            return False
        if self.config.send_confirm != "SEND":
            print("[WARN] SCAMBAITER_SEND aktiv, aber SCAMBAITER_SEND_CONFIRM != 'SEND'.")
            return False
        current_context = context
        current_suggestion = suggestion

        for attempt in range(3):
            sent = await self.send_message_with_optional_delete(current_context, current_suggestion)
            if sent:
                return True

            refreshed = await self.refresh_chat_context(current_context.chat_id, current_context.title)
            if refreshed is None:
                self._debug(f"Chat {current_context.chat_id}: inzwischen beantwortet, kein Senden nötig.")
                return False
            current_context = refreshed
            current_suggestion = self.generate_suggestion(current_context)
            self._debug(f"Chat {current_context.chat_id}: neue Nachricht erkannt, antworte neu (Versuch {attempt + 2}/3).")

        return False

    async def send_message_with_optional_delete(self, context: ChatContext, message: str) -> bool:
        if await self._wait_for_reply_window(context):
            return False

        typing_seconds = calculate_typing_delay_seconds(len(message))
        if typing_seconds > 0:
            async with self.client.action(context.chat_id, "typing"):
                await asyncio.sleep(typing_seconds)
                if await self._has_new_incoming_message(context):
                    return False

        sent = await self.client.send_message(context.chat_id, message)
        print(f"[SEND] Nachricht an {context.title} gesendet (msg_id={sent.id}).")
        if self.config.delete_after_seconds > 0:
            await asyncio.sleep(self.config.delete_after_seconds)
            await self.client.delete_messages(context.chat_id, [sent.id])
            print(f"[SEND] Nachricht {sent.id} in {context.title} gelöscht.")
        return True

    async def maybe_interactive_console_reply(self, context: ChatContext, suggestion: str) -> bool:
        if not self.config.interactive_enabled:
            return False
        if not __import__("os").isatty(0):
            print("[WARN] Interaktiv-Modus aktiv, aber kein TTY verfügbar.")
            return False

        print("Aktion: [Enter]=nicht senden | s=Vorschlag senden | e=editieren+senden")
        choice = input("> ").strip().lower()
        if choice == "s":
            await self.send_message_with_optional_delete(context, suggestion)
            return True
        if choice == "e":
            print("Gib deine Nachricht ein (leere Zeile = Abbruch):")
            custom = input("> ").strip()
            if custom:
                await self.send_message_with_optional_delete(context, custom)
            return True
        return True

    async def refresh_chat_context(self, chat_id: int, fallback_title: str = "Chat") -> ChatContext | None:
        my_id = await self._get_my_id()
        entity = await self.client.get_entity(chat_id)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or fallback_title
        messages = await self.client.get_messages(entity, limit=self.config.history_limit)
        return self._build_chat_context(chat_id=chat_id, title=title, my_id=my_id, messages=messages)

    async def _get_my_id(self) -> int:
        if self._my_id is None:
            me = await self.client.get_me()
            self._my_id = me.id
        return self._my_id

    def _build_chat_context(self, chat_id: int, title: str, my_id: int, messages: list) -> ChatContext | None:
        lines: list[str] = []
        pending_chars = 0
        latest_incoming_id: int | None = None

        for message in reversed(messages):
            text = message.message or ""
            if not text.strip():
                continue
            sender = "Ich" if message.sender_id == my_id else title
            lines.append(f"[{self._fmt_dt(message.date)}] {sender}: {text}")

        for message in messages:
            if message.sender_id == my_id:
                break
            text = (message.message or "").strip()
            if not text:
                continue
            pending_chars += len(text)
            if latest_incoming_id is None:
                latest_incoming_id = message.id

        if not lines:
            return None

        return ChatContext(
            chat_id=chat_id,
            title=title,
            lines=lines,
            pending_incoming_chars=pending_chars,
            last_incoming_message_id=latest_incoming_id,
        )

    async def _wait_for_reply_window(self, context: ChatContext) -> bool:
        wait_seconds = calculate_response_delay_seconds(context.pending_incoming_chars)
        elapsed = 0.0
        while elapsed < wait_seconds:
            sleep_for = min(0.8, wait_seconds - elapsed)
            await asyncio.sleep(sleep_for)
            elapsed += sleep_for
            if await self._has_new_incoming_message(context):
                return True
        return False

    async def _has_new_incoming_message(self, context: ChatContext) -> bool:
        latest = await self.client.get_messages(context.chat_id, limit=1)
        if not latest:
            return False
        newest = latest[0]
        if newest.id == context.last_incoming_message_id:
            return False
        my_id = await self._get_my_id()
        return newest.sender_id != my_id

    @staticmethod
    def _fmt_dt(dt: datetime | None) -> str:
        if dt is None:
            return "?"
        return dt.strftime("%Y-%m-%d %H:%M")


def strip_think_segments(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.replace("<think>", "").replace("</think>", "").strip()


def strip_wrapping_quotes(text: str) -> str:
    pairs = [('"', '"'), ("'", "'"), ("„", "“"), ("“", "”"), ("«", "»")]
    cleaned = text.strip()
    changed = True
    while changed and len(cleaned) >= 2:
        changed = False
        for left, right in pairs:
            if cleaned.startswith(left) and cleaned.endswith(right):
                cleaned = cleaned[len(left) : -len(right)].strip()
                changed = True
                break
    return cleaned


def extract_final_reply(text: str) -> str:
    cleaned = strip_think_segments(text)
    match = re.search(r"(?:ANTWORT|REPLY)\s*:\s*(.+)", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if match:
        reply = match.group(1).strip()
        reply = re.split(r"\n(?:ANALYSE|HINWEIS|NOTE)\s*:", reply, maxsplit=1, flags=re.IGNORECASE)[0]
        return strip_wrapping_quotes(reply)

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    filtered = [
        line
        for line in lines
        if not re.match(r"^(ANALYSE|HINWEIS|NOTE|GEDANKE|THOUGHT)\s*:", line, flags=re.IGNORECASE)
    ]
    return strip_wrapping_quotes("\n".join(filtered).strip())


def calculate_response_delay_seconds(incoming_chars: int) -> float:
    normalized = max(0, incoming_chars)
    return min(45.0, max(1.5, normalized / 24))


def calculate_typing_delay_seconds(outgoing_chars: int) -> float:
    normalized = max(0, outgoing_chars)
    return min(30.0, max(1.0, normalized / 14))
