from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .forward_meta import baiter_name_from_meta, scammer_name_from_meta
from .model_client import call_hf_openai_chat, extract_reasoning_details, extract_result_text, extract_tool_calls

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
You operate strictly through the act() tool. Never output free text.
Per turn, call act() exactly once with an actions array containing:
- set_memory and add_note entries as needed (before the message action).
- At most one send_message OR one wait, never both.
- send_typing is optional, use sparingly.
- If the situation requires human review, include a decide_handoff action with a reason.
- If no message should be sent, omit send_message entirely.
Safety: Never send real name, address, phone, email, financial data, or admit you are a bot.
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
ALLOWED_CONFLICT_CODES = {
    "policy_tension",
    "insufficient_context",
    "conversation_stall",
    "operator_required",
    "uncertain_target",
}
ALLOWED_CONFLICT_MODES = {"pivot", "escalate", "hold"}
TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": (
                "Execute one or more actions for this turn. "
                "Include set_memory/add_note before send_message. "
                "At most one send_message OR one wait per turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "anyOf": [
                                {
                                    "properties": {
                                        "type": {"const": "set_memory"},
                                        "key": {"type": "string"},
                                        "value": {"type": "string"},
                                    },
                                    "required": ["type", "key", "value"],
                                },
                                {
                                    "properties": {
                                        "type": {"const": "add_note"},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["type", "text"],
                                },
                                {
                                    "properties": {
                                        "type": {"const": "send_message"},
                                        "text": {"type": "string", "maxLength": 4000},
                                        "reply_to": {"type": "integer"},
                                    },
                                    "required": ["type", "text"],
                                },
                                {
                                    "properties": {
                                        "type": {"const": "send_typing"},
                                        "duration_seconds": {"type": "number", "minimum": 0, "maximum": 60},
                                    },
                                    "required": ["type", "duration_seconds"],
                                },
                                {
                                    "properties": {
                                        "type": {"const": "wait"},
                                        "latency_class": {"type": "string", "enum": ["short", "medium", "long"]},
                                    },
                                    "required": ["type", "latency_class"],
                                },
                                {
                                    "properties": {
                                        "type": {"const": "decide_handoff"},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["type", "reason"],
                                },
                            ],
                        },
                    },
                },
                "required": ["actions"],
            },
        },
    },
]
_WAIT_LATENCY_MAP: dict[str, tuple[int, str]] = {
    "short": (30, "seconds"),
    "medium": (3, "minutes"),
    "long": (15, "minutes"),
}
SEMANTIC_CONFLICT_REASON_HINTS = (
    "cannot",
    "can't",
    "unable",
    "insufficient",
    "unclear",
    "uncertain",
    "policy",
    "not enough context",
)
META_TURN_PROMPT_CONTRACT = """You are ScamBaiter Meta Core.
Return exactly one valid JSON object with this schema:
{
  "schema": "scambait.meta.turn.v1",
  "turn_options": [
    {"text": string, "strategy": string, "risk": "low|med|high"}
  ],
  "recommended_text": string
}
Rules:
- JSON only, no markdown.
- Focus on conversation redirection for scambaiting.
- Do not include real commitments, credentials, or money transfer promises.
- Provide 1-3 options.
"""


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
    conflict: dict[str, Any] | None = None


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
        normalized = dict(action)
        action_type = str(normalized.get("type") or "").strip()
        # Some models emit dotted key aliases like "message.text" for send_message.
        # Normalize to the contract shape: {"message": {"text": ...}}.
        if action_type == "send_message" and "message.text" in normalized:
            dotted_text = normalized.pop("message.text")
            message_obj = normalized.get("message")
            if not isinstance(message_obj, dict):
                message_obj = {}
            if "text" not in message_obj:
                message_obj["text"] = dotted_text
            normalized["message"] = message_obj
        return normalized

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


