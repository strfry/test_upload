from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal


MAX_GENERATION_ATTEMPTS = 2
MAX_REPAIR_SOURCE_CHARS = 12000

from huggingface_hub import InferenceClient
from telethon import TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter
from telethon.utils import get_peer_id

from scambaiter.config import AppConfig
from scambaiter.storage import AnalysisStore


IMAGE_MARKER_PREFIX = "[Bild gesendet"

SYSTEM_PROMPT = """Du bist eine Scambaiting-AI. Deine Rolle: Eine naive, aber freundliche potenzielle Zielperson.
Du bist ein autonomer Generator für Scambaiting-Antworten.

AUSGABEVERTRAG:
- Gib ausschließlich ein einzelnes gültiges JSON-Objekt aus.
- Kein Markdown. Kein Fließtext außerhalb des JSON.
- Keine zusätzlichen Kommentare oder Erklärungen.
- Erlaubte Top-Level-Keys:
  - schema
  - analysis
  - message
  - actions
- schema MUSS exakt "scambait.llm.v1" sein.

PFLICHTFELDER:
- schema (String, exakt "scambait.llm.v1")
- analysis (Objekt, kein String)
- message.text (String, max. 4000 Zeichen)
- actions (Array, max. 10 Elemente)

STRUKTURREGELN:
- message.text enthält die tatsächlich zu sendende Nachricht.
- send_message bedeutet: sende message.text.
- Keine zusätzlichen Keys erzeugen.
- Keine nicht spezifizierten Felder erfinden.
- Wenn Lesen sinnvoll ist, setze mark_read explizit vor Sende-Aktionen.
- Jede Action im actions-Array ist ein Objekt mit Pflichtfeld "type".
- Verboten: Kurzformen wie {"send_message":{}} oder {"simulate_typing":{...}}.

ERLAUBTE ACTION-TYPEN:
- mark_read
- simulate_typing (benötigt duration_seconds)
- delay_send (benötigt delay_seconds)
- send_message (optional reply_to)
- edit_message (benötigt message_id und new_text)
- noop
- escalate_to_human (benötigt reason)

ACTION-GRENZEN:
- duration_seconds: 0–60
- delay_seconds: 0–86400

REFERENZREGELN:
- Nachrichten können über "message_id" referenziert werden.
- reply_to muss auf eine existierende message_id aus der Konversation verweisen.
- Erfinde keine neuen message_id-Werte.

ZEITINFORMATION:
- Nachrichten können ein Feld "ts_utc" (ISO-8601) enthalten.
- Nutze Zeitabstände optional zur Einschätzung von Dringlichkeit oder natürlichem Antwortverhalten.
- Führe keine komplexe Datumsberechnung durch.

QUEUE-KONTEXT:
- Der Input kann bereits geplante Actions aus einer früheren Planung enthalten (`planned_queue`).
- Wenn die alte Planung weiterhin passt, gib die geplanten Actions konsistent erneut aus (Bestätigung).
- Wenn sie nicht mehr passt (z.B. neue eingehende Nachricht), verwerfe/ersetze sie durch eine aktualisierte Actions-Liste.
- Antworte immer mit der vollständigen, aktuell gültigen Actions-Liste.

SPRACHE:
- Verwende ausschließlich die Sprache aus dem Eingabefeld "language".
- Die JSON-Struktur und Feldnamen bleiben unverändert.

SICHERHEITSREGELN:
- Niemals echtes Geld senden oder zusagen.
- Keine echten persönlichen Daten preisgeben.
- Keine illegalen Anleitungen geben.
- Keine Schadsoftware oder Credential-Erfassung unterstützen.

FALLBACK:
- Wenn keine sichere oder regelkonforme Antwort möglich ist:
  Verwende escalate_to_human.

Gib ausschließlich gültiges JSON zurück."""


@dataclass
class ChatContext:
    chat_id: int
    title: str
    messages: list["ChatMessage"]


@dataclass
class ChatMessage:
    timestamp: datetime
    sender: str
    role: Literal["assistant", "user"]
    text: str


