"""Prompt card display and memory rendering — pure view/logic functions."""

from __future__ import annotations

import json
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from scambaiter.bot_state import _prompt_card_contexts


PROMPT_SECTION_LABELS: dict[str, str] = {
    "messages": "messages",
    "memory": "memory",
    "system": "system",
}


def _placeholder_for_event_type(event_type: str | None) -> str:
    label = str(event_type or "event").strip().lower()
    if not label:
        label = "event"
    return f"[{label}]"


def _trim_block(text: str, max_len: int = 3800) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _render_prompt_card_text(chat_id: int, prompt_events: list[dict[str, Any]], max_lines: int = 24) -> str:
    lines = [f"Prompt Card", f"chat_id: /{chat_id}", f"events_in_prompt: {len(prompt_events)}", "---"]
    tail = prompt_events[-max_lines:]
    for item in tail:
        role = str(item.get("role", "unknown"))
        time = str(item.get("time") or "--:--")
        event_type = str(item.get("event_type") or "message")
        raw_text = item.get("text")
        compact = ""
        if isinstance(raw_text, str) and raw_text.strip():
            compact = " ".join(raw_text.split())
        if not compact:
            compact = _placeholder_for_event_type(event_type)
        if len(compact) > 220:
            compact = compact[:217] + "..."
        lines.append(f"{time} {role}: {compact}")
    return "\n".join(lines)


def _load_latest_reply_payload(store: Any, chat_id: int) -> tuple[dict[str, Any] | None, str, int | None, str | None]:
    attempts = store.list_generation_attempts(chat_id=chat_id, limit=20)
    if not attempts:
        return None, "", None, None
    attempt = attempts[0]
    raw_text = str(attempt.result_text or "")
    payload: dict[str, Any] | None = None
    try:
        loaded = json.loads(raw_text)
        if isinstance(loaded, dict):
            payload = loaded
    except Exception:
        payload = None
    return payload, raw_text, int(attempt.id), str(attempt.status)


def _set_prompt_card_context(
    application: Application,
    message_id: int,
    chat_id: int,
    attempt_id: int | None,
) -> None:
    contexts = _prompt_card_contexts(application)
    if isinstance(attempt_id, int):
        contexts[int(message_id)] = {"chat_id": int(chat_id), "attempt_id": int(attempt_id)}
    else:
        contexts.pop(int(message_id), None)


def _matches_prompt_card_context(application: Application, message_id: int, chat_id: int, attempt_id: int) -> bool:
    contexts = _prompt_card_contexts(application)
    payload = contexts.get(int(message_id))
    if not isinstance(payload, dict):
        return False
    return int(payload.get("chat_id", -1)) == int(chat_id) and int(payload.get("attempt_id", -1)) == int(attempt_id)


def _prompt_keyboard(
    chat_id: int,
    active_section: str = "messages",
) -> InlineKeyboardMarkup:
    def _btn(code: str) -> InlineKeyboardButton:
        label = PROMPT_SECTION_LABELS.get(code, code)
        if code == active_section:
            label = f"• {label}"
        return InlineKeyboardButton(label, callback_data=f"sc:psec:{code}:{chat_id}")

    return InlineKeyboardMarkup(
        [
            [_btn("messages"), _btn("memory"), _btn("system")],
            [
                InlineKeyboardButton("Dry Run", callback_data=f"sc:dryrun:{chat_id}"),
                InlineKeyboardButton("Close", callback_data=f"sc:prompt_close:{chat_id}"),
            ],
        ]
    )


def _send_confirm_keyboard(chat_id: int, attempt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm Send", callback_data=f"sc:send_confirm:{chat_id}:{attempt_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"sc:send_cancel:{chat_id}:{attempt_id}"),
            ]
        ]
    )


def _send_result_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Cancel/Delete Last", callback_data=f"sc:undo_send:{chat_id}")],
            [InlineKeyboardButton("Delete", callback_data=f"sc:reply_delete:{chat_id}")],
        ]
    )


def _memory_summary_prompt_lines(memory: dict[str, Any] | None) -> list[str]:
    if not isinstance(memory, dict):
        return []
    lines: list[str] = []
    intent = memory.get("current_intent", {})
    facts = memory.get("key_facts") or {}
    risks = memory.get("risk_flags") or []
    latest_topic = intent.get("latest_topic")
    if latest_topic:
        lines.append(f"memory topic: {latest_topic}")
    current_phase = memory.get("narrative", {}).get("phase")
    if current_phase:
        lines.append(f"memory phase: {current_phase}")
    if facts:
        lines.append(f"memory facts: {', '.join(str(k) for k in facts.keys())}")
    if risks:
        lines.append(f"memory risks: {', '.join(str(r) for r in risks)}")
    return lines


