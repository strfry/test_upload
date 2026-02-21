from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .model_client import call_hf_openai_chat, extract_result_text

EventType = str
RoleType = str

SYSTEM_PROMPT_CONTRACT = """You are ScamBaiter assistant.
Return exactly one valid JSON object with top-level keys: schema, analysis, message, actions.
Rules:
- schema must be exactly \"scambait.llm.v1\".
- analysis must be an object.
- message must be an object with exactly one key: text (string, <= 4000 chars).
- actions must be a non-empty array (max 10 actions).
- If actions contains send_message, message.text must be non-empty.
- No markdown, no prose outside JSON, no function-call wrappers.
Allowed action types:
- mark_read
- simulate_typing (duration_seconds 0..60)
- wait (value >=0, unit in seconds|minutes; max 86400 seconds / 10080 minutes)
- send_message (optional reply_to, optional send_at_utc)
- edit_message (message_id, new_text)
- noop
- escalate_to_human (reason)
"""

ALLOWED_TOP_LEVEL_KEYS = {"schema", "analysis", "message", "actions"}
ALLOWED_ACTION_TYPES = {
    "mark_read",
    "simulate_typing",
    "wait",
    "send_message",
    "edit_message",
    "noop",
    "escalate_to_human",
}


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


def strip_think_segments(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.replace("<think>", "").replace("</think>", "").strip()


def normalize_iso_utc(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return text


def normalize_action_shape(action: object) -> dict[str, Any] | None:
    if not isinstance(action, dict):
        return None
    if "type" in action:
        return dict(action)

    # Accept alias used by some models: {"action":"send_message", ...}
    if "action" in action and isinstance(action.get("action"), str):
        action_type = str(action.get("action") or "").strip()
        if action_type in ALLOWED_ACTION_TYPES:
            normalized = dict(action)
            normalized.pop("action", None)
            normalized["type"] = action_type
            return normalized

    # Accept malformed shorthand for compatibility:
    # {"send_message": {}} -> {"type": "send_message"}
    if len(action) == 1:
        key = next(iter(action.keys()))
        value = action[key]
        if isinstance(key, str) and key in ALLOWED_ACTION_TYPES:
            normalized: dict[str, Any] = {"type": key}
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    normalized[str(nested_key)] = nested_value
            return normalized
    return dict(action)


def _validate_actions(actions_value: object, reply: str) -> list[dict[str, Any]] | None:
    if isinstance(actions_value, dict):
        actions_value = [actions_value]
    if not isinstance(actions_value, list):
        return None
    if len(actions_value) > 10:
        return None
    if not actions_value:
        return [{"type": "send_message"}] if reply else [{"type": "noop"}]

    normalized_actions: list[dict[str, Any]] = []
    for action in actions_value:
        normalized_action = normalize_action_shape(action)
        if not isinstance(normalized_action, dict):
            return None

        action_type = normalized_action.get("type")
        if not isinstance(action_type, str) or action_type not in ALLOWED_ACTION_TYPES:
            return None

        if action_type == "mark_read":
            if set(normalized_action.keys()) != {"type"}:
                return None
            normalized_actions.append({"type": "mark_read"})
            continue

        if action_type == "simulate_typing":
            if set(normalized_action.keys()) != {"type", "duration_seconds"}:
                return None
            duration = normalized_action.get("duration_seconds")
            if not isinstance(duration, (int, float)) or duration < 0 or duration > 60:
                return None
            normalized_actions.append({"type": "simulate_typing", "duration_seconds": float(duration)})
            continue

        if action_type == "wait":
            if set(normalized_action.keys()) != {"type", "value", "unit"}:
                return None
            value = normalized_action.get("value")
            unit = normalized_action.get("unit")
            if not isinstance(value, (int, float)) or not isinstance(unit, str):
                return None
            unit_norm = unit.strip().lower()
            if unit_norm not in {"seconds", "minutes"}:
                return None
            numeric_value = float(value)
            if numeric_value < 0:
                return None
            if unit_norm == "seconds" and numeric_value > 86400:
                return None
            if unit_norm == "minutes" and numeric_value > 10080:
                return None
            normalized_actions.append({"type": "wait", "value": numeric_value, "unit": unit_norm})
            continue

        if action_type == "send_message":
            allowed = {"type", "reply_to", "send_at_utc"}
            keys = set(normalized_action.keys())
            if not keys.issubset(allowed) or "type" not in keys:
                return None
            entry: dict[str, Any] = {"type": "send_message"}
            if "reply_to" in normalized_action:
                reply_to = normalized_action.get("reply_to")
                if not isinstance(reply_to, (str, int)):
                    return None
                entry["reply_to"] = reply_to
            if "send_at_utc" in normalized_action:
                send_at_utc = normalized_action.get("send_at_utc")
                if not isinstance(send_at_utc, str):
                    return None
                normalized_ts = normalize_iso_utc(send_at_utc)
                if not normalized_ts:
                    return None
                entry["send_at_utc"] = normalized_ts
            normalized_actions.append(entry)
            continue

        if action_type == "edit_message":
            if set(normalized_action.keys()) != {"type", "message_id", "new_text"}:
                return None
            message_id = normalized_action.get("message_id")
            new_text = normalized_action.get("new_text")
            if not isinstance(message_id, (str, int)) or not isinstance(new_text, str):
                return None
            normalized_actions.append({"type": "edit_message", "message_id": message_id, "new_text": new_text})
            continue

        if action_type == "noop":
            if set(normalized_action.keys()) != {"type"}:
                return None
            normalized_actions.append({"type": "noop"})
            continue

        if action_type == "escalate_to_human":
            if set(normalized_action.keys()) != {"type", "reason"}:
                return None
            reason = normalized_action.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                return None
            normalized_actions.append({"type": "escalate_to_human", "reason": reason.strip()})
            continue

        return None

    has_send_message = any(str(action.get("type")) == "send_message" for action in normalized_actions)
    if has_send_message and not reply:
        return None
    if reply and not has_send_message:
        normalized_actions.append({"type": "send_message"})

    return normalized_actions


def _build_repair_messages(failed_generation: str) -> list[dict[str, str]]:
    clipped = failed_generation.strip()
    if len(clipped) > 12000:
        clipped = clipped[:12000]
    return [
        {"role": "system", "content": SYSTEM_PROMPT_CONTRACT},
        {
            "role": "system",
            "content": (
                "Repair task: previous output violated the JSON contract. "
                "Return only a corrected scambait.llm.v1 JSON object."
            ),
        },
        {"role": "user", "content": json.dumps({"failed_generation": clipped}, ensure_ascii=True)},
    ]


def parse_structured_model_output(text: str) -> ModelOutput | None:
    cleaned = strip_think_segments(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    required_keys = {"schema", "analysis", "message", "actions"}
    if not required_keys.issubset(set(str(key) for key in data.keys())):
        return None

    schema_value = data.get("schema")
    if not isinstance(schema_value, str) or schema_value.strip() != "scambait.llm.v1":
        return None

    analysis_value = data.get("analysis")
    if not isinstance(analysis_value, dict):
        return None

    message_value = data.get("message")
    if not isinstance(message_value, dict) or "text" not in message_value:
        return None
    text_value = message_value.get("text")
    if not isinstance(text_value, str):
        return None
    reply = text_value.strip()
    if len(reply) > 4000:
        return None

    normalized_actions = _validate_actions(data.get("actions"), reply)
    if normalized_actions is None:
        return None

    return ModelOutput(
        raw=text,
        suggestion=reply,
        analysis=analysis_value,
        metadata={"schema": "scambait.llm.v1"},
        actions=normalized_actions,
    )


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
            {"role": "system", "content": SYSTEM_PROMPT_CONTRACT},
            {
                "role": "system",
                "content": (
                    "Chat context for chat_id="
                    f"{chat_id}. Events are chronological. "
                    "Prefer the newest user/scammer intent."
                ),
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

        attempts: list[dict[str, Any]] = []
        initial_messages = self.build_model_messages(chat_id=chat_id)
        initial_prompt = {"messages": initial_messages, "max_tokens": max_tokens}

        try:
            initial_response = call_hf_openai_chat(
                token=token,
                model=model,
                messages=initial_messages,
                max_tokens=max_tokens,
                # Dry run is pinned to HF router to avoid accidental provider drift via HF_BASE_URL.
                base_url=None,
            )
            initial_text = extract_result_text(initial_response)
        except Exception as exc:
            attempts.append(
                {
                    "phase": "initial",
                    "status": "error",
                    "accepted": False,
                    "reject_reason": "provider_error",
                    "error_message": str(exc),
                    "prompt_json": initial_prompt,
                    "response_json": {},
                    "result_text": "",
                }
            )
            return {
                "provider": "huggingface_openai_compat",
                "model": model,
                "prompt_json": initial_prompt,
                "response_json": {},
                "result_text": "",
                "valid_output": False,
                "parsed_output": None,
                "error_message": str(exc),
                "attempts": attempts,
            }

        parsed = parse_structured_model_output(initial_text)
        attempts.append(
            {
                "phase": "initial",
                "status": "ok" if parsed is not None else "invalid",
                "accepted": parsed is not None,
                "reject_reason": None if parsed is not None else "contract_validation_failed",
                "error_message": None,
                "prompt_json": initial_prompt,
                "response_json": initial_response,
                "result_text": initial_text,
            }
        )

        final_prompt = initial_prompt
        final_response = initial_response
        final_text = initial_text

        if parsed is None:
            repair_messages = _build_repair_messages(initial_text)
            repair_prompt = {"messages": repair_messages, "max_tokens": max_tokens}
            try:
                repair_response = call_hf_openai_chat(
                    token=token,
                    model=model,
                    messages=repair_messages,
                    max_tokens=max_tokens,
                    base_url=None,
                )
                repair_text = extract_result_text(repair_response)
                repaired = parse_structured_model_output(repair_text)
                attempts.append(
                    {
                        "phase": "repair",
                        "status": "ok" if repaired is not None else "invalid",
                        "accepted": repaired is not None,
                        "reject_reason": None if repaired is not None else "contract_validation_failed",
                        "error_message": None,
                        "prompt_json": repair_prompt,
                        "response_json": repair_response,
                        "result_text": repair_text,
                    }
                )
                if repaired is not None:
                    parsed = repaired
                    final_prompt = repair_prompt
                    final_response = repair_response
                    final_text = repair_text
                else:
                    final_prompt = repair_prompt
                    final_response = repair_response
                    final_text = repair_text
            except Exception as exc:
                attempts.append(
                    {
                        "phase": "repair",
                        "status": "error",
                        "accepted": False,
                        "reject_reason": "provider_error",
                        "error_message": str(exc),
                        "prompt_json": repair_prompt,
                        "response_json": {},
                        "result_text": "",
                    }
                )
                return {
                    "provider": "huggingface_openai_compat",
                    "model": model,
                    "prompt_json": repair_prompt,
                    "response_json": {},
                    "result_text": "",
                    "valid_output": False,
                    "parsed_output": None,
                    "error_message": str(exc),
                    "attempts": attempts,
                }

        return {
            "provider": "huggingface_openai_compat",
            "model": model,
            "prompt_json": final_prompt,
            "response_json": final_response,
            "result_text": final_text,
            "valid_output": parsed is not None,
            "parsed_output": {
                "analysis": parsed.analysis,
                "message": {"text": parsed.suggestion},
                "actions": parsed.actions,
                "metadata": parsed.metadata,
            }
            if parsed is not None
            else None,
            "error_message": None if parsed is not None else "invalid model output contract (expected scambait.llm.v1 with analysis/message/actions)",
            "attempts": attempts,
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

        candidate_payload = {
            "schema": "scambait.llm.v1",
            "analysis": {},
            "message": {"text": suggestion},
            "actions": [{"type": "send_message"}],
        }
        raw = json.dumps(candidate_payload, ensure_ascii=True)
        parsed = parse_structured_model_output(raw)
        if parsed is not None:
            return parsed

        # Safety fallback (should be unreachable unless parser contract changes unexpectedly).
        return ModelOutput(
            raw=raw,
            suggestion=suggestion,
            analysis={},
            metadata={"schema": "scambait.llm.v1", "fallback": True},
            actions=[{"type": "noop"}],
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
