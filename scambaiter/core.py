from __future__ import annotations

import asyncio
import json
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
    "Gib die Ausgabe vorzugsweise in drei Zeilen aus: 'ANALYSE: ...', optional 'META: key=value;key2=value2' und 'ANTWORT: ...'. "
    "Nutze META für strukturierte Key-Value-Infos (z.B. sprache=de), wenn sinnvoll. "
    "Die ANTWORT muss genau eine sendefertige Telegram-Nachricht enthalten. "
    "Vermeide KI-typische Ausgaben, insbesondere Emojis und den langen Gedankenstrich (—)."
)


@dataclass
class ChatContext:
    chat_id: int
    title: str
    lines: list[str]


@dataclass
class SuggestionResult:
    context: ChatContext
    suggestion: str
    analysis: str | None = None
    metadata: dict[str, str] | None = None


@dataclass
class ModelOutput:
    raw: str
    suggestion: str
    analysis: str | None
    metadata: dict[str, str]


class ScambaiterCore:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client = TelegramClient(config.telegram_session, config.telegram_api_id, config.telegram_api_hash)
        self.hf_client = InferenceClient(api_key=config.hf_token, base_url=config.hf_base_url)

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
        me = await self.client.get_me()
        my_id = me.id
        contexts: list[ChatContext] = []

        async for dialog in self.client.iter_dialogs():
            if dialog.id not in folder_chat_ids or dialog.message is None:
                continue

            if getattr(dialog.message, "sender_id", None) == my_id:
                continue

            messages = await self.client.get_messages(dialog.entity, limit=self.config.history_limit)
            lines: list[str] = []
            for message in reversed(messages):
                text = message.message or ""
                if not text.strip():
                    continue
                sender = "Ich" if message.sender_id == my_id else dialog.title
                lines.append(f"[{self._fmt_dt(message.date)}] {sender}: {text}")

            if lines:
                contexts.append(ChatContext(chat_id=dialog.id, title=dialog.title, lines=lines))

        self._debug(f"Unbeantwortete Chats gefunden: {len(contexts)}")
        return contexts

    def build_user_prompt(self, context: ChatContext) -> str:
        history = "\n".join(context.lines)
        return (
            f"Konversation mit {context.title} (Telegram Chat-ID: {context.chat_id})\n\n"
            f"Chatverlauf:\n{history}"
        )

    def generate_suggestion(self, context: ChatContext, suggestion_callback: Callable[[str], str] | None = None) -> str:
        return self.generate_output(context, suggestion_callback=suggestion_callback).suggestion

    def generate_output(
        self,
        context: ChatContext,
        suggestion_callback: Callable[[str], str] | None = None,
    ) -> ModelOutput:
        completion = self.hf_client.chat.completions.create(
            model=self.config.hf_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self.build_user_prompt(context)},
            ],
        )
        raw = completion.choices[0].message.content
        suggestion = (suggestion_callback or extract_final_reply)(raw)
        analysis = extract_analysis(raw)
        metadata = extract_metadata(raw)
        return ModelOutput(raw=raw, suggestion=suggestion, analysis=analysis, metadata=metadata)

    async def maybe_send_suggestion(self, context: ChatContext, suggestion: str) -> bool:
        if not self.config.send_enabled:
            return False
        if self.config.send_confirm != "SEND":
            print("[WARN] SCAMBAITER_SEND aktiv, aber SCAMBAITER_SEND_CONFIRM != 'SEND'.")
            return False
        await self.send_message_with_optional_delete(context, suggestion)
        return True

    async def send_message_with_optional_delete(self, context: ChatContext, message: str) -> None:
        sent = await self.client.send_message(context.chat_id, message)
        print(f"[SEND] Nachricht an {context.title} gesendet (msg_id={sent.id}).")
        if self.config.delete_after_seconds > 0:
            await asyncio.sleep(self.config.delete_after_seconds)
            await self.client.delete_messages(context.chat_id, [sent.id])
            print(f"[SEND] Nachricht {sent.id} in {context.title} gelöscht.")

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


def extract_analysis(text: str) -> str | None:
    cleaned = strip_think_segments(text)
    match = re.search(r"ANALYSE\s*:\s*(.+)", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    analysis = re.split(r"\n(?:ANTWORT|REPLY|META)\s*:", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
    analysis = analysis.strip()
    return analysis or None


def extract_metadata(text: str) -> dict[str, str]:
    cleaned = strip_think_segments(text)
    metadata: dict[str, str] = {}

    json_match = re.search(r"META\s*:\s*(\{.+?\})", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if isinstance(data, dict):
                for key, value in data.items():
                    k = str(key).strip().lower()
                    v = str(value).strip()
                    if k and v:
                        metadata[k] = v
        except json.JSONDecodeError:
            pass

    line_match = re.search(r"META\s*:\s*(.+)", cleaned, flags=re.IGNORECASE)
    if line_match:
        for part in line_match.group(1).split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                metadata[key] = value

    return metadata