def _normalize_memory_payload(memory: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    meta: dict[str, Any] = {
        "state": "missing",
        "cursor_event_id": None,
        "model": None,
        "last_updated_at": None,
    }
    if memory is None:
        return {}, meta
    if isinstance(memory, dict):
        meta["state"] = "ok"
        return memory, meta

    summary = getattr(memory, "summary", None)
    if isinstance(summary, dict):
        meta["state"] = "ok"
        cursor = getattr(memory, "cursor_event_id", None)
        model = getattr(memory, "model", None)
        updated = getattr(memory, "last_updated_at", None)
        if isinstance(cursor, int):
            meta["cursor_event_id"] = cursor
        if isinstance(model, str):
            meta["model"] = model
        if isinstance(updated, str):
            meta["last_updated_at"] = updated
        return summary, meta

    meta["state"] = "invalid"
    meta["reason"] = f"unsupported memory type: {type(memory).__name__}"
    return {}, meta


def _render_memory_compact(summary: dict[str, Any], meta: dict[str, Any], *, chat_id: int, events_in_prompt: int) -> str:
    state = str(meta.get("state") or "missing")
    lines = [
        "Model Input Section: memory",
        f"chat_id: /{chat_id}",
        f"events_in_prompt: {events_in_prompt}",
        f"state: {state}",
    ]
    cursor_event_id = meta.get("cursor_event_id")
    if isinstance(cursor_event_id, int):
        lines.append(f"cursor_event_id: {cursor_event_id}")
    model = meta.get("model")
    if isinstance(model, str) and model.strip():
        lines.append(f"model: {model.strip()}")
    last_updated = meta.get("last_updated_at")
    if isinstance(last_updated, str) and last_updated.strip():
        lines.append(f"last_updated_at: {last_updated.strip()}")
    lines.append("---")

    if state == "missing":
        lines.append("memory unavailable (not built yet)")
        return _trim_block("\n".join(lines))
    if state == "invalid":
        reason = str(meta.get("reason") or "unknown")
        lines.append(f"memory unavailable ({reason})")
        return _trim_block("\n".join(lines))

    claimed = summary.get("claimed_identity") if isinstance(summary.get("claimed_identity"), dict) else {}
    current_intent = summary.get("current_intent") if isinstance(summary.get("current_intent"), dict) else {}
    narrative = summary.get("narrative") if isinstance(summary.get("narrative"), dict) else {}
    key_facts = summary.get("key_facts") if isinstance(summary.get("key_facts"), dict) else {}
    risk_flags = summary.get("risk_flags") if isinstance(summary.get("risk_flags"), list) else []
    open_questions = summary.get("open_questions") if isinstance(summary.get("open_questions"), list) else []
    next_focus = summary.get("next_focus") if isinstance(summary.get("next_focus"), list) else []

    identity_name = str(claimed.get("name") or "").strip() or "-"
    identity_role = str(claimed.get("role_claim") or "").strip() or "-"
    identity_conf = str(claimed.get("confidence") or "").strip() or "-"
    lines.append(f"claimed_identity: {identity_name} ({identity_role}, confidence={identity_conf})")

    scammer_intent = str(current_intent.get("scammer_intent") or "").strip() or "-"
    baiter_intent = str(current_intent.get("baiter_intent") or "").strip() or "-"
    latest_topic = str(current_intent.get("latest_topic") or "").strip() or "-"
    lines.append(f"current_intent.scammer: {scammer_intent}")
    lines.append(f"current_intent.baiter: {baiter_intent}")
    lines.append(f"current_intent.topic: {latest_topic}")

    phase = str(narrative.get("phase") or "").strip() or "-"
    short_story = str(narrative.get("short_story") or "").strip()
    if short_story and len(short_story) > 220:
        short_story = short_story[:217] + "..."
    lines.append(f"narrative.phase: {phase}")
    if short_story:
        lines.append(f"narrative.story: {short_story}")

    fact_keys = [str(key) for key in key_facts.keys()][:8]
    lines.append(f"key_facts.keys: {', '.join(fact_keys) if fact_keys else '-'}")

    if risk_flags:
        compact_risks = ", ".join(str(item) for item in risk_flags[:5])
        lines.append(f"risk_flags: {compact_risks}")
    else:
        lines.append("risk_flags: -")

    if open_questions:
        lines.append("open_questions:")
        for item in open_questions[:5]:
            lines.append(f"- {item}")
    else:
        lines.append("open_questions: -")

    if next_focus:
        lines.append("next_focus:")
        for item in next_focus[:5]:
            lines.append(f"- {item}")
    else:
        lines.append("next_focus: -")

    return _trim_block("\n".join(lines))


def _parse_prompt_event_content(content: str | None) -> tuple[str | None, str | None, str | None, str | None, bool]:
    if not isinstance(content, str):
        return None, None, None, None
    trimmed = content.strip()
    if not trimmed:
        return None, None, None, None
    normalized_trimmed = " ".join(trimmed.split())
    try:
        payload = json.loads(trimmed)
    except Exception:
        return normalized_trimmed, None, None, None, False
    if not isinstance(payload, dict):
        return normalized_trimmed, None, None, None, False
    text = payload.get("text")
    normalized_text: str | None = None
    if isinstance(text, str) and text.strip():
        normalized_text = " ".join(text.split())
    event_type = payload.get("event_type")
    if isinstance(event_type, str):
        event_type = event_type.strip()
        if not event_type:
            event_type = None
    else:
        event_type = None
    payload_time = payload.get("time")
    if isinstance(payload_time, str):
        payload_time = payload_time.strip()
        if not payload_time:
            payload_time = None
    else:
        payload_time = None
    role = payload.get("role")
    if isinstance(role, str):
        role = role.strip()
        if not role:
            role = None
    else:
        role = None
    return normalized_text, event_type, payload_time, role, True


def _extract_recent_messages(model_messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in model_messages:
        if not isinstance(item, dict):
            continue
        candidate_role = str(item.get("role") or "user")
        if candidate_role.strip().lower() == "system":
            continue
        row: dict[str, str] = {"role": candidate_role or "user"}
        raw_time = item.get("time")
        if isinstance(raw_time, str) and raw_time.strip():
            row["time"] = raw_time.strip()
        content = item.get("content")
        parsed_text, parsed_event_type, parsed_time, parsed_role, parsed_as_json = _parse_prompt_event_content(content)
        if parsed_role:
            row["role"] = parsed_role
        if parsed_time:
            row["time"] = parsed_time
        if parsed_text:
            row["content"] = parsed_text
        else:
            if parsed_as_json:
                row["content"] = _placeholder_for_event_type(parsed_event_type)
            elif isinstance(content, str):
                trimmed = content.strip()
                if trimmed:
                    row["content"] = trimmed
                else:
                    row["content"] = _placeholder_for_event_type(parsed_event_type)
            else:
                row["content"] = _placeholder_for_event_type(parsed_event_type)
        out.append(row)
    return out


def _extract_system_prompt(model_messages: list[dict[str, Any]]) -> str:
    for item in model_messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role != "system":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _render_messages_chat_window(
    recent_messages: list[dict[str, str]],
    *,
    max_items: int = 20,
    max_chars: int = 160,
) -> list[str]:
    def _role_name(raw_role: str) -> str:
        role = raw_role.strip().lower()
        if role == "assistant":
            return "A"
        if role == "scambaiter":
            return "B"
        if role == "scammer":
            return "S"
        if role == "system":
            return "S"
        if role == "user":
            return "U"
        return role[:1].upper() if role else "U"

    def _clean_and_truncate(text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 3].rstrip() + "..."

    visible = recent_messages[-max_items:] if len(recent_messages) > max_items else list(recent_messages)
    lines: list[str] = []
    for row in visible:
        role = _role_name(str(row.get("role") or "user"))
        text = _clean_and_truncate(str(row.get("content") or ""))
        raw_time = str(row.get("time") or "").strip()
        if raw_time:
            lines.append(f"{raw_time} {role}: {text}")
        else:
            lines.append(f"{role}: {text}")
    return lines


def _render_prompt_section_text(
    *,
    chat_id: int,
    prompt_events: list[dict[str, Any]],
    model_messages: list[dict[str, Any]],
    latest_payload: dict[str, Any] | None,
    latest_raw: str,
    latest_attempt_id: int | None,
    latest_status: str | None,
    section: str,
    memory: dict[str, Any] | None = None,
) -> str:
    if section == "messages":
        recent_messages = _extract_recent_messages(model_messages)
        message_lines = _render_messages_chat_window(recent_messages, max_items=20, max_chars=160)
        memory_summary, memory_meta = _normalize_memory_payload(memory)
        has_memory = bool(memory_summary) and str(memory_meta.get("state") or "") == "ok"
        lines = [
            "Model Input Section: messages",
            f"chat_id: /{chat_id}",
            f"events_in_prompt: {len(prompt_events)}",
            f"recent_messages_count: {len(recent_messages)}",
            f"showing_recent_messages: {len(message_lines)}",
        ]
        if has_memory:
            lines.append("[...] earlier context summarized in memory")
        lines.extend(["---", "```"])
        lines.extend(message_lines if message_lines else ["(no recent messages)"])
        lines.append("```")
        return _trim_block("\n".join(lines))

    if section == "system":
        system_prompt = _extract_system_prompt(model_messages)
        lines = [
            "Model Input Section: system",
            f"chat_id: /{chat_id}",
            "---",
            system_prompt or "system prompt unavailable",
        ]
        return _trim_block("\n".join(lines))

    summary, meta = _normalize_memory_payload(memory)
    return _render_memory_compact(summary, meta, chat_id=chat_id, events_in_prompt=len(prompt_events))