@dataclass
class SuggestionResult:
    context: ChatContext
    suggestion: str
    analysis: dict[str, object] | None = None
    metadata: dict[str, str] | None = None
    actions: list[dict[str, object]] | None = None


@dataclass
class ModelOutput:
    raw: str
    suggestion: str
    analysis: dict[str, object] | None
    metadata: dict[str, str]
    actions: list[dict[str, object]]


def parse_structured_model_output(text: str) -> ModelOutput | None:
    cleaned = strip_think_segments(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    allowed_top_level_keys = {"schema", "analysis", "message", "actions"}
    if any(str(key) not in allowed_top_level_keys for key in data.keys()):
        return None

    schema_value = data.get("schema")
    if not isinstance(schema_value, str) or schema_value.strip() != "scambait.llm.v1":
        return None

    message_value = data.get("message")
    if not isinstance(message_value, dict):
        return None
    if any(str(key) != "text" for key in message_value.keys()):
        return None
    text_value = message_value.get("text")
    if not isinstance(text_value, str):
        return None
    reply = text_value.strip()
    if not reply or len(reply) > 4000:
        return None

    actions_value = data.get("actions")
    if not isinstance(actions_value, list) or not actions_value or len(actions_value) > 10:
        return None

    normalized_actions: list[dict[str, object]] = []
    for action in actions_value:
        normalized_action = normalize_action_shape(action)
        if not isinstance(normalized_action, dict):
            return None
        if (
            isinstance(action, dict)
            and "type" not in action
            and normalized_action.get("type") is not None
        ):
            print(
                "[WARN] Parser normalisiert Action-Kurzform: "
                + truncate_for_log(json.dumps(action, ensure_ascii=False), max_len=500)
            )
        action_type = normalized_action.get("type")
        if not isinstance(action_type, str):
            return None

        if action_type == "mark_read":
            if set(normalized_action.keys()) != {"type"}:
                return None
            normalized_actions.append({"type": "mark_read"})
        elif action_type == "simulate_typing":
            expected = {"type", "duration_seconds"}
            if set(normalized_action.keys()) != expected:
                return None
            duration = normalized_action.get("duration_seconds")
            if not isinstance(duration, (int, float)) or duration < 0 or duration > 60:
                return None
            normalized_actions.append({"type": "simulate_typing", "duration_seconds": float(duration)})
        elif action_type == "delay_send":
            expected = {"type", "delay_seconds"}
            if set(normalized_action.keys()) != expected:
                return None
            delay = normalized_action.get("delay_seconds")
            if not isinstance(delay, (int, float)) or delay < 0 or delay > 86400:
                return None
            normalized_actions.append({"type": "delay_send", "delay_seconds": float(delay)})
        elif action_type == "send_message":
            expected = {"type", "reply_to"}
            keys = set(normalized_action.keys())
            if keys != {"type"} and keys != expected:
                return None
            entry: dict[str, object] = {"type": "send_message"}
            if "reply_to" in normalized_action:
                reply_to = normalized_action.get("reply_to")
                if not isinstance(reply_to, (str, int)):
                    return None
                entry["reply_to"] = reply_to
            normalized_actions.append(entry)
        elif action_type == "edit_message":
            expected = {"type", "message_id", "new_text"}
            if set(normalized_action.keys()) != expected:
                return None
            if not isinstance(normalized_action.get("new_text"), str):
                return None
            message_id = normalized_action.get("message_id")
            if not isinstance(message_id, (str, int)):
                return None
            normalized_actions.append(
                {
                    "type": "edit_message",
                    "message_id": message_id,
                    "new_text": normalized_action.get("new_text", ""),
                }
            )
        elif action_type == "noop":
            if set(normalized_action.keys()) != {"type"}:
                return None
            normalized_actions.append({"type": "noop"})
        elif action_type == "escalate_to_human":
            expected = {"type", "reason"}
            if set(normalized_action.keys()) != expected:
                return None
            if not isinstance(normalized_action.get("reason"), str) or not normalized_action.get("reason").strip():
                return None
            normalized_actions.append({"type": "escalate_to_human", "reason": normalized_action.get("reason", "").strip()})
        else:
            return None

    analysis_value = data.get("analysis")
    if not isinstance(analysis_value, dict):
        return None
    analysis: dict[str, object] = analysis_value

    metadata: dict[str, str] = {}
    for top_level_key in ("schema",):
        top_level_value = data.get(top_level_key)
        if isinstance(top_level_value, str) and top_level_value.strip():
            metadata[top_level_key] = top_level_value.strip()

    return ModelOutput(
        raw=text,
        suggestion=reply,
        analysis=analysis or None,
        metadata=metadata,
        actions=normalized_actions,
    )


def normalize_action_shape(action: object) -> dict[str, object] | None:
    if not isinstance(action, dict):
        return None
    if "type" in action:
        return dict(action)

    # Accept and normalize legacy malformed shorthand:
    # {"send_message": {}} -> {"type": "send_message"}
    if len(action) == 1:
        key = next(iter(action.keys()))
        value = action[key]
        if isinstance(key, str) and key in {
            "mark_read",
            "simulate_typing",
            "delay_send",
            "send_message",
            "edit_message",
            "noop",
            "escalate_to_human",
        }:
            normalized: dict[str, object] = {"type": key}
            if isinstance(value, dict):
                for k, v in value.items():
                    normalized[str(k)] = v
            return normalized
    return dict(action)


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

    async def collect_folder_chats(self, folder_chat_ids: set[int]) -> list[ChatContext]:
        me = await self.client.get_me()
        my_id = me.id
        contexts: list[ChatContext] = []

        async for dialog in self.client.iter_dialogs():
            if dialog.id not in folder_chat_ids or dialog.message is None:
                continue

            messages = await self.client.get_messages(dialog.entity, limit=self.config.history_limit)
            chat_messages: list[ChatMessage] = []
            for message in reversed(messages):
                chat_message = await self._format_message_line(message, my_id=my_id, dialog_title=dialog.title)
                if chat_message:
                    chat_messages.append(chat_message)

            if chat_messages:
                contexts.append(ChatContext(chat_id=dialog.id, title=dialog.title, messages=chat_messages))

        self._debug(f"Chats im Ordner gefunden: {len(contexts)}")
        return contexts

    async def build_chat_context(self, chat_id: int) -> ChatContext | None:
        me = await self.client.get_me()
        my_id = me.id
        entity = await self.client.get_entity(chat_id)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
        messages = await self.client.get_messages(entity, limit=self.config.history_limit)

        chat_messages: list[ChatMessage] = []
        for message in reversed(messages):
            chat_message = await self._format_message_line(message, my_id=my_id, dialog_title=title)
            if chat_message:
                chat_messages.append(chat_message)

        if not chat_messages:
            return None

        return ChatContext(chat_id=chat_id, title=title, messages=chat_messages)

    async def get_chat_profile_photo(self, chat_id: int) -> bytes | None:
        try:
            entity = await self.client.get_entity(chat_id)
        except Exception as exc:
            self._debug(f"Profilbild: Entity fuer {chat_id} konnte nicht geladen werden: {exc}")
            return None

        try:
            photo_bytes = await self.client.download_profile_photo(entity, file=bytes)
        except Exception as exc:
            self._debug(f"Profilbild: Download fuer {chat_id} fehlgeschlagen: {exc}")
            return None

        if isinstance(photo_bytes, (bytes, bytearray)) and photo_bytes:
            return bytes(photo_bytes)
        return None

    def build_conversation_messages(
        self,
        context: ChatContext,
        prompt_context: dict[str, object] | None = None,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = [
            {
                "role": "user",
                "content": (
                    f"Konversation mit {context.title} (Telegram Chat-ID: {context.chat_id}). "
                    "Die folgenden Nachrichten sind chronologisch sortiert."
                ),
            }
        ]

        if prompt_context:
            context_json = json.dumps(prompt_context, ensure_ascii=False, indent=2)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Strukturierter System-Kontext (nur intern, nicht wortwörtlich zitieren):\n"
                        f"{context_json}"
                    ),
                }
            )

        for item in context.messages:
            timestamp = self._fmt_dt(item.timestamp)
            speaker = "Ich" if item.role == "assistant" else item.sender
            content = f"[{timestamp}] {speaker}: {item.text}"
            messages.append({"role": item.role, "content": content})

        return messages

    def build_prompt_debug_summary(self, context: ChatContext, max_lines: int = 5) -> str:
        rendered_messages = [self.render_chat_message(item) for item in context.messages]
        image_lines = [line for line in rendered_messages if IMAGE_MARKER_PREFIX in line]
        head_lines = rendered_messages[:max_lines]
        tail_lines = rendered_messages[-max_lines:] if len(rendered_messages) > max_lines else []

        parts = [
            f"Chat: {context.title} ({context.chat_id})",
            f"Zeilen gesamt: {len(rendered_messages)}",
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

    @staticmethod
    def render_chat_message(message: ChatMessage) -> str:
        ts = ScambaiterCore._fmt_dt(message.timestamp)
        return f"[{ts}] {message.sender}: {message.text}"

    async def _collect_recent_chat_images(
        self,
        chat_id: int,
        limit: int = 3,
    ) -> list[tuple[int, str, bytes, str]]:
        messages = await self.client.get_messages(chat_id, limit=max(20, limit * 10))
        images: list[tuple[int, str, bytes, str]] = []

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
            if not description:
                continue

            marker = "photo" if is_photo else f"document:{mime_type}"
            images.append((int(message.id), marker, image_bytes, description))
            if len(images) >= limit:
                break

        return images

    async def describe_recent_images_for_chat(self, chat_id: int, limit: int = 3) -> list[str]:
        images = await self._collect_recent_chat_images(chat_id, limit=limit)
        return [
            f"msg_id={msg_id} ({marker}): {truncate_for_log(description, max_len=300)}"
            for msg_id, marker, _image_bytes, description in images
        ]

    async def get_recent_images_with_captions_for_control_channel(
        self,
        chat_id: int,
        limit: int = 3,
    ) -> list[tuple[bytes, str]]:
        images = await self._collect_recent_chat_images(chat_id, limit=limit)
        return [
            (
                image_bytes,
                f"Chat {chat_id} | msg_id={msg_id} ({marker})\nCaption: {description}",
            )
            for msg_id, marker, image_bytes, description in images
        ]

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

    async def _format_message_line(self, message, my_id: int, dialog_title: str) -> ChatMessage | None:
        text = (message.message or "").strip()
        is_own_message = message.sender_id == my_id
        sender = "Ich" if is_own_message else dialog_title
        role: Literal["assistant", "user"] = "assistant" if is_own_message else "user"

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
                return ChatMessage(timestamp=message.date, sender=sender, role=role, text=f"{marker} {text}")
            return ChatMessage(timestamp=message.date, sender=sender, role=role, text=marker)

        if text:
            return ChatMessage(timestamp=message.date, sender=sender, role=role, text=text)

        return None

    def generate_suggestion(self, context: ChatContext) -> str:
        return self.generate_output(context).suggestion

    @staticmethod
    def build_language_system_prompt(language_hint: str | None = None) -> str | None:
        if not language_hint:
            return None

        lang = language_hint.strip().lower()
        if lang in {"en", "english", "englisch"}:
            return "You must respond exclusively in English."
        if lang in {"de", "deutsch", "german"}:
            return "Du antwortest immer auf Deutsch."
        return None

    def generate_output(
        self,
        context: ChatContext,
        language_hint: str | None = None,
        prompt_context: dict[str, object] | None = None,
        on_warning: Callable[[str], None] | None = None,
    ) -> ModelOutput:
        last_output: ModelOutput | None = None
        language_system_prompt = self.build_language_system_prompt(language_hint)
        conversation_messages = self.build_conversation_messages(context, prompt_context=prompt_context)

        self._debug(
            f"Generierung gestartet für {context.title} ({context.chat_id}) | language_hint={language_hint!r}"
        )
        self._debug(f"System-Prompt (Basis): {truncate_for_log(SYSTEM_PROMPT)}")
        if language_system_prompt:
            self._debug(f"System-Prompt (Sprache): {truncate_for_log(language_system_prompt)}")
        self._debug(f"Conversation-Messages: {truncate_for_log(str(conversation_messages), max_len=3000)}")
        self._debug("Prompt-Zusammenfassung:\n" + self.build_prompt_debug_summary(context))

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
            ]
            if language_system_prompt:
                messages.append({"role": "system", "content": language_system_prompt})
            messages.extend(conversation_messages)
            if attempt > 1:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Deine letzte Antwort war nicht sendefertig. "
                            "Gib jetzt ausschließlich ein valides JSON-Objekt zurück "
                            "mit schema=\"scambait.llm.v1\", analysis als OBJEKT, "
                            "message.text als nicht-leerem String und actions als nicht-leerem Array. "
                            "Keine Zusatztexte."
                        ),
                    }
                )

            self._debug(f"Model-Request (Versuch {attempt}): {truncate_for_log(str(messages), max_len=2000)}")

            try:
                completion = self.hf_client.chat.completions.create(
                    model=self.config.hf_model,
                    max_tokens=self.config.hf_max_tokens,
                    messages=messages,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                error_detail = format_model_exception_details(exc)
                print(
                    f"[ERROR] Model-Request fehlgeschlagen für {context.title} ({context.chat_id}) "
                    f"in Versuch {attempt}: {error_detail}"
                )
                failed_generation = extract_failed_generation_from_exception(exc)
                if failed_generation:
                    repaired = self._attempt_repair_output(
                        source_text=failed_generation,
                        language_system_prompt=language_system_prompt,
                    )
                    if repaired is not None:
                        if on_warning:
                            on_warning(
                                f"Repair-Pfad genutzt für {context.title} ({context.chat_id}) nach Request-Fehler."
                            )
                        return repaired
                if attempt < MAX_GENERATION_ATTEMPTS:
                    continue
                raise RuntimeError(
                    "Model request failed after retries. "
                    "Details im Log unter [ERROR] Model-Request fehlgeschlagen."
                ) from exc
            choice = completion.choices[0]
            finish_reason = (getattr(choice, "finish_reason", None) or "").strip().lower()
            usage = getattr(completion, "usage", None)
            if usage is not None:
                self._debug(f"Model-Usage (Versuch {attempt}): {usage}")
            if finish_reason:
                self._debug(f"Model-Finish-Reason (Versuch {attempt}): {finish_reason}")
            if finish_reason == "length":
                warning_message = (
                    f"Token-Limit erreicht: Ausgabe für {context.title} ({context.chat_id}) "
                    f"wurde abgeschnitten (HF_MAX_TOKENS={self.config.hf_max_tokens})."
                )
                print(f"[WARN] {warning_message}")
                if on_warning:
                    on_warning(warning_message)

            content = choice.message.content
            raw = _normalize_text_content(content)
            structured = parse_structured_model_output(raw)
            if structured is None:
                print(
                    f"[WARN] Keine valide JSON-Ausgabe für {context.title} ({context.chat_id}) in Versuch {attempt}."
                )
                repaired = self._attempt_repair_output(
                    source_text=raw,
                    language_system_prompt=language_system_prompt,
                )
                if repaired is not None:
                    if on_warning:
                        on_warning(
                            f"Repair-Pfad genutzt für {context.title} ({context.chat_id}) nach invalidem JSON."
                        )
                    return repaired
                last_output = ModelOutput(raw=raw, suggestion="", analysis=None, metadata={}, actions=[])
                continue

            suggestion = structured.suggestion.strip()
            analysis = structured.analysis
            metadata = structured.metadata

            self._debug(f"Model-Raw-Ausgabe (Versuch {attempt}) für {context.title} ({context.chat_id}): {raw}")
            self._debug(f"Extrahierte Antwort (Versuch {attempt}) für {context.title} ({context.chat_id}): {suggestion}")

            current_output = ModelOutput(
                raw=raw,
                suggestion=suggestion,
                analysis=analysis,
                metadata=metadata,
                actions=structured.actions,
            )
            last_output = current_output
            if suggestion and not looks_like_reasoning_output(suggestion):
                return current_output

            print(
                f"[WARN] Extrahierte Antwort wirkt wie Denkprozess oder ist leer für {context.title} "
                f"({context.chat_id}) in Versuch {attempt}: {suggestion}"
            )

        if last_output is None:
            return ModelOutput(raw="", suggestion="", analysis=None, metadata={}, actions=[])
        return last_output

    def _attempt_repair_output(
        self,
        source_text: str,
        language_system_prompt: str | None = None,
    ) -> ModelOutput | None:
        candidate = (source_text or "").strip()
        if not candidate:
            return None
        if len(candidate) > MAX_REPAIR_SOURCE_CHARS:
            candidate = candidate[:MAX_REPAIR_SOURCE_CHARS]

        repair_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "Du bist ein JSON-Reparaturmodul. "
                    "Gib exakt ein valides JSON-Objekt im geforderten Schema zurück. "
                    "Keine Erklärungen, kein Markdown."
                ),
            }
        ]
        if language_system_prompt:
            repair_messages.append({"role": "system", "content": language_system_prompt})
        repair_messages.append(
            {
                "role": "user",
                "content": (
                    "Repariere die folgende fehlerhafte Modellausgabe in ein valides JSON-Objekt "
                    "mit den Top-Level-Feldern schema, analysis, message und actions. "
                    "schema MUSS exakt \"scambait.llm.v1\" sein. "
                    "analysis MUSS ein JSON-Objekt sein (kein String). "
                    "message.text darf nicht leer sein. actions muss mindestens ein Element enthalten "
                    "(falls nötig: noop). "
                    "Jede Action muss ein Objekt mit Pflichtfeld \"type\" sein. "
                    "Keine Kurzformen wie {\"send_message\":{}}.\n\n"
                    f"Fehlerhafte Ausgabe:\n{candidate}"
                ),
            }
        )

        try:
            repair_completion = self.hf_client.chat.completions.create(
                model=self.config.hf_model,
                max_tokens=self.config.hf_max_tokens,
                messages=repair_messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            print(f"[WARN] Repair-Request fehlgeschlagen: {format_model_exception_details(exc)}")
            return None

        repair_raw = _normalize_text_content(repair_completion.choices[0].message.content)
        repaired = parse_structured_model_output(repair_raw)
        if repaired is None:
            print(f"[WARN] Repair-Request lieferte weiterhin invalides JSON: {truncate_for_log(repair_raw, max_len=1200)}")
            return None
        if not repaired.suggestion or looks_like_reasoning_output(repaired.suggestion):
            return None
        return repaired

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


def _find_key_deep(obj: object, key: str) -> object | None:
    if isinstance(obj, dict):
        if key in obj:
            return obj.get(key)
        for value in obj.values():
            found = _find_key_deep(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key_deep(item, key)
            if found is not None:
                return found
    return None


def extract_failed_generation_from_exception(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    failed_generation = _find_key_deep(payload, "failed_generation")
    if isinstance(failed_generation, str):
        cleaned = failed_generation.strip()
        return cleaned or None
    return None


def format_model_exception_details(exc: Exception) -> str:
    parts = [f"{type(exc).__name__}: {exc}"]

    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            parts.append(f"status_code={status}")
        try:
            payload = response.json()
            failed_generation = _find_key_deep(payload, "failed_generation")
            if failed_generation:
                parts.append(f"failed_generation={truncate_for_log(str(failed_generation), max_len=2500)}")
            parts.append(f"response_json={truncate_for_log(json.dumps(payload, ensure_ascii=False), max_len=2500)}")
        except Exception:
            text = getattr(response, "text", None)
            if text:
                parts.append(f"response_text={truncate_for_log(str(text), max_len=2500)}")

    stack = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    if stack:
        parts.append(f"exception_only={truncate_for_log(stack, max_len=800)}")
    return " | ".join(parts)