def _validate_conflict(conflict_value: object) -> tuple[dict[str, Any] | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []

    def _fail(path: str, reason: str, expected: str | None = None, actual: str | None = None) -> None:
        issues.append(ValidationIssue(path=path, reason=reason, expected=expected, actual=actual))

    if conflict_value is None:
        return None, issues
    if not isinstance(conflict_value, dict):
        _fail("conflict", "conflict must be object", expected="object", actual=type(conflict_value).__name__)
        return None, issues
    conflict_type = str(conflict_value.get("type") or "").strip()
    if conflict_type != "semantic_conflict":
        _fail("conflict.type", "invalid conflict type", expected="semantic_conflict", actual=conflict_type or "missing")
        return None, issues
    code_value = str(conflict_value.get("code") or "").strip().lower() or "operator_required"
    if code_value not in ALLOWED_CONFLICT_CODES:
        _fail(
            "conflict.code",
            "invalid conflict code",
            expected="|".join(sorted(ALLOWED_CONFLICT_CODES)),
            actual=code_value,
        )
        return None, issues
    reason_value = str(conflict_value.get("reason") or "").strip()
    if not reason_value:
        _fail("conflict.reason", "conflict reason must be non-empty string", expected="string")
        return None, issues
    if len(reason_value) > 2000:
        _fail("conflict.reason", "reason too long", expected="<=2000 chars", actual=str(len(reason_value)))
        return None, issues
    requires_human = conflict_value.get("requires_human")
    if requires_human is None:
        requires_human = True
    if not isinstance(requires_human, bool):
        _fail(
            "conflict.requires_human",
            "requires_human must be boolean",
            expected="bool",
            actual=type(requires_human).__name__,
        )
        return None, issues
    suggested_mode = str(conflict_value.get("suggested_mode") or "hold").strip().lower()
    if suggested_mode not in ALLOWED_CONFLICT_MODES:
        _fail(
            "conflict.suggested_mode",
            "invalid suggested_mode",
            expected="pivot|escalate|hold",
            actual=suggested_mode,
        )
        return None, issues
    return {
        "type": "semantic_conflict",
        "code": code_value,
        "reason": reason_value,
        "requires_human": requires_human,
        "suggested_mode": suggested_mode,
    }, issues


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

    normalized_conflict, conflict_issues = _validate_conflict(data.get("conflict"))
    if conflict_issues:
        return ParseResult(output=None, issues=conflict_issues)

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

    if not suggestion and normalized_conflict is None:
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
        conflict=normalized_conflict,
    ), issues=[])


def parse_structured_model_output(text: str) -> ModelOutput | None:
    parsed = parse_structured_model_output_detailed(text)
    return parsed.output


