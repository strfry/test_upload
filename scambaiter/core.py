from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .forward_meta import baiter_name_from_meta, scammer_name_from_meta
from .model_client import call_hf_openai_chat, extract_result_text

EventType = str
RoleType = str

SYSTEM_PROMPT_CONTRACT = """You are the ScamBaiter.
Primary mission:
- Keep the scammer engaged in conversation and steer toward concrete, verifiable details.
- Use a play-along-lightly style: stay natural, curious, and progress-focused without sounding defensive.
- Do not drift into generic consumer safety advisory tone unless the operator explicitly asks for it.
Conversation style rules:
- Prefer specific follow-up questions tied to the latest counterparty claim.
- Keep momentum; avoid moralizing disclaimers and avoid ending the thread early.
- Never make real commitments to send money, reveal credentials, or perform real financial actions.
Return exactly one valid JSON object with top-level keys: schema, analysis, message, actions.
Rules:
- schema must be exactly \"scambait.llm.v1\".
- analysis must be an object.
- message must be an object (reserved for metadata/compat).
- actions must be a non-empty array (max 10 actions).
- send_message action must include message.text (string, <= 4000 chars).
- No markdown, no prose outside JSON, no function-call wrappers.
Allowed action types:
- mark_read
- simulate_typing (duration_seconds 0..60)
- wait (value >=0, unit in seconds|minutes; max 86400 seconds / 10080 minutes)
- send_message (message.text required; optional reply_to, optional send_at_utc)
- edit_message (message_id, new_text)
- delete_message (message_id)
- noop
- escalate_to_human (reason)
"""

