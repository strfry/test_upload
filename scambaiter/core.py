from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


REPLY_MARKER_PATTERN = re.compile(r"(?:ANTWORT|ANWORT|REPLY)\s*:", flags=re.IGNORECASE)

MAX_GENERATION_ATTEMPTS = 2

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
    "Vermeide KI-typische Ausgaben, insbesondere Emojis und den langen Gedankenstrich (-)."
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

    async def resolve_control_chat_id(self, bot_username: str) -> int:
        me = await self.client.get_me()
        my_id = me.id
        wanted = bot_username.strip().lstrip("@").lower()

        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            username = (getattr(entity, "username", None) or "").strip().lstrip("@").lower()
            if username == wanted:
                return my_id

        raise ValueError(
            f"Bot-Dialog mit @{wanted} nicht gefunden. Bitte den Bot zuerst anschreiben und neu starten."
        )

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

    async def build_chat_context(self, chat_id: int) -> ChatContext | None:
        me = await self.client.get_me()
        my_id = me.id
        entity = await self.client.get_entity(chat_id)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
        messages = await self.client.get_messages(entity, limit=self.config.history_limit)

        lines: list[str] = []
        for message in reversed(messages):
            line = await self._format_message_line(message, my_id=my_id, dialog_title=title)
            if line:
                lines.append(line)

        if not lines:
            return None

        return ChatContext(chat_id=chat_id, title=title, lines=lines)

    def build_user_prompt(self, context: ChatContext) -> str:
        history = "\n".join(context.lines)
        return (
            f"Konversation mit {context.title} (Telegram Chat-ID: {context.chat_id})\n\n"
            f"Chatverlauf:\n{history}"
        )

    def build_prompt_debug_summary(self, context: ChatContext, max_lines: int = 5) -> str:
        image_lines = [line for line in context.lines if IMAGE_MARKER_PREFIX in line]
        head_lines = context.lines[:max_lines]
        tail_lines = context.lines[-max_lines:] if len(context.lines) > max_lines else []

        parts = [
            f"Chat: {context.title} ({context.chat_id})",
            f"Zeilen gesamt: {len(context.lines)}",
            f"Bildzeilen: {len(image_lines)}",
        ]

        if image_lines:
            parts.append("Bildzeilen (gekürzt):")
            for line in image_lines[:max_lines]:
                parts.append("- " + truncate_for_log(line, max_len=220))

        if head_lines:
            parts.append("Anfang des Verlaufs:")
            for line in head_lines:
                parts.append("- " + truncate_for_log(line, max_len=220))

        if tail_lines:
            parts.append("Ende des Verlaufs:")
            for line in tail_lines:
                parts.append("- " + truncate_for_log(line, max_len=220))

        return "\n".join(parts)

    async def describe_recent_images_for_chat(self, chat_id: int, limit: int = 3) -> list[str]:
        messages = await self.client.get_messages(chat_id, limit=max(20, limit * 10))
        descriptions: list[str] = []

        for message in messages:
            is_photo = bool(getattr(message, "photo", None))
            document = getattr(message, "document", None)
            mime_type = getattr(document, "mime_type", "") if document else ""
            is_image_document = bool(mime_type and mime_type.startswith("image/"))
            if not is_photo and not is_image_document:
                continue

            image_bytes = await self.client.download_media(message, file=bytes)
            if not image_bytes:
                continue

            description = await self.describe_image(image_bytes)
            if description:
                marker = "photo" if is_photo else f"document:{mime_type}"
                descriptions.append(
                    f"msg_id={message.id} ({marker}): {truncate_for_log(description, max_len=300)}"
                )
            if len(descriptions) >= limit:
                break

        return descriptions

    async def describe_image(self, image_bytes: bytes) -> str | None:
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        if self.store:
            cached = self.store.image_description_get(image_hash)
            if cached and cached.description.strip():
                cached_description = extract_image_description(cached.description)
                if cached_description:
                    if cached_description != cached.description:
                        self.store.image_description_set(image_hash, cached_description)
                    return cached_description

        encoded = base64.b64encode(image_bytes).decode("ascii")
        completion = self.hf_client.chat.completions.create(
            model=self.config.hf_vision_model,
            max_tokens=720,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You are an image captioning assistant. "
                                "Return final answer only in this exact format: "
                                "DESCRIPTION: <2-4 complete sentences>. "
                                "Use concrete observable details, keep a benevolent tone, no speculation. "
                                "Do not include analysis, planning, bullets, or meta text."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                    ],
                }
            ],
        )
        content = completion.choices[0].message.content
        raw_description = _normalize_text_content(content).strip()
        description = extract_image_description(raw_description)
        if not description:
            self._debug(f"Ungültige Bildbeschreibung verworfen: {truncate_for_log(raw_description, max_len=600)}")
            return None

        if self.store:
            self.store.image_description_set(image_hash, description)
        return description

    async def _format_message_line(self, message, my_id: int, dialog_title: str) -> str | None:
        text = (message.message or "").strip()
        sender = "Ich" if message.sender_id == my_id else dialog_title

        document = getattr(message, "document", None)
        mime_type = getattr(document, "mime_type", "") if document else ""
        has_image = bool(getattr(message, "photo", None)) or bool(mime_type and mime_type.startswith("image/"))

        if has_image and message.sender_id != my_id:
            marker = IMAGE_MARKER_PREFIX + "]"
            try:
                image_bytes = await self.client.download_media(message, file=bytes)
                if image_bytes:
                    description = await self.describe_image(image_bytes)
                    if description:
                        marker = f"{IMAGE_MARKER_PREFIX}: {description}]"
                else:
                    self._debug(f"Keine Bilddaten heruntergeladen (msg_id={message.id}).")
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
        parser = suggestion_callback or extract_final_reply
        last_output: ModelOutput | None = None
        system_prompt = self.build_system_prompt(language_hint)
        user_prompt = self.build_user_prompt(context)

        self._debug(
            f"Generierung gestartet für {context.title} ({context.chat_id}) | language_hint={language_hint!r}"
        )
        self._debug(f"System-Prompt: {truncate_for_log(system_prompt)}")
        self._debug(f"User-Prompt: {truncate_for_log(user_prompt, max_len=3000)}")
        self._debug("Prompt-Zusammenfassung:\n" + self.build_prompt_debug_summary(context))

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            if attempt > 1:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Deine letzte Antwort war nicht sendefertig. "
                            "Gib jetzt nur eine einzige natürliche Telegram-Nachricht aus, "
                            "ohne Analyse, ohne Meta, ohne Denkprozess."
                        ),
                    }
                )

            self._debug(f"Model-Request (Versuch {attempt}): {truncate_for_log(str(messages), max_len=2000)}")

            completion = self.hf_client.chat.completions.create(
                model=self.config.hf_model,
                max_tokens=self.config.hf_max_tokens,
                messages=messages,
            )
            content = completion.choices[0].message.content
            raw = _normalize_text_content(content)
            suggestion = parser(raw).strip()
            analysis = extract_analysis(raw)
            metadata = extract_metadata(raw)

            self._debug(f"Model-Raw-Ausgabe (Versuch {attempt}) für {context.title} ({context.chat_id}): {raw}")
            self._debug(f"Extrahierte Antwort (Versuch {attempt}) für {context.title} ({context.chat_id}): {suggestion}")
            if not has_explicit_reply_marker(raw):
                print(
                    f"[WARN] Kein ANTWORT/REPLY-Marker in Modellausgabe für {context.title} ({context.chat_id}) "
                    f"(Versuch {attempt})."
                )

            current_output = ModelOutput(raw=raw, suggestion=suggestion, analysis=analysis, metadata=metadata)
            last_output = current_output
            if suggestion and not looks_like_reasoning_output(suggestion):
                return current_output

            print(
                f"[WARN] Extrahierte Antwort wirkt wie Denkprozess oder ist leer für {context.title} "
                f"({context.chat_id}) in Versuch {attempt}: {suggestion}"
            )

        assert last_output is not None
        return last_output

    async def maybe_send_suggestion(self, context: ChatContext, suggestion: str) -> bool:
        if not self.config.send_enabled:
            return False
        if self.config.send_confirm != "SEND":
            print("[WARN] SCAMBAITER_SEND aktiv, aber SCAMBAITER_SEND_CONFIRM != 'SEND'.")
            return False
        if looks_like_reasoning_output(suggestion):
            print(
                f"[WARN] Nachricht für {context.title} ({context.chat_id}) nicht gesendet: extrahierter Text wirkt wie Denkprozess."
            )
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