def parse_tool_calls_to_model_output(
    tool_calls: list[dict[str, Any]],
    raw_response: str = "",
) -> tuple[ParseResult, list[tuple[str, str]]]:
    issues: list[ValidationIssue] = []

    if not tool_calls:
        issues.append(ValidationIssue(
            path="tool_calls",
            reason="no tool calls returned",
            expected="at least one tool call",
        ))
        return ParseResult(output=None, issues=issues), []

    # Find the act() tool call
    act_call = None
    for call in tool_calls:
        func = call.get("function") if isinstance(call, dict) else None
        if isinstance(func, dict) and str(func.get("name") or "").strip() == "act":
            act_call = func
            break

    if act_call is None:
        issues.append(ValidationIssue(
            path="tool_calls",
            reason="no act() tool call",
            expected="act() tool call",
        ))
        return ParseResult(output=None, issues=issues), []

    raw_args = act_call.get("arguments") or "{}"
    if isinstance(raw_args, str):
        try:
            act_args: dict[str, Any] = json.loads(raw_args)
        except json.JSONDecodeError:
            issues.append(ValidationIssue(path="act.arguments", reason="invalid JSON in act() arguments"))
            return ParseResult(output=None, issues=issues), []
    elif isinstance(raw_args, dict):
        act_args = raw_args
    else:
        act_args = {}

    action_list = act_args.get("actions")
    if not isinstance(action_list, list) or not action_list:
        issues.append(ValidationIssue(
            path="act.actions",
            reason="actions must be a non-empty array",
            expected="non-empty array",
        ))
        return ParseResult(output=None, issues=issues), []

    actions: list[dict[str, Any]] = []
    analysis: dict[str, Any] = {}
    suggestion: str = ""
    memory_pairs: list[tuple[str, str]] = []
    send_message_count = 0
    wait_count = 0

    for action_item in action_list:
        if not isinstance(action_item, dict):
            continue
        action_type = str(action_item.get("type") or "").strip()

        if action_type == "set_memory":
            key = str(action_item.get("key") or "").strip()
            value = str(action_item.get("value") or "").strip()
            if key:
                memory_pairs.append((key, value))
                analysis[key] = value
        elif action_type == "add_note":
            text = str(action_item.get("text") or "").strip()
            if text:
                notes = analysis.setdefault("notes", [])
                if isinstance(notes, list):
                    notes.append(text)
        elif action_type == "send_message":
            if send_message_count >= 1:
                issues.append(ValidationIssue(
                    path="act.actions",
                    reason="duplicate send_message action skipped",
                    expected="at most one send_message per turn",
                ))
                continue
            msg_text = str(action_item.get("text") or "").strip()
            if not msg_text:
                issues.append(ValidationIssue(
                    path="act.actions.send_message.text",
                    reason="text must be non-empty",
                ))
                continue
            if len(msg_text) > 4000:
                msg_text = msg_text[:4000]
            action: dict[str, Any] = {"type": "send_message", "message": {"text": msg_text}}
            reply_to = action_item.get("reply_to")
            if reply_to is not None:
                try:
                    action["reply_to"] = int(reply_to)
                except (TypeError, ValueError):
                    pass
            actions.append(action)
            suggestion = msg_text
            send_message_count += 1
        elif action_type == "send_typing":
            raw_dur = action_item.get("duration_seconds", 5)
            try:
                duration = float(raw_dur)
            except (TypeError, ValueError):
                duration = 5.0
            duration = max(0.0, min(60.0, duration))
            actions.append({"type": "simulate_typing", "duration_seconds": duration})
        elif action_type == "wait":
            if wait_count >= 1:
                issues.append(ValidationIssue(
                    path="act.actions",
                    reason="duplicate wait action skipped",
                    expected="at most one wait per turn",
                ))
                continue
            latency_class = str(action_item.get("latency_class") or "short").strip()
            value_unit = _WAIT_LATENCY_MAP.get(latency_class, _WAIT_LATENCY_MAP["short"])
            actions.append({"type": "wait", "value": value_unit[0], "unit": value_unit[1]})
            wait_count += 1
        elif action_type == "decide_handoff":
            reason = str(action_item.get("reason") or "").strip()
            if not reason:
                reason = "Handoff requested."
            actions.append({"type": "escalate_to_human", "reason": reason})
        else:
            issues.append(ValidationIssue(
                path=f"act.actions.{action_type}",
                reason=f"unknown action type: {action_type}",
            ))

    # If only set_memory/add_note were called (no executable actions) → noop
    if not actions:
        actions.append({"type": "noop"})

    output = ModelOutput(
        raw=raw_response,
        suggestion=suggestion,
        analysis=analysis,
        metadata={"schema": "scambait.llm.v1"},
        actions=actions,
        conflict=None,
    )
    return ParseResult(output=output, issues=issues), memory_pairs


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
        current = self.store.get_summary(chat_id=chat_id)
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
                saved = self.store.upsert_summary(
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
            saved = self.store.upsert_summary(
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
        saved = self.store.upsert_summary(
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

    @staticmethod
    def _extract_analysis_reason_from_result_text(result_text: str) -> str:
        cleaned = strip_think_segments(result_text or "")
        if not cleaned:
            return ""
        try:
            loaded = json.loads(cleaned)
        except Exception:
            return ""
        if not isinstance(loaded, dict):
            return ""
        analysis = loaded.get("analysis")
        if not isinstance(analysis, dict):
            return ""
        reason = analysis.get("reason")
        if not isinstance(reason, str):
            return ""
        return reason.strip()

    @staticmethod
    def _classify_conflict_code(reason: str) -> str:
        text = (reason or "").strip().lower()
        if not text:
            return "operator_required"
        if "insufficient" in text or "not enough context" in text or "unclear" in text or "uncertain" in text:
            return "insufficient_context"
        if "policy" in text or "cannot" in text or "can't" in text or "unable" in text:
            return "policy_tension"
        if "stall" in text:
            return "conversation_stall"
        if "target" in text:
            return "uncertain_target"
        return "operator_required"

    def _detect_semantic_conflict(self, parsed: ModelOutput | None, result_text: str) -> tuple[bool, dict[str, Any] | None]:
        if parsed is not None and isinstance(parsed.conflict, dict):
            return True, parsed.conflict
        analysis_reason = ""
        if parsed is not None and isinstance(parsed.analysis, dict):
            candidate = parsed.analysis.get("reason")
            if isinstance(candidate, str):
                analysis_reason = candidate.strip()
        if not analysis_reason:
            analysis_reason = self._extract_analysis_reason_from_result_text(result_text)
        reason_lower = analysis_reason.lower()
        has_reason_hint = bool(reason_lower) and any(hint in reason_lower for hint in SEMANTIC_CONFLICT_REASON_HINTS)
        has_escalate = False
        if parsed is not None:
            for action in parsed.actions:
                if isinstance(action, dict) and str(action.get("type") or "") == "escalate_to_human":
                    has_escalate = True
                    break
        if has_escalate or has_reason_hint:
            reason = analysis_reason or "Semantic conflict signaled by model output."
            return True, {
                "type": "semantic_conflict",
                "code": self._classify_conflict_code(reason),
                "reason": reason,
                "requires_human": True,
                "suggested_mode": "hold",
            }
        return False, None

    @staticmethod
    def _parse_meta_turn_output(result_text: str) -> dict[str, Any] | None:
        cleaned = strip_think_segments(result_text or "")
        if not cleaned:
            return None
        try:
            loaded = json.loads(cleaned)
        except Exception:
            return None
        if not isinstance(loaded, dict):
            return None
        if str(loaded.get("schema") or "").strip() != "scambait.meta.turn.v1":
            return None
        recommended_text = loaded.get("recommended_text")
        turn_options = loaded.get("turn_options")
        if not isinstance(recommended_text, str) or not recommended_text.strip():
            return None
        if not isinstance(turn_options, list):
            return None
        normalized_options: list[dict[str, str]] = []
        for item in turn_options[:3]:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            strategy = item.get("strategy")
            risk = item.get("risk")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(strategy, str):
                strategy = ""
            risk_value = str(risk or "").strip().lower()
            if risk_value not in {"low", "med", "high"}:
                risk_value = "med"
            normalized_options.append(
                {
                    "text": text.strip(),
                    "strategy": strategy.strip(),
                    "risk": risk_value,
                }
            )
        if not normalized_options:
            normalized_options.append({"text": recommended_text.strip(), "strategy": "", "risk": "med"})
        return {
            "schema": "scambait.meta.turn.v1",
            "recommended_text": recommended_text.strip(),
            "turn_options": normalized_options,
        }

    def _build_semantic_pivot(self, chat_id: int, conflict: dict[str, Any] | None) -> dict[str, Any] | None:
        token = (getattr(self.config, "hf_token", None) or "").strip()
        if not token:
            return None
        model = (getattr(self.config, "hf_memory_model", None) or "").strip() or (getattr(self.config, "hf_model", None) or "").strip()
        if not model:
            return None
        max_tokens = int(getattr(self.config, "hf_memory_max_tokens", 150000))
        prompt_events = self.build_prompt_events(chat_id=chat_id, token_limit=int(getattr(self.config, "hf_max_tokens", 1500)))
        memory_state = self.store.get_summary(chat_id=chat_id)
        payload = {
            "schema": "scambait.meta.turn.input.v1",
            "chat_id": chat_id,
            "conflict": conflict or {},
            "recent_messages": prompt_events[-20:],
            "memory_summary": memory_state.summary if memory_state is not None else {},
        }
        messages = [
            {"role": "system", "content": META_TURN_PROMPT_CONTRACT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
        ]
        response = call_hf_openai_chat(
            token=token,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            base_url=None,
        )
        result_text = extract_result_text(response)
        parsed = self._parse_meta_turn_output(result_text)
        if parsed is None:
            raise RuntimeError("invalid meta turn contract (expected scambait.meta.turn.v1)")
        parsed["model"] = model
        return parsed

    def run_hf_dry_run(self, chat_id: int) -> dict[str, Any]:
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_model", None) or "").strip()
        if not token or not model:
            raise RuntimeError("HF_TOKEN/HF_MODEL missing")
        max_tokens = int(getattr(self.config, "hf_max_tokens", 1500))

        attempts: list[dict[str, Any]] = []
        initial_messages = self.build_model_messages(chat_id=chat_id, include_memory=False)
        initial_prompt = {"messages": initial_messages, "max_tokens": max_tokens}
        reasoning_cycles = 0
        reasoning_snippet: str = ""

        try:
            initial_response = call_hf_openai_chat(
                token=token,
                model=model,
                messages=initial_messages,
                max_tokens=max_tokens,
                # Dry run is pinned to HF router to avoid accidental provider drift via HF_BASE_URL.
                base_url=None,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
            )
            initial_text = extract_result_text(initial_response)
            initial_tool_calls = extract_tool_calls(initial_response)
            if not initial_text and initial_tool_calls:
                initial_text = json.dumps(initial_tool_calls, ensure_ascii=True)
            reasoning_cycles, reasoning_snippet = extract_reasoning_details(initial_response)
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
                "outcome_class": "provider_error",
                "semantic_conflict": False,
                "conflict": None,
                "pivot": None,
                "repair_available": False,
                "repair_context": None,
                "attempts": attempts,
                "reasoning_cycles": reasoning_cycles,
                "reasoning_snippet": reasoning_snippet,
            }

        parsed_result, memory_pairs = parse_tool_calls_to_model_output(initial_tool_calls, raw_response=initial_text)
        parsed = parsed_result.output
        initial_reject_reason: str | None = None
        if parsed is None:
            initial_reject_reason = "no_tool_calls"
        elif violates_scambait_style_policy(parsed.suggestion):
            initial_reject_reason = "style_policy_violation"
            parsed = None
        else:
            for k, v in memory_pairs:
                self.store.set_memory_kv(chat_id, k, v)
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

        final_reasoning_cycles = reasoning_cycles
        final_reasoning_snippet = reasoning_snippet
        final_prompt = initial_prompt
        final_response = initial_response
        final_text = initial_text

        if parsed is None and not initial_tool_calls:
            follow_messages = initial_messages + [
                {"role": "user", "content": "Please use the available tools to respond."}
            ]
            follow_prompt = {"messages": follow_messages, "max_tokens": max_tokens}
            try:
                follow_response = call_hf_openai_chat(
                    token=token,
                    model=model,
                    messages=follow_messages,
                    max_tokens=max_tokens,
                    base_url=None,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="required",
                )
                follow_text = extract_result_text(follow_response)
                follow_tool_calls = extract_tool_calls(follow_response)
                if not follow_text and follow_tool_calls:
                    follow_text = json.dumps(follow_tool_calls, ensure_ascii=True)
            except Exception as exc:
                attempts.append(
                    {
                        "phase": "tool_retry",
                        "status": "error",
                        "accepted": False,
                        "reject_reason": "provider_error",
                        "error_message": str(exc),
                        "prompt_json": follow_prompt,
                        "response_json": {},
                        "result_text": "",
                    }
                )
                return {
                    "provider": "huggingface_openai_compat",
                    "model": model,
                    "prompt_json": follow_prompt,
                    "response_json": {},
                    "result_text": "",
                    "valid_output": False,
                    "parsed_output": None,
                    "contract_issues": [],
                    "outcome_class": "provider_error",
                    "semantic_conflict": False,
                    "conflict": None,
                    "pivot": None,
                    "repair_available": False,
                    "repair_context": None,
                    "error_message": str(exc),
                    "attempts": attempts,
                    "reasoning_cycles": final_reasoning_cycles,
                    "reasoning_snippet": final_reasoning_snippet,
                }
            follow_result, follow_memory_pairs = parse_tool_calls_to_model_output(follow_tool_calls, raw_response=follow_text)
            parsed = follow_result.output
            parsed_result = follow_result
            if parsed is not None and not violates_scambait_style_policy(parsed.suggestion):
                for k, v in follow_memory_pairs:
                    self.store.set_memory_kv(chat_id, k, v)
            else:
                if parsed is not None:
                    parsed = None
            attempts.append(
                {
                    "phase": "tool_retry",
                    "status": "ok" if parsed is not None else "invalid",
                    "accepted": parsed is not None,
                    "reject_reason": None,
                    "error_message": None,
                    "contract_issues": [item.as_dict() for item in follow_result.issues] if parsed is None else [],
                    "prompt_json": follow_prompt,
                    "response_json": follow_response,
                    "result_text": follow_text,
                }
            )
            final_prompt = follow_prompt
            final_response = follow_response
            final_text = follow_text

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

        semantic_conflict, conflict_payload = self._detect_semantic_conflict(parsed=parsed, result_text=final_text)
        pivot_payload: dict[str, Any] | None = None
        if semantic_conflict:
            try:
                pivot_payload = self._build_semantic_pivot(chat_id=chat_id, conflict=conflict_payload)
            except Exception as exc:
                pivot_payload = {"error": str(exc)}
            # Keep a deterministic trail in attempts: conflict means operator decision is still required.
            if attempts:
                attempts[-1]["accepted"] = False
                attempts[-1]["reject_reason"] = "semantic_conflict"
                attempts[-1]["status"] = "invalid"

        if semantic_conflict:
            outcome_class = "semantic_conflict"
        elif parsed is None and any(str(item.get("reject_reason") or "") == "style_policy_violation" for item in attempts):
            outcome_class = "style_violation"
        elif parsed is None:
            outcome_class = "contract_invalid"
        else:
            outcome_class = "ok"

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
                "conflict": parsed.conflict,
            }
            if parsed is not None
            else None,
            "contract_issues": contract_issues,
            "outcome_class": outcome_class,
            "semantic_conflict": semantic_conflict,
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "repair_available": parsed is None,
            "repair_context": (
                {
                    "chat_id": chat_id,
                    "reject_reason": initial_reject_reason or "no_tool_calls",
                    "failed_generation_excerpt": final_text[:2000],
                }
                if parsed is None
                else None
            ),
            "error_message": (
                None
                if parsed is not None and not semantic_conflict
                else (
                    "semantic conflict detected (operator decision required)"
                    if semantic_conflict
                    else (
                        "model output violates scambait style policy"
                        if any(str(item.get("reject_reason") or "") == "style_policy_violation" for item in attempts)
                        else "no tool calls in model output"
                        + first_issue_str
                    )
                )
            ),
            "attempts": attempts,
            "reasoning_cycles": final_reasoning_cycles,
            "reasoning_snippet": final_reasoning_snippet,
        }

    def run_hf_dry_run_repair(
        self,
        chat_id: int,
        failed_generation: str,
        reject_reason: str = "contract_validation_failed",
    ) -> dict[str, Any]:
        # `failed_generation` and `reject_reason` are kept for API compatibility but no longer
        # embedded in the prompt — the repair simply forces tool use on a fresh context rebuild.
        _ = (failed_generation, reject_reason)
        token = (getattr(self.config, "hf_token", None) or "").strip()
        model = (getattr(self.config, "hf_model", None) or "").strip()
        if not token or not model:
            raise RuntimeError("HF_TOKEN/HF_MODEL missing")
        max_tokens = int(getattr(self.config, "hf_max_tokens", 1500))
        repair_messages = self.build_model_messages(chat_id=chat_id, include_memory=False)
        repair_prompt = {"messages": repair_messages, "max_tokens": max_tokens}
        attempts: list[dict[str, Any]] = []
        try:
            repair_response = call_hf_openai_chat(
                token=token,
                model=model,
                messages=repair_messages,
                max_tokens=max_tokens,
                base_url=None,
                tools=TOOL_DEFINITIONS,
                tool_choice="required",
            )
            repair_text = extract_result_text(repair_response)
            repair_tool_calls = extract_tool_calls(repair_response)
            if not repair_text and repair_tool_calls:
                repair_text = json.dumps(repair_tool_calls, ensure_ascii=True)
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
                "outcome_class": "provider_error",
                "semantic_conflict": False,
                "conflict": None,
                "pivot": None,
                "repair_available": False,
                "repair_context": None,
                "attempts": attempts,
            }

        repaired_result, repair_memory_pairs = parse_tool_calls_to_model_output(repair_tool_calls, raw_response=repair_text)
        repaired = repaired_result.output
        repair_reject_reason: str | None = None
        if repaired is None:
            repair_reject_reason = "no_tool_calls"
        elif violates_scambait_style_policy(repaired.suggestion):
            repair_reject_reason = "style_policy_violation"
            repaired = None
        else:
            for k, v in repair_memory_pairs:
                self.store.set_memory_kv(chat_id, k, v)
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

        contract_issues: list[dict[str, Any]] = []
        if repaired is None:
            contract_issues = [item.as_dict() for item in repaired_result.issues]
        first_issue = contract_issues[0] if contract_issues else None
        first_issue_str = ""
        if isinstance(first_issue, dict):
            issue_path = str(first_issue.get("path") or "").strip()
            issue_reason = str(first_issue.get("reason") or "").strip()
            if issue_path or issue_reason:
                first_issue_str = f" ({issue_path}: {issue_reason})"

        semantic_conflict, conflict_payload = self._detect_semantic_conflict(parsed=repaired, result_text=repair_text)
        pivot_payload: dict[str, Any] | None = None
        if semantic_conflict:
            try:
                pivot_payload = self._build_semantic_pivot(chat_id=chat_id, conflict=conflict_payload)
            except Exception as exc:
                pivot_payload = {"error": str(exc)}
            attempts[-1]["accepted"] = False
            attempts[-1]["reject_reason"] = "semantic_conflict"
            attempts[-1]["status"] = "invalid"

        if semantic_conflict:
            outcome_class = "semantic_conflict"
        elif repaired is None and repair_reject_reason == "style_policy_violation":
            outcome_class = "style_violation"
        elif repaired is None:
            outcome_class = "contract_invalid"
        else:
            outcome_class = "ok"

        return {
            "provider": "huggingface_openai_compat",
            "model": model,
            "prompt_json": repair_prompt,
            "response_json": repair_response,
            "result_text": repair_text,
            "valid_output": repaired is not None,
            "parsed_output": {
                "analysis": repaired.analysis,
                "message": {"text": repaired.suggestion},
                "actions": repaired.actions,
                "metadata": repaired.metadata,
                "conflict": repaired.conflict,
            }
            if repaired is not None
            else None,
            "contract_issues": contract_issues,
            "outcome_class": outcome_class,
            "semantic_conflict": semantic_conflict,
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "repair_available": repaired is None,
            "repair_context": (
                {
                    "chat_id": chat_id,
                    "reject_reason": repair_reject_reason or "no_tool_calls",
                    "failed_generation_excerpt": repair_text[:2000],
                }
                if repaired is None
                else None
            ),
            "error_message": (
                None
                if repaired is not None and not semantic_conflict
                else (
                    "semantic conflict detected (operator decision required)"
                    if semantic_conflict
                    else (
                        "model output violates scambait style policy"
                        if repair_reject_reason == "style_policy_violation"
                        else "no tool calls in repair output"
                        + first_issue_str
                    )
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