MEMORY_SUMMARY_PROMPT_CONTRACT = """You are ScamBaiter memory summarizer.
Return exactly one valid JSON object with this schema:
{
  "schema": "scambait.memory.v1",
  "claimed_identity": {"name": string, "role_claim": string, "confidence": "low|medium|high"},
  "narrative": {"phase": string, "short_story": string, "timeline_points": [string]},
  "current_intent": {"scammer_intent": string, "baiter_intent": string, "latest_topic": string},
  "key_facts": object,
  "risk_flags": [string],
  "open_questions": [string],
  "next_focus": [string]
}
Rules:
- JSON only, no markdown or prose outside JSON.
- Keep key_facts concise and evidence-based.
- Use empty arrays/objects if information is missing.
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
DISALLOWED_STYLE_PHRASES = (
    "qualified financial advisor",
    "verify the platform's legitimacy",
    "if you have concerns about potential scams",
    "next steps to protect yourself",
    "request a written agreement",
    "independent legal advice",
    "risk-free high yields is a red flag",
)


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


@dataclass(slots=True)
class ValidationIssue:
    path: str
    reason: str
    expected: str | None = None
    actual: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "reason": self.reason}
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not None:
            payload["actual"] = self.actual
        return payload


@dataclass(slots=True)
class ParseResult:
    output: ModelOutput | None
    issues: list[ValidationIssue]


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


def _validate_actions(actions_value: object) -> tuple[list[dict[str, Any]] | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []

    def _fail(path: str, reason: str, expected: str | None = None, actual: str | None = None) -> None:
        issues.append(ValidationIssue(path=path, reason=reason, expected=expected, actual=actual))

    if isinstance(actions_value, dict):
        actions_value = [actions_value]
    if not isinstance(actions_value, list):
        _fail("actions", "must be an array", expected="array", actual=type(actions_value).__name__)
        return None, issues
    if len(actions_value) > 10:
        _fail("actions", "too many actions", expected="<=10", actual=str(len(actions_value)))
        return None, issues
    if not actions_value:
        _fail("actions", "must not be empty", expected="non-empty array")
        return None, issues

    normalized_actions: list[dict[str, Any]] = []
    for idx, action in enumerate(actions_value):
        action_path = f"actions[{idx}]"
        normalized_action = normalize_action_shape(action)
        if not isinstance(normalized_action, dict):
            _fail(action_path, "must be an object", expected="object", actual=type(action).__name__)
            return None, issues

        action_type = normalized_action.get("type")
        if not isinstance(action_type, str) or action_type not in ALLOWED_ACTION_TYPES:
            _fail(f"{action_path}.type", "unknown or missing action type", expected="allowed action type")
            return None, issues

        if action_type == "mark_read":
            if set(normalized_action.keys()) != {"type"}:
                _fail(
                    action_path,
                    "unexpected keys for mark_read",
                    expected="{'type'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            normalized_actions.append({"type": "mark_read"})
            continue

        if action_type == "simulate_typing":
            if set(normalized_action.keys()) != {"type", "duration_seconds"}:
                _fail(
                    action_path,
                    "unexpected keys for simulate_typing",
                    expected="{'type','duration_seconds'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            duration = normalized_action.get("duration_seconds")
            if not isinstance(duration, (int, float)) or duration < 0 or duration > 60:
                _fail(
                    f"{action_path}.duration_seconds",
                    "duration out of range",
                    expected="number in [0,60]",
                    actual=str(duration),
                )
                return None, issues
            normalized_actions.append({"type": "simulate_typing", "duration_seconds": float(duration)})
            continue

        if action_type == "wait":
            if set(normalized_action.keys()) != {"type", "value", "unit"}:
                _fail(
                    action_path,
                    "unexpected keys for wait",
                    expected="{'type','value','unit'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            value = normalized_action.get("value")
            unit = normalized_action.get("unit")
            if not isinstance(value, (int, float)) or not isinstance(unit, str):
                _fail(
                    action_path,
                    "invalid wait payload",
                    expected="value:number and unit:string",
                    actual=f"value={value!r}, unit={unit!r}",
                )
                return None, issues
            unit_norm = unit.strip().lower()
            if unit_norm not in {"seconds", "minutes"}:
                _fail(f"{action_path}.unit", "invalid wait unit", expected="seconds|minutes", actual=unit_norm)
                return None, issues
            numeric_value = float(value)
            if numeric_value < 0:
                _fail(f"{action_path}.value", "wait value must be >= 0", expected=">=0", actual=str(numeric_value))
                return None, issues
            if unit_norm == "seconds" and numeric_value > 86400:
                _fail(
                    f"{action_path}.value",
                    "wait seconds exceed max",
                    expected="<=86400",
                    actual=str(numeric_value),
                )
                return None, issues
            if unit_norm == "minutes" and numeric_value > 10080:
                _fail(
                    f"{action_path}.value",
                    "wait minutes exceed max",
                    expected="<=10080",
                    actual=str(numeric_value),
                )
                return None, issues
            normalized_actions.append({"type": "wait", "value": numeric_value, "unit": unit_norm})
            continue

        if action_type == "send_message":
            allowed = {"type", "message", "reply_to", "send_at_utc"}
            keys = set(normalized_action.keys())
            if not keys.issubset(allowed) or "type" not in keys:
                _fail(
                    action_path,
                    "unexpected keys for send_message",
                    expected="subset of {'type','message','reply_to','send_at_utc'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            message_obj = normalized_action.get("message")
            if not isinstance(message_obj, dict):
                _fail(f"{action_path}.message", "missing message object", expected="object with text")
                return None, issues
            message_text = message_obj.get("text")
            if not isinstance(message_text, str):
                _fail(f"{action_path}.message.text", "missing text", expected="string", actual=type(message_text).__name__)
                return None, issues
            text = message_text.strip()
            if not text:
                _fail(f"{action_path}.message.text", "text must be non-empty")
                return None, issues
            if len(text) > 4000:
                _fail(
                    f"{action_path}.message.text",
                    "text too long",
                    expected="<=4000 chars",
                    actual=str(len(text)),
                )
                return None, issues
            entry: dict[str, Any] = {"type": "send_message", "message": {"text": text}}
            if "reply_to" in normalized_action:
                reply_to = normalized_action.get("reply_to")
                if not isinstance(reply_to, (str, int)):
                    _fail(f"{action_path}.reply_to", "invalid reply_to", expected="string|int", actual=type(reply_to).__name__)
                    return None, issues
                entry["reply_to"] = reply_to
            if "send_at_utc" in normalized_action:
                send_at_utc = normalized_action.get("send_at_utc")
                if not isinstance(send_at_utc, str):
                    _fail(
                        f"{action_path}.send_at_utc",
                        "invalid send_at_utc type",
                        expected="string",
                        actual=type(send_at_utc).__name__,
                    )
                    return None, issues
                normalized_ts = normalize_iso_utc(send_at_utc)
                if not normalized_ts:
                    _fail(f"{action_path}.send_at_utc", "invalid ISO timestamp", expected="ISO8601 UTC string")
                    return None, issues
                entry["send_at_utc"] = normalized_ts
            normalized_actions.append(entry)
            continue

        if action_type == "edit_message":
            if set(normalized_action.keys()) != {"type", "message_id", "new_text"}:
                _fail(
                    action_path,
                    "unexpected keys for edit_message",
                    expected="{'type','message_id','new_text'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            message_id = normalized_action.get("message_id")
            new_text = normalized_action.get("new_text")
            if not isinstance(message_id, (str, int)) or not isinstance(new_text, str):
                _fail(
                    action_path,
                    "invalid edit_message payload",
                    expected="message_id:string|int and new_text:string",
                    actual=f"message_id={message_id!r}, new_text={type(new_text).__name__}",
                )
                return None, issues
            normalized_actions.append({"type": "edit_message", "message_id": message_id, "new_text": new_text})
            continue

        if action_type == "noop":
            if set(normalized_action.keys()) != {"type"}:
                _fail(
                    action_path,
                    "unexpected keys for noop",
                    expected="{'type'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            normalized_actions.append({"type": "noop"})
            continue

        if action_type == "escalate_to_human":
            if set(normalized_action.keys()) != {"type", "reason"}:
                _fail(
                    action_path,
                    "unexpected keys for escalate_to_human",
                    expected="{'type','reason'}",
                    actual=str(sorted(normalized_action.keys())),
                )
                return None, issues
            reason = normalized_action.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                _fail(f"{action_path}.reason", "reason must be non-empty string")
                return None, issues
            normalized_actions.append({"type": "escalate_to_human", "reason": reason.strip()})
            continue

        _fail(action_path, "unsupported action")
        return None, issues

    return normalized_actions, issues


def _build_repair_messages(
    failed_generation: str,
    reject_reason: str = "contract_validation_failed",
) -> list[dict[str, str]]:
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
        {
            "role": "system",
            "content": (
                "If reject_reason is style_policy_violation, keep the output in-role as ScamBaiter and avoid "
                "generic financial safety/advisory language."
            ),
        },
        {
            "role": "system",
            "content": "For send_message actions, use actions[].message.text. Do not use actions[].text.",
        },
        {"role": "user", "content": json.dumps({"failed_generation": clipped}, ensure_ascii=True)},
        {"role": "user", "content": json.dumps({"reject_reason": reject_reason}, ensure_ascii=True)},
    ]


def parse_structured_model_output_detailed(text: str) -> ParseResult:
    issues: list[ValidationIssue] = []

    def _fail(path: str, reason: str, expected: str | None = None, actual: str | None = None) -> ParseResult:
        issues.append(ValidationIssue(path=path, reason=reason, expected=expected, actual=actual))
        return ParseResult(output=None, issues=issues)

    cleaned = strip_think_segments(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return _fail("root", "invalid json", expected="valid JSON object")

    if not isinstance(data, dict):
        return _fail("root", "must be an object", expected="object", actual=type(data).__name__)

    required_keys = {"schema", "analysis", "message", "actions"}
    if not required_keys.issubset(set(str(key) for key in data.keys())):
        missing = sorted(required_keys - set(str(key) for key in data.keys()))
        return _fail("root", "missing required top-level keys", expected="schema,analysis,message,actions", actual=str(missing))

    schema_value = data.get("schema")
    if not isinstance(schema_value, str) or schema_value.strip() != "scambait.llm.v1":
        return _fail("schema", "invalid schema", expected="scambait.llm.v1", actual=str(schema_value))

    analysis_value = data.get("analysis")
    if not isinstance(analysis_value, dict):
        return _fail("analysis", "analysis must be object", expected="object", actual=type(analysis_value).__name__)

    message_value = data.get("message")
    if not isinstance(message_value, dict):
        return _fail("message", "message must be object", expected="object", actual=type(message_value).__name__)

    normalized_actions, action_issues = _validate_actions(data.get("actions"))
    if normalized_actions is None:
        return ParseResult(output=None, issues=action_issues)

    suggestion = ""
    for action in normalized_actions:
        if str(action.get("type") or "") == "send_message":
            message_obj = action.get("message")
            if isinstance(message_obj, dict):
                candidate = message_obj.get("text")
                if isinstance(candidate, str) and candidate.strip():
                    suggestion = candidate.strip()
                    break

    if not suggestion:
        text_value = message_value.get("text")
        if isinstance(text_value, str) and text_value.strip():
            suggestion = text_value.strip()
        else:
            return _fail(
                "actions",
                "missing send_message action with message.text",
                expected="at least one send_message action containing message.text",
            )

    return ParseResult(output=ModelOutput(
        raw=text,
        suggestion=suggestion,
        analysis=analysis_value,
        metadata={"schema": "scambait.llm.v1"},
        actions=normalized_actions,
    ), issues=[])


def parse_structured_model_output(text: str) -> ModelOutput | None:
    parsed = parse_structured_model_output_detailed(text)
    return parsed.output


def violates_scambait_style_policy(reply_text: str) -> bool:
    text = reply_text.strip().lower()
    if not text:
        return False
    matches = sum(1 for phrase in DISALLOWED_STYLE_PHRASES if phrase in text)
    if matches >= 2:
        return True
    if "qualified financial advisor" in text:
        return True
    if "next steps to protect yourself" in text:
        return True
    return False


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
            # Legacy rows may contain synthetic profile_update system messages from older builds.
            # Skip these in prompt context to avoid profile noise amplification.
            if str(getattr(event, "role", "")) == "system":
                text_value = getattr(event, "text", None)
                if isinstance(text_value, str) and text_value.strip().startswith("profile_update:"):
                    continue
            prompt_events.append(
                {
                    "event_type": event.event_type,
                    "role": event.role,
                    "text": event.text,
                    "time": self._as_hhmm(event.ts_utc),
                    "meta": event.meta,
                }
            )
        return self._trim_prompt_events(prompt_events, token_budget)

    def build_memory_events(self, chat_id: int, after_event_id: int = 0) -> list[dict[str, Any]]:
        events = self.store.list_events(chat_id=chat_id, limit=5000)
        out: list[dict[str, Any]] = []
        for event in events:
            event_id = int(getattr(event, "id", 0))
            if event_id <= int(after_event_id):
                continue
            meta = getattr(event, "meta", None)
            if not isinstance(meta, dict):
                meta = {}
            media_type = str(getattr(event, "event_type", "") or "")
            caption = None
            if media_type == "photo":
                text_value = getattr(event, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    caption = text_value.strip()
            out.append(
                {
                    "event_id": event_id,
                    "ts_utc": getattr(event, "ts_utc", None),
                    "role": getattr(event, "role", None),
                    "scammer_username": scammer_name_from_meta(meta),
                    "baiter_username": baiter_name_from_meta(meta),
                    "text": getattr(event, "text", None),
                    "caption": caption,
                    "citation": meta.get("citation"),
                    "media_type": media_type,
                }
            )
        return out

    def _build_memory_messages(
        self,
        *,
        chat_id: int,
        cursor_event_id: int,
        existing_memory: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        payload = {
            "schema": "scambait.memory.input.v1",
            "chat_id": chat_id,
            "memory_cursor_event_id": int(cursor_event_id),
            "existing_memory": existing_memory,
            "events": events,
        }
        return [
            {"role": "system", "content": MEMORY_SUMMARY_PROMPT_CONTRACT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
        ]

    @staticmethod
    def _parse_memory_summary_output(text: str) -> dict[str, Any] | None:
        cleaned = strip_think_segments(text)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        if str(value.get("schema") or "").strip() != "scambait.memory.v1":
            return None
        required = {
            "claimed_identity",
            "narrative",
            "current_intent",
            "key_facts",
            "risk_flags",
            "open_questions",
            "next_focus",
        }
        if not required.issubset(value.keys()):
            return None
        return value

    def ensure_memory_context(self, chat_id: int, force_refresh: bool = False) -> dict[str, Any]:
        current = self.store.get_memory_context(chat_id=chat_id)
        latest_events = self.store.list_events(chat_id=chat_id, limit=5000)
        latest_id = int(latest_events[-1].id) if latest_events else 0

        if current is not None and not force_refresh and int(current.cursor_event_id) >= latest_id:
            return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}

        cursor = 0
        existing_summary = None
        if current is not None:
            existing_summary = current.summary
            if not force_refresh:
                cursor = int(current.cursor_event_id)
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_memory_model", None) or "").strip() or "openai/gpt-oss-120b"
        max_tokens = int(getattr(self.config, "hf_memory_max_tokens", 150000))
        if not token:
            # Offline-safe fallback when HF token is unavailable.
            fallback_summary = {
                "schema": "scambait.memory.v1",
                "claimed_identity": {"name": "", "role_claim": "", "confidence": "low"},
                "narrative": {"phase": "unknown", "short_story": "", "timeline_points": []},
                "current_intent": {"scammer_intent": "", "baiter_intent": "", "latest_topic": ""},
                "key_facts": {},
                "risk_flags": [],
                "open_questions": [],
                "next_focus": [],
            }
            if current is None or force_refresh or int(current.cursor_event_id) < latest_id:
                saved = self.store.upsert_memory_context(
                    chat_id=chat_id,
                    summary=fallback_summary,
                    cursor_event_id=latest_id,
                    model=model,
                )
                return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}
            return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}

        events = self.build_memory_events(chat_id=chat_id, after_event_id=cursor)
        if not events:
            if current is not None:
                return {"summary": current.summary, "cursor_event_id": current.cursor_event_id, "updated": False}
            empty_summary = {
                "schema": "scambait.memory.v1",
                "claimed_identity": {"name": "", "role_claim": "", "confidence": "low"},
                "narrative": {"phase": "empty", "short_story": "", "timeline_points": []},
                "current_intent": {"scammer_intent": "", "baiter_intent": "", "latest_topic": ""},
                "key_facts": {},
                "risk_flags": [],
                "open_questions": [],
                "next_focus": [],
            }
            saved = self.store.upsert_memory_context(
                chat_id=chat_id,
                summary=empty_summary,
                cursor_event_id=0,
                model=model,
            )
            return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}

        messages = self._build_memory_messages(
            chat_id=chat_id,
            cursor_event_id=cursor,
            existing_memory=existing_summary,
            events=events,
        )
        response = call_hf_openai_chat(
            token=token,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            base_url=(getattr(self.config, "hf_base_url", None) or None),
        )
        result_text = extract_result_text(response)
        parsed = self._parse_memory_summary_output(result_text)
        if parsed is None:
            raise RuntimeError("invalid memory summary contract (expected scambait.memory.v1)")
        latest_cursor = int(events[-1]["event_id"])
        saved = self.store.upsert_memory_context(
            chat_id=chat_id,
            summary=parsed,
            cursor_event_id=latest_cursor,
            model=model,
        )
        return {"summary": saved.summary, "cursor_event_id": saved.cursor_event_id, "updated": True}

    def build_model_messages(
        self,
        chat_id: int,
        token_limit: int | None = None,
        force_refresh_memory: bool = False,
        include_memory: bool = True,
    ) -> list[dict[str, str]]:
        prompt_events = self.build_prompt_events(chat_id=chat_id, token_limit=token_limit)
        memory_state: dict[str, Any] = {"summary": {}, "cursor_event_id": 0, "updated": False}
        if include_memory:
            memory_state = self.ensure_memory_context(chat_id=chat_id, force_refresh=force_refresh_memory)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT_CONTRACT},
            {
                "role": "system",
                "content": (
                    "Memory summary for chat_id="
                    f"{chat_id}: {json.dumps(memory_state.get('summary') or {}, ensure_ascii=True)}"
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
        initial_messages = self.build_model_messages(chat_id=chat_id, include_memory=False)
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

        parsed_result = parse_structured_model_output_detailed(initial_text)
        parsed = parsed_result.output
        initial_reject_reason: str | None = None
        if parsed is None:
            initial_reject_reason = "contract_validation_failed"
        elif violates_scambait_style_policy(parsed.suggestion):
            initial_reject_reason = "style_policy_violation"
            parsed = None
        attempts.append(
            {
                "phase": "initial",
                "status": "ok" if parsed is not None else "invalid",
                "accepted": parsed is not None,
                "reject_reason": initial_reject_reason,
                "error_message": None,
                "contract_issues": [item.as_dict() for item in parsed_result.issues] if parsed is None else [],
                "prompt_json": initial_prompt,
                "response_json": initial_response,
                "result_text": initial_text,
            }
        )

        final_prompt = initial_prompt
        final_response = initial_response
        final_text = initial_text

        if parsed is None:
            repair_messages = _build_repair_messages(
                initial_text,
                reject_reason=initial_reject_reason or "contract_validation_failed",
            )
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
                repaired_result = parse_structured_model_output_detailed(repair_text)
                repaired = repaired_result.output
                repair_reject_reason: str | None = None
                if repaired is None:
                    repair_reject_reason = "contract_validation_failed"
                elif violates_scambait_style_policy(repaired.suggestion):
                    repair_reject_reason = "style_policy_violation"
                    repaired = None
                attempts.append(
                    {
                        "phase": "repair",
                        "status": "ok" if repaired is not None else "invalid",
                        "accepted": repaired is not None,
                        "reject_reason": repair_reject_reason,
                        "error_message": None,
                        "contract_issues": [item.as_dict() for item in repaired_result.issues] if repaired is None else [],
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

        contract_issues: list[dict[str, Any]] = []
        if parsed is None:
            for attempt in reversed(attempts):
                candidate = attempt.get("contract_issues")
                if isinstance(candidate, list) and candidate:
                    contract_issues = candidate
                    break
        first_issue = contract_issues[0] if contract_issues else None
        first_issue_str = ""
        if isinstance(first_issue, dict):
            issue_path = str(first_issue.get("path") or "").strip()
            issue_reason = str(first_issue.get("reason") or "").strip()
            if issue_path or issue_reason:
                first_issue_str = f" ({issue_path}: {issue_reason})"

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
            "contract_issues": contract_issues,
            "error_message": (
                None
                if parsed is not None
                else (
                    "model output violates scambait style policy"
                    if any(str(item.get("reject_reason") or "") == "style_policy_violation" for item in attempts)
                    else "invalid model output contract (expected scambait.llm.v1 with analysis/message/actions)"
                    + first_issue_str
                )
            ),
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
            "actions": [{"type": "send_message", "message": {"text": suggestion}}],
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
