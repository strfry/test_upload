from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .model_client import call_hf_openai_chat, extract_result_text

EventType = str
RoleType = str


@dataclass(slots=True)
class ChatEvent:
    event_type: EventType
    role: RoleType
    text: str | None = None
    ts_utc: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatContext:
    chat_id: int
    title: str
    messages: list[ChatEvent | dict[str, Any]]


@dataclass(slots=True)
class ModelOutput:
    raw: str
    suggestion: str
    analysis: dict[str, Any]
    metadata: dict[str, Any]
    actions: list[dict[str, Any]]


class ScambaiterCore:
    """Core analysis/prompt component.

    This class intentionally does not send any messages. It only prepares
    context and generates structured model outputs.
    """

    def __init__(self, config: Any, store: Any) -> None:
        self.config = config
        self.store = store

    async def start(self) -> None:  # pragma: no cover - lifecycle hook
        return

    async def close(self) -> None:  # pragma: no cover - lifecycle hook
        return

    async def build_chat_context(self, chat_id: int) -> ChatContext | None:
        events = self.store.list_events(chat_id=chat_id, limit=500)
        if not events:
            return None
        messages: list[dict[str, Any]] = []
        for event in events:
            messages.append(
                {
                    "event_type": event.event_type,
                    "role": event.role,
                    "text": event.text,
                    "ts_utc": event.ts_utc,
                    "meta": event.meta,
                }
            )
        return ChatContext(chat_id=chat_id, title=f"chat-{chat_id}", messages=messages)

    def get_recent_typing_hint(self, chat_id: int, max_age_seconds: int = 120) -> dict[str, Any] | None:
        _ = (chat_id, max_age_seconds)
        return None

    def build_prompt_events(self, chat_id: int, token_limit: int | None = None) -> list[dict[str, Any]]:
        token_budget = token_limit if token_limit is not None else int(getattr(self.config, "hf_max_tokens", 1500))
        events = self.store.list_events(chat_id=chat_id, limit=5000)
        prompt_events: list[dict[str, Any]] = []
        for event in events:
            prompt_events.append(
                {
                    "event_type": event.event_type,
                    "role": event.role,
                    "text": event.text,
                    "time": self._as_hhmm(event.ts_utc),
                    "meta": event.meta,
                }
            )
        profile_updates = self.store.list_profile_system_messages(chat_id=chat_id, limit=20)
        for item in profile_updates:
            prompt_events.append(
                {
                    "event_type": item.get("event_type", "message"),
                    "role": item.get("role", "system"),
                    "text": item.get("text"),
                    "time": self._as_hhmm(item.get("ts_utc")),
                    "meta": item.get("meta", {}),
                }
            )
        return self._trim_prompt_events(prompt_events, token_budget)

    def build_model_messages(self, chat_id: int) -> list[dict[str, str]]:
        prompt_events = self.build_prompt_events(chat_id=chat_id)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are ScamBaiter assistant. Produce valid JSON with fields: "
                    "analysis, message, actions."
                ),
            },
            {
                "role": "system",
                "content": f"Chat context for chat_id={chat_id}. Events are chronological.",
            },
        ]
        for event in prompt_events:
            payload = {
                "time": event.get("time"),
                "role": event.get("role"),
                "event_type": event.get("event_type"),
                "text": event.get("text"),
            }
            messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=True)})
        return messages

    def run_hf_dry_run(self, chat_id: int) -> dict[str, Any]:
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_model", None) or "").strip()
        if not token or not model:
            raise RuntimeError("HF_TOKEN/HF_MODEL missing")
        max_tokens = int(getattr(self.config, "hf_max_tokens", 1500))
        messages = self.build_model_messages(chat_id=chat_id)
        response_json = call_hf_openai_chat(
            token=token,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            # Dry run is pinned to HF router to avoid accidental provider drift via HF_BASE_URL.
            base_url=None,
        )
        return {
            "provider": "huggingface_openai_compat",
            "model": model,
            "prompt_json": {"messages": messages, "max_tokens": max_tokens},
            "response_json": response_json,
            "result_text": extract_result_text(response_json),
        }

    def generate_output(
        self,
        context: ChatContext,
        language_hint: str | None = None,
        prompt_context: dict[str, Any] | None = None,
    ) -> ModelOutput:
        _ = (language_hint, prompt_context)
        last_text = ""
        for message in reversed(context.messages):
            if isinstance(message, dict):
                candidate = message.get("text")
                if isinstance(candidate, str) and candidate.strip():
                    last_text = candidate.strip()
                    break
            elif isinstance(message, ChatEvent) and isinstance(message.text, str) and message.text.strip():
                last_text = message.text.strip()
                break
        suggestion = "Noted."
        if last_text:
            suggestion = f"Noted: {last_text[:120]}"
        return ModelOutput(
            raw='{"schema":"scambait.llm.v1"}',
            suggestion=suggestion,
            analysis={},
            metadata={"schema": "scambait.llm.v1"},
            actions=[{"type": "prepare_message"}],
        )

    @staticmethod
    def _as_hhmm(ts_utc: str | None) -> str | None:
        if not ts_utc:
            return None
        try:
            cleaned = ts_utc.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(cleaned)
            return parsed.astimezone(timezone.utc).strftime("%H:%M")
        except ValueError:
            return ts_utc[-8:-3] if len(ts_utc) >= 5 else ts_utc

    @classmethod
    def _trim_prompt_events(cls, events: list[dict[str, Any]], token_limit: int) -> list[dict[str, Any]]:
        if token_limit <= 0:
            return []
        kept_rev: list[dict[str, Any]] = []
        running = 0
        # Keep newest events and drop from conversation start when limit is hit.
        for event in reversed(events):
            estimated = cls._estimate_tokens(event)
            if kept_rev and running + estimated > token_limit:
                break
            if not kept_rev and estimated > token_limit:
                # Ensure we keep at least one newest event.
                kept_rev.append(event)
                break
            kept_rev.append(event)
            running += estimated
        kept_rev.reverse()
        return kept_rev

    @staticmethod
    def _estimate_tokens(event: dict[str, Any]) -> int:
        text = str(event.get("text") or "")
        meta = str(event.get("meta") or "")
        base = len(text) + len(meta) + 24
        return max(1, base // 4)
