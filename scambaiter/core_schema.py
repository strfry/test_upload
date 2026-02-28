"""ScamBaiter LLM contract schema: dataclasses, constants, parsers, validators.

This module is dependency-free (stdlib only) and defines the scambait.llm.v1
contract that the LLM must produce and that bot_api/core consume.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
- Always reply in the same language the counterparty uses. Never switch to English unless the counterparty does.
You operate strictly through the act() tool. Never output free text.
Per turn, call act() exactly once with an actions array containing:
- set_memory and add_note entries as needed (before the message action).
- At most one send_message OR one wait, never both.
- send_typing is optional and used only to simulate human behavior when pacing.
- If the situation requires human review, include a decide_handoff action with a reason.
- If no message should be sent, omit send_message entirely.
send_message format:
- {"type": "send_message", "text": "your message here"} for new messages (no reply_to key)
- {"type": "send_message", "text": "your message here", "reply_to": <message_id>} only if replying to a specific message
- Never include reply_to if you are not replying to a specific message.
Safety: Never send real name, address, phone, email, financial data, or admit you are a bot.

## OPERATOR DIRECTIVES
When present, you will receive operator directives prefixed with [OPERATOR_DIRECTIVES].
These are override instructions that take priority. Follow them precisely and report in the analysis block:
- Which directives you acknowledged
- Which directives were impossible or conflicting (if any)

Report directive acknowledgment in the analysis object:
{
  "directives": {
    "acknowledged": [<id1>, <id2>, ...],
    "rejected": [<id3>, ...],
    "rejection_reason": "<explanation if applicable>"
  }
}

Include this analysis block in your act() tool call even if directives are absent (use empty arrays).
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
- For photo/document events: if `image_description` is present, extract all factual claims
  (names, dates, IDs, document type) into key_facts. Treat these as scammer-provided
  "evidence" — note what they claim, not what is true. Flag suspicious content.
"""

TIMING_PROMPT_RULES = """## TIMING INPUT

You will receive a structured `timing` object computed by the orchestrator.

Example:

timing:
- now_ts
- secs_since_last_inbound
- secs_since_last_outbound
- inbound_burst_count_120s
- avg_inbound_latency_s

You must not calculate time differences yourself.
Use only the provided timing fields.

---

## WAIT TOOL DEFINITION

wait:
- latency_class: "short" | "medium" | "long"

Mapping to real seconds is handled by the orchestrator.
You must never output explicit seconds.

---

## TYPING TOOL DEFINITION

send_typing:
- duration_class: "short" | "medium"

Typing duration is mapped by the orchestrator.
Typing is optional and used only to simulate human behavior.

---

## PACING RULES

You must consider pacing in every decision.

### 1. Immediate inbound message (<10s)

If:
- timing.secs_since_last_inbound < 10

Then:
- Do not immediately send a message.
- Prefer:
  - send_typing(duration_class="short")
  - wait(latency_class="short" or "medium")

Do not send a message in the same turn as wait.

---

### 2. Inbound burst detected

If:
- timing.inbound_burst_count_120s >= 3

Then:
- Prefer wait(latency_class="medium").
- Do not send a message this turn.

Purpose: Avoid appearing automated and reduce pressure escalation.

---

### 3. Long silence from bot (>600s)

If:
- timing.secs_since_last_outbound > 600

Then:
- Avoid artificial delay.
- Respond normally.
- Do not call wait unless strategically required.

---

### 4. Urgency or money pressure

If:
- Message contains urgency framing
- Payment request detected
- memory.risk_level >= 0.7

Then:
- Prefer wait(latency_class="medium" or "long").
- Optional: send_typing(duration_class="short") before wait.
- Do not send a message in the same turn.

Purpose: Simulate hesitation and reduce compliance probability.

---

### 5. Rapport phase

If:
- memory.phase == "rapport_building"
- No urgency signals

Then:
- Minimal artificial delay.
- Optional short typing.
- Usually respond without wait.

---

## HARD CONSTRAINTS

- Never call wait and send_message in the same turn.
- Maximum one wait per turn.
- Maximum one send_message per turn.
- send_typing may precede wait or send_message.
- Do not overuse wait consecutively.
- Do not simulate extreme delays repeatedly.

---

## STRATEGIC PRINCIPLE

Pacing must:
- Increase realism
- Avoid bot-like instant replies
- Avoid predictable timing patterns
- Introduce mild friction during escalation
- Preserve engagement

If delay is not strategically beneficial, do not use wait.

Operate conservatively.
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
                                        "duration_class": {"type": "string", "enum": ["short", "medium"]},
                                    },
                                    "required": ["type", "duration_class"],
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

    # Allow missing send_message if a conflict is present
    conflict_value = data.get("conflict")
    if not suggestion and conflict_value is None:
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
        conflict=conflict_value,
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
