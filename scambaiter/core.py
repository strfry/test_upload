from __future__ import annotations

import asyncio
import base64
import hashlib
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
from scambaiter.storage import AnalysisStore


IMAGE_MARKER_PREFIX = "[Bild gesendet"

SYSTEM_PROMPT = (
    "Du bist eine Scambaiting-AI in der Rolle einer potenziellen Scam-Zielperson. "
    "Die andere Person im Chat ist der vermutete Scammer. Du darfst niemals selbst scammen, "
    "betrügen, erpressen oder Social-Engineering gegen die andere Person betreiben. "
    "Dein einziges Ziel ist, den Scammer mit plausiblen, harmlosen Antworten möglichst lange "
    "in ein Gespräch zu verwickeln. Nutze nur den bereitgestellten Chatverlauf. "
    "Gib die Ausgabe vorzugsweise in drei Zeilen aus: 'ANALYSE: ...', 'META: ...' und 'ANTWORT: ...'. "
    "Wichtige Struktur für META: mindestens der Key 'sprache' (z.B. 'META: sprache=de'). "
    "Weitere Key-Value-Infos sind erlaubt (Format: key=value;key2=value2). "
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


def _normalize_text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    parts.append(text_value.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts)
    return ""


class ScambaiterCore:
    def __init__(self, config: AppConfig, store: AnalysisStore | None = None) -> None:
        self.config = config
        self.store = store
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
                line = await self._format_message_line(message, my_id=my_id, dialog_title=dialog.title)
                if line:
                    lines.append(line)

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

    async def describe_image(self, image_bytes: bytes) -> str | None:
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        if self.store:
            cached = self.store.image_description_get(image_hash)
            if cached and cached.description.strip():
                return cached.description.strip()

        encoded = base64.b64encode(image_bytes).decode("ascii")
        completion = self.hf_client.chat.completions.create(
            model=self.config.hf_vision_model,
            max_tokens=240,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Beschreibe das Bild ausführlich, konkret und wohlwollend auf Deutsch. "
                                "Nutze 2-4 Sätze mit gut beobachtbaren Details ohne Spekulationen."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                    ],
                }
            ],
        )
        content = completion.choices[0].message.content
        description = _normalize_text_content(content).strip()
        if not description:
            return None

        if self.store:
            self.store.image_description_set(image_hash, description)
        return description

    async def _format_message_line(self, message, my_id: int, dialog_title: str) -> str | None:
        text = (message.message or "").strip()
        sender = "Ich" if message.sender_id == my_id else dialog_title

        if getattr(message, "photo", None) and message.sender_id != my_id:
            marker = IMAGE_MARKER_PREFIX + "]"
            try:
                image_bytes = await self.client.download_media(message, file=bytes)
                if image_bytes:
                    description = await self.describe_image(image_bytes)
                    if description:
                        marker = f"{IMAGE_MARKER_PREFIX}: {description}]"
            except Exception as exc:
                self._debug(f"Bildbeschreibung fehlgeschlagen (msg_id={message.id}): {exc}")

            if text:
                return f"[{self._fmt_dt(message.date)}] {sender}: {marker} {text}"
            return f"[{self._fmt_dt(message.date)}] {sender}: {marker}"

        if text:
            return f"[{self._fmt_dt(message.date)}] {sender}: {text}"

        return None

    def generate_suggestion(self, context: ChatContext, suggestion_callback: Callable[[str], str] | None = None) -> str:
        return self.generate_output(context, suggestion_callback=suggestion_callback).suggestion

    @staticmethod
    def build_system_prompt(language_hint: str | None = None) -> str:
        prompt = SYSTEM_PROMPT
        if not language_hint:
            return prompt

        lang = language_hint.strip().lower()
        if lang in {"en", "english", "englisch"}:
            return prompt + " You must respond exclusively in English."
        if lang in {"de", "deutsch", "german"}:
            return prompt + " Du antwortest immer auf Deutsch."
        return prompt

    def generate_output(
        self,
        context: ChatContext,
        suggestion_callback: Callable[[str], str] | None = None,
        language_hint: str | None = None,
    ) -> ModelOutput:
        completion = self.hf_client.chat.completions.create(
            model=self.config.hf_model,
            max_tokens=self.config.hf_max_tokens,
            messages=[
                {"role": "system", "content": self.build_system_prompt(language_hint)},
                {"role": "user", "content": self.build_user_prompt(context)},
            ],
        )
        content = completion.choices[0].message.content
        raw = _normalize_text_content(content)
        suggestion = (suggestion_callback or extract_final_reply)(raw).strip()
        analysis = extract_analysis(raw)
        metadata = extract_metadata(raw)
        return ModelOutput(raw=raw, suggestion=suggestion, analysis=analysis, metadata=metadata)

    async def maybe_send_suggestion(self, context: ChatContext, suggestion: str) -> bool:
        if not self.config.send_enabled:
            return False
        if self.config.send_confirm != "SEND":
            print("[WARN] SCAMBAITER_SEND aktiv, aber SCAMBAITER_SEND_CONFIRM != 'SEND'.")
            return False
        if not suggestion.strip():
            print(f"[WARN] Leerer Antwortvorschlag für {context.title} (ID: {context.chat_id}) - Nachricht wird nicht gesendet.")
            return False
        await self.send_message_with_optional_delete(context, suggestion)
        return True

    async def send_message_with_optional_delete(self, context: ChatContext, message: str) -> None:
        if not message.strip():
            print(f"[WARN] Leere Nachricht für {context.title} (ID: {context.chat_id}) verworfen.")
            return
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
    lines = cleaned.splitlines()
    start_idx: int | None = None
    collected: list[str] = []

    for idx, line in enumerate(lines):
        match = re.match(r"^\s*ANALYSE\s*:\s*(.*)$", line, flags=re.IGNORECASE)
        if match:
            start_idx = idx
            first = match.group(1).strip()
            if first:
                collected.append(first)
            break

    if start_idx is None:
        return None

    for line in lines[start_idx + 1 :]:
        if re.match(r"^\s*(?:META|ANTWORT|REPLY)\s*:", line, flags=re.IGNORECASE):
            break
        collected.append(line.strip())

    analysis = "\n".join(part for part in collected if part).strip()
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