def truncate_for_log(text: str, max_len: int = 1200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "... [gekürzt]"


def strip_think_segments(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.replace("<think>", "").replace("</think>", "").strip()


def strip_wrapping_quotes(text: str) -> str:
    pairs = [('"', '"'), ("'", "'")]
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


def extract_image_description(text: str) -> str | None:
    cleaned = strip_think_segments(text)

    # Prefer explicit markers if the model follows instructions.
    marker_match = re.search(r"(?:^|\n)\s*(?:BESCHREIBUNG|DESCRIPTION)\s*:\s*(.+)", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if marker_match:
        cleaned = marker_match.group(1).strip()

    # If the model emits preamble + draft in one line, keep only the draft tail.
    drafting_match = re.search(r"(?:drafting|entwurf|final(?: answer)?|beschreibung)\s*:\s*(.+)$", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if drafting_match:
        cleaned = drafting_match.group(1).strip()

    raw_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not raw_lines:
        return None

    filtered: list[str] = []
    reject_patterns = (
        r"^(the user wants|let me|looking at the image|now i need|i will|i should|analysis|analyse|reasoning)\b",
        r"^(description should|bildbeschreibung)\b",
        r"^(meta|antwort|reply|hinweis|note)\s*:",
        r"^[-*]\s*",
    )
    for line in raw_lines:
        normalized = line.strip().strip('"').strip("'")
        if not normalized:
            continue

        # Drop inline reasoning lead-ins before a colon and keep trailing candidate text.
        if re.search(r"^(looking at the image|analysis|let me|drafting|entwurf)\s*:", normalized, flags=re.IGNORECASE):
            parts = normalized.split(":", 1)
            normalized = parts[1].strip() if len(parts) > 1 else ""
            if not normalized:
                continue

        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in reject_patterns):
            continue
        filtered.append(normalized)

    if not filtered:
        return None

    description = " ".join(filtered)
    description = re.sub(r"\s+", " ", description).strip()
    description = strip_wrapping_quotes(description)
    if not description or looks_like_reasoning_output(description):
        return None
    return description


def extract_final_reply(text: str) -> str:
    cleaned = strip_think_segments(text)
    match = re.search(r"(?:ANTWORT|ANWORT|REPLY)\s*:\s*(.+)", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if match:
        reply = match.group(1).strip()
        reply = re.split(
            r"\n(?:ANALYSE|HINWEIS|NOTE|META|ANTWORT|ANWORT|REPLY)\s*:",
            reply,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        return strip_wrapping_quotes(reply)

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""

    section_header = re.compile(
        r"^\s*(ANALYSE|HINWEIS|NOTE|GEDANKE|THOUGHT|META|ANTWORT|ANWORT|REPLY)\s*:\s*(.*)$",
        flags=re.IGNORECASE,
    )
    reply_sections = {"antwort", "anwort", "reply"}
    current_section: str | None = None
    reply_lines: list[str] = []
    unsectioned_lines: list[str] = []

    for line in lines:
        header_match = section_header.match(line)
        if header_match:
            current_section = header_match.group(1).strip().lower()
            trailing = header_match.group(2).strip()
            if current_section in reply_sections and trailing:
                reply_lines.append(trailing)
            continue

        if current_section in reply_sections:
            reply_lines.append(line)
        elif current_section is None:
            unsectioned_lines.append(line)

    if reply_lines:
        return strip_wrapping_quotes("\n".join(reply_lines).strip())

    if not unsectioned_lines:
        return ""

    # Drop boilerplate lead-ins if the model omitted explicit markers.
    leadin_pattern = re.compile(
        r"^(hier ist|here is|antwort|reply|finale?\s+antwort|final\s+answer)\b",
        flags=re.IGNORECASE,
    )
    filtered_unsectioned = [line for line in unsectioned_lines if not leadin_pattern.match(line)]
    candidate_lines = filtered_unsectioned or unsectioned_lines
    return strip_wrapping_quotes("\n".join(candidate_lines).strip())




def has_explicit_reply_marker(text: str) -> bool:
    return REPLY_MARKER_PATTERN.search(strip_think_segments(text)) is not None


def looks_like_reasoning_output(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return True
    if "<think>" in stripped or "</think>" in stripped:
        return True
    reasoning_patterns = (
        r"^(analyse|analysis|gedanke|thinking|thought|chain[- ]of[- ]thought|schritt\s*\d+)\b",
        r"^(let me think|i should|zuerst|first|danach|then|abschließend|finally)\b",
    )
    return any(re.search(pattern, stripped, flags=re.IGNORECASE) for pattern in reasoning_patterns)


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
        if re.match(r"^\s*(?:META|ANTWORT|ANWORT|REPLY)\s*:", line, flags=re.IGNORECASE):
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


