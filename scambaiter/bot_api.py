from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
from datetime import datetime, timezone
from typing import Any

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from scambaiter.core import parse_structured_model_output
from scambaiter.forward_meta import baiter_name_from_meta, scammer_name_from_meta


def _resolve_store(service: Any) -> Any:
    store = getattr(service, "store", None)
    if store is None:
        raise RuntimeError("service.store is required for bot api")
    return store


def _active_targets(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("active_target_chat_by_control_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["active_target_chat_by_control_chat"] = state
    return state


def _auto_targets(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("auto_target_chat_by_control_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["auto_target_chat_by_control_chat"] = state
    return state


def _pending_forwards(application: Application) -> dict[int, list[dict[str, Any]]]:
    state = application.bot_data.setdefault("pending_forwards_by_control_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["pending_forwards_by_control_chat"] = state
    return state


def _forward_card_messages(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("forward_card_message_by_control_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["forward_card_message_by_control_chat"] = state
    return state


def _forward_card_targets(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("forward_card_target_by_control_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["forward_card_target_by_control_chat"] = state
    return state


def _sent_control_messages(application: Application) -> dict[int, list[int]]:
    state = application.bot_data.setdefault("sent_control_messages_by_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["sent_control_messages_by_chat"] = state
    return state


def _last_status_message(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("last_status_message_by_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["last_status_message_by_chat"] = state
    return state


def _last_user_card_message(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("last_user_card_message_by_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["last_user_card_message_by_chat"] = state
    return state


def _user_card_tasks(application: Application) -> dict[int, asyncio.Task[Any]]:
    state = application.bot_data.setdefault("user_card_task_by_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["user_card_task_by_chat"] = state
    return state


def _prompt_card_contexts(application: Application) -> dict[int, dict[str, int]]:
    state = application.bot_data.setdefault("prompt_card_context_by_message", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["prompt_card_context_by_message"] = state
    return state


def _reply_card_states(application: Application) -> dict[int, dict[str, Any]]:
    state = application.bot_data.setdefault("reply_card_state_by_message", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["reply_card_state_by_message"] = state
    return state


def _next_reply_run_id(application: Application) -> int:
    raw = application.bot_data.get("reply_run_counter")
    try:
        current = int(raw)
    except Exception:
        current = 0
    current += 1
    application.bot_data["reply_run_counter"] = current
    return current


def _set_reply_card_state(
    application: Application,
    message_id: int,
    *,
    chat_id: int,
    provider: str,
    model: str,
    parsed_output: dict[str, Any] | None,
    result_text: str,
    retry_context: dict[str, Any] | None,
    run_id: int | None = None,
    status: str | None = None,
    outcome_class: str | None = None,
    error_message: str | None = None,
    contract_issues: list[dict[str, Any]] | None = None,
    response_json: dict[str, Any] | None = None,
    conflict: dict[str, Any] | None = None,
    pivot: dict[str, Any] | None = None,
    active_section: str = "message",
) -> None:
    _reply_card_states(application)[int(message_id)] = {
        "chat_id": int(chat_id),
        "provider": str(provider or "unknown"),
        "model": str(model or "unknown"),
        "parsed_output": parsed_output if isinstance(parsed_output, dict) else None,
        "result_text": str(result_text or ""),
        "retry_context": retry_context if isinstance(retry_context, dict) else None,
        "run_id": int(run_id) if isinstance(run_id, int) else None,
        "status": str(status or "").strip() or "unknown",
        "outcome_class": str(outcome_class or "").strip() or "unknown",
        "error_message": str(error_message or "").strip() or "",
        "contract_issues": [item for item in (contract_issues or []) if isinstance(item, dict)],
        "response_json": response_json if isinstance(response_json, dict) else {},
        "conflict": conflict if isinstance(conflict, dict) else None,
        "pivot": pivot if isinstance(pivot, dict) else None,
        "active_section": str(active_section or "message"),
    }


def _get_reply_card_state(application: Application, message_id: int) -> dict[str, Any] | None:
    payload = _reply_card_states(application).get(int(message_id))
    return payload if isinstance(payload, dict) else None


def _drop_reply_card_state(application: Application, message_id: int) -> None:
    _reply_card_states(application).pop(int(message_id), None)


RESULT_SECTION_LABELS: dict[str, str] = {
    "message": "message",
    "actions": "actions",
    "analysis": "analysis",
    "error": "error",
    "response": "response",
    "raw": "raw",
}


def _truncate_for_card(text: str, max_len: int = 2200) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _render_result_section_message(state: dict[str, Any]) -> str:
    parsed_output = state.get("parsed_output")
    message_text = _extract_action_message_text(parsed_output if isinstance(parsed_output, dict) else None)
    if not message_text and isinstance(parsed_output, dict):
        message_obj = parsed_output.get("message")
        if isinstance(message_obj, dict):
            raw = message_obj.get("text")
            if isinstance(raw, str):
                message_text = raw.strip()
    if not message_text:
        message_text = _extract_partial_message_preview(str(state.get("result_text") or ""))
    return message_text or "(empty)"


def _render_result_section_actions(state: dict[str, Any]) -> str:
    parsed_output = state.get("parsed_output")
    actions = parsed_output.get("actions") if isinstance(parsed_output, dict) and isinstance(parsed_output.get("actions"), list) else []
    if not actions:
        return "(none)"
    lines: list[str] = []
    for idx, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            lines.append(f"{idx}. <invalid>")
            continue
        action_type = str(action.get("type") or "unknown")
        compact = json.dumps(action, ensure_ascii=True, separators=(",", ":"))
        if len(compact) > 200:
            compact = compact[:197] + "..."
        lines.append(f"{idx}. {action_type}  {compact}")
    return "\n".join(lines)


def _render_result_section_analysis(state: dict[str, Any]) -> str:
    parsed_output = state.get("parsed_output")
    analysis = parsed_output.get("analysis") if isinstance(parsed_output, dict) else None
    if not isinstance(analysis, dict) or not analysis:
        return "(none)"
    raw = json.dumps(analysis, ensure_ascii=True, indent=2)
    return _truncate_for_card(raw, max_len=2400)


def _render_result_section_error(state: dict[str, Any]) -> str:
    lines = [
        f"class: {state.get('outcome_class') or 'unknown'}",
        f"status: {state.get('status') or 'unknown'}",
    ]
    error_message = str(state.get("error_message") or "").strip()
    if error_message:
        lines.extend(["", "error", error_message])
    result_text = str(state.get("result_text") or "").strip()
    if result_text:
        snippet = result_text if len(result_text) <= 300 else result_text[:297] + "..."
        lines.extend(["", "result_text", snippet])
    contract_issues = state.get("contract_issues")
    if isinstance(contract_issues, list) and contract_issues:
        lines.extend(["", "contract issues"])
        for item in contract_issues[:8]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "unknown")
            reason = str(item.get("reason") or "unknown")
            lines.append(f"- {path}: {reason}")
    response_json = state.get("response_json")
    if isinstance(response_json, dict) and response_json:
        debug_meta = _extract_response_debug_meta(response_json)
        lines.extend(
            [
                "",
                "response debug",
                f"- finish_reason: {debug_meta.get('finish_reason', 'unknown')}",
                f"- content_type: {debug_meta.get('content_type', 'unknown')}",
                f"- message_keys: {debug_meta.get('message_keys', '-')}",
            ]
        )
    conflict = state.get("conflict")
    if isinstance(conflict, dict):
        lines.extend(["", "conflict"])
        lines.append(f"- code: {conflict.get('code') or 'unknown'}")
        reason = conflict.get("reason")
        if isinstance(reason, str) and reason.strip():
            lines.append(f"- reason: {reason.strip()}")
    pivot = state.get("pivot")
    if isinstance(pivot, dict):
        recommended = pivot.get("recommended_text")
        if isinstance(recommended, str) and recommended.strip():
            lines.extend(
                [
                    "",
                    "recommended pivot",
                    recommended.strip(),
                ]
            )
    return "\n".join(lines)


def _render_result_section_response(state: dict[str, Any]) -> str:
    result_text = str(state.get("result_text") or "").strip()
    if not result_text:
        response_json = state.get("response_json")
        if not isinstance(response_json, dict) or not response_json:
            return "(no raw output available)"
        result_text = _extract_textual_response_fallback(response_json)
        if not result_text:
            result_text = _compact_response_excerpt(response_json, max_len=2000)
    snippet = _format_raw_result_snippet(result_text)
    error_note = _describe_parsing_error(state)
    lines = ["Raw model output:", "```json", snippet, "```"]
    if error_note:
        lines.extend(["", error_note])
    return "\n".join(lines)


def _format_raw_result_snippet(raw: str, max_chars: int = 600) -> str:
    cleaned = raw.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


def _extract_error_note_from_contracts(issues: object) -> str | None:
    if not isinstance(issues, list):
        return None
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        path = str(issue.get("path") or "").lower()
        reason = str(issue.get("reason") or "").strip()
        if path == "root" and "invalid json" in reason.lower():
            return reason
    return None


def _describe_parsing_error(state: dict[str, Any]) -> str | None:
    issues = state.get("contract_issues")
    reason = _extract_error_note_from_contracts(issues)
    if not reason:
        if isinstance(issues, list):
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                r = str(issue.get("reason") or "").strip()
                if r:
                    reason = r
                    break
    if not reason:
        return None
    result_text = str(state.get("result_text") or "")
    if not result_text:
        return f"parse error: {reason}"
    cursor = _find_error_context(result_text)
    return f"parse error near line {cursor[0]}, col {cursor[1]}: {reason}"


def _find_error_context(text: str) -> tuple[int, int]:
    snippet = text.strip()
    if not snippet:
        return 0, 0
    idx = snippet.find("{")
    if idx < 0:
        idx = 0
    line = snippet[:idx].count("\n") + 1
    last_newline = snippet[:idx].rfind("\n")
    col = idx - (last_newline + 1) + 1
    return line, col


def _raw_model_output_text(state: dict[str, Any]) -> str:
    text = str(state.get("result_text") or "").strip()
    if text:
        return text
    response_json = state.get("response_json")
    if isinstance(response_json, dict) and response_json:
        extracted = _extract_textual_response_fallback(response_json)
        if extracted:
            return extracted
        return json.dumps(response_json, ensure_ascii=True, indent=2)
    return ""


def _render_result_section_raw(state: dict[str, Any]) -> str:
    response_json = state.get("response_json")
    has_raw = isinstance(response_json, dict) and bool(response_json)
    if has_raw:
        return "Raw output export available.\nUse 'Send raw file' to download the full JSON."
    return "Raw output unavailable."


def _render_result_card_text(state: dict[str, Any], section: str) -> str:
    chat_id = int(state.get("chat_id") or -1)
    run_id = state.get("run_id")
    provider = str(state.get("provider") or "unknown")
    model = str(state.get("model") or "unknown")
    status = str(state.get("status") or "unknown")
    outcome_class = str(state.get("outcome_class") or "unknown")
    lines = [
        "Result Card",
        f"chat_id: /{chat_id}",
        f"run_id: {run_id if isinstance(run_id, int) else '-'}",
        f"status: {status}",
        f"outcome_class: {outcome_class}",
        f"provider: {provider}",
        f"model: {model}",
        f"section: {section}",
        "---",
    ]
    if section == "message":
        body = _render_result_section_message(state)
    elif section == "actions":
        body = _render_result_section_actions(state)
    elif section == "analysis":
        body = _render_result_section_analysis(state)
    elif section == "error":
        body = _render_result_section_error(state)
    elif section == "response":
        body = _render_result_section_response(state)
    else:
        body = _render_result_section_raw(state)
    lines.append(body)
    return _trim_block("\n".join(lines))


def _last_sent_by_chat(application: Application) -> dict[int, dict[str, int]]:
    state = application.bot_data.setdefault("last_sent_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["last_sent_by_target_chat"] = state
    return state


def _render_html_copy_block(text: str) -> str:
    raw = text or ""
    if len(raw) > 3200:
        raw = raw[:3197] + "..."
    return "Message text (copy)\n" + raw


def _classify_dry_run_error(error_message: str) -> tuple[str, str]:
    normalized = (error_message or "").strip().lower()
    if "hf_token/hf_model missing" in normalized:
        return (
            "Missing provider configuration",
            "Set HF_TOKEN and HF_MODEL in secrets.sh, then restart the bot.",
        )
    if "openai package missing" in normalized:
        return (
            "Dependency missing",
            "Install the openai package in the project venv and restart.",
        )
    if "invalid model output contract" in normalized:
        return (
            "Model output contract violation",
            "Model response did not match scambait.llm.v1. Inspect result excerpt and adjust prompt/model.",
        )
    if "sqlite objects created in a thread" in normalized:
        return (
            "Thread/DB mismatch",
            "Dry run touched SQLite from a different thread. Keep DB work in the main thread.",
        )
    return (
        "Dry run execution error",
        "Check provider/model connectivity and inspect the stored attempt payload.",
    )


def _extract_response_debug_meta(response_json: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(response_json, dict):
        return {}
    finish_reason = "unknown"
    content_type = "missing"
    message_keys = "-"
    choices = response_json.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            finish_value = first.get("finish_reason")
            if isinstance(finish_value, str) and finish_value.strip():
                finish_reason = finish_value.strip()
            message = first.get("message")
            if isinstance(message, dict):
                message_keys = ",".join(sorted(str(key) for key in message.keys())) or "-"
                content = message.get("content")
                if content is None:
                    content_type = "none"
                elif isinstance(content, str):
                    content_type = "string"
                elif isinstance(content, list):
                    content_type = "list"
                else:
                    content_type = type(content).__name__
            else:
                message_keys = "(non-dict)"
                content_type = "(no-message)"
    return {
        "finish_reason": finish_reason,
        "content_type": content_type,
        "message_keys": message_keys,
    }


def _extract_textual_response_fallback(response_json: dict[str, Any] | None) -> str:
    if not isinstance(response_json, dict):
        return ""
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    parts.append(text_value.strip())
                continue
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts:
            return "\n".join(parts)
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        return refusal.strip()
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first_call = tool_calls[0]
        if isinstance(first_call, dict):
            function_obj = first_call.get("function")
            if isinstance(function_obj, dict):
                arguments = function_obj.get("arguments")
                if isinstance(arguments, str) and arguments.strip():
                    return arguments.strip()
    return ""


def _compact_response_excerpt(response_json: dict[str, Any] | None, max_len: int = 1400) -> str:
    if not isinstance(response_json, dict) or not response_json:
        return ""
    raw = json.dumps(response_json, ensure_ascii=True)
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 3] + "..."


def _dry_run_retry_keyboard(chat_id: int, attempt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Retry", callback_data=f"sc:reply_retry:{chat_id}")],
            [InlineKeyboardButton("Delete", callback_data=f"sc:reply_delete:{chat_id}")],
        ]
    )


def _result_card_keyboard(
    *,
    chat_id: int,
    active_section: str,
    status: str,
    telethon_enabled: bool,
    retry_enabled: bool,
    has_raw: bool,
) -> InlineKeyboardMarkup:
    def _tab(code: str) -> InlineKeyboardButton:
        label = RESULT_SECTION_LABELS.get(code, code)
        if code == active_section:
            label = f"• {label}"
        return InlineKeyboardButton(label, callback_data=f"sc:rsec:{code}:{chat_id}")

    rows: list[list[InlineKeyboardButton]] = [
        [_tab("message"), _tab("actions"), _tab("analysis")],
        [_tab("error"), _tab("response"), _tab("raw")],
    ]
    if has_raw and active_section == "raw":
        rows.append([InlineKeyboardButton("Send raw file", callback_data=f"sc:rawfile:{chat_id}")])

    if status == "ok":
        action_label = "Send" if telethon_enabled else "Mark as Sent"
        action_code = "reply_send" if telethon_enabled else "reply_mark"
        rows.append([InlineKeyboardButton(action_label, callback_data=f"sc:{action_code}:{chat_id}")])
    elif retry_enabled:
        rows.append([InlineKeyboardButton("Retry", callback_data=f"sc:reply_retry:{chat_id}")])
    rows.append([InlineKeyboardButton("Delete", callback_data=f"sc:reply_delete:{chat_id}")])
    return InlineKeyboardMarkup(rows)


def _reply_action_keyboard(chat_id: int, telethon_enabled: bool) -> InlineKeyboardMarkup:
    action_label = "Send" if telethon_enabled else "Mark as Sent"
    action_code = "reply_send" if telethon_enabled else "reply_mark"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(action_label, callback_data=f"sc:{action_code}:{chat_id}")],
            [InlineKeyboardButton("Delete", callback_data=f"sc:reply_delete:{chat_id}")],
        ]
    )


def _reply_error_keyboard(chat_id: int, retry_enabled: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if retry_enabled:
        rows.append([InlineKeyboardButton("Retry", callback_data=f"sc:reply_retry:{chat_id}")])
    rows.append([InlineKeyboardButton("Delete", callback_data=f"sc:reply_delete:{chat_id}")])
    return InlineKeyboardMarkup(rows)


def _extract_action_message_text(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return ""
    for action in actions:
        if not isinstance(action, dict):
            continue
        if str(action.get("type") or "").strip() != "send_message":
            continue
        message_obj = action.get("message")
        if not isinstance(message_obj, dict):
            dotted = action.get("message.text")
            if isinstance(dotted, str) and dotted.strip():
                return dotted.strip()
            continue
        text = message_obj.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _extract_partial_message_preview(result_text: str) -> str:
    raw = (result_text or "").strip()
    if not raw:
        return ""
    try:
        loaded = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(loaded, dict):
        return ""
    text_value = _extract_action_message_text(loaded)
    if not text_value:
        message = loaded.get("message")
        if isinstance(message, dict):
            text = message.get("text")
            if isinstance(text, str) and text.strip():
                text_value = text.strip()
    if text_value:
        compact = " ".join(text_value.split())
        return compact[:800] + ("..." if len(compact) > 800 else "")
    return ""


async def _send_control_text(
    application: Application,
    message: Message,
    text: str,
    *,
    parse_mode: str | None = None,
    replace_previous_status: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    if len(text) > 3500:
        text = text[:3497] + "..."
    chat_id = int(message.chat_id)
    last_status = _last_status_message(application)
    if replace_previous_status:
        previous_id = last_status.get(chat_id)
        if isinstance(previous_id, int):
            try:
                await application.bot.delete_message(chat_id=chat_id, message_id=previous_id)
            except Exception:
                pass
    sent = await message.reply_text(
        text,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    sent_messages = _sent_control_messages(application).setdefault(chat_id, [])
    sent_messages.append(int(sent.message_id))
    if len(sent_messages) > 500:
        del sent_messages[: len(sent_messages) - 500]
    if replace_previous_status:
        last_status[chat_id] = int(sent.message_id)
    return sent


def _render_user_card(
    target_chat_id: int,
    event_count: int,
    last_preview: str | None,
    profile_lines: list[str],
) -> str:
    preview = last_preview or "-"
    profile_block = "\n".join(profile_lines) if profile_lines else "profile: unavailable"
    return (
        "Chat Card\n"
        f"chat_id: /{target_chat_id}\n"
        f"events: {event_count}\n"
        f"{profile_block}\n"
        f"last: {preview}"
    )


def _chat_card_keyboard(target_chat_id: int, live_mode: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Prompt", callback_data=f"sc:prompt:{target_chat_id}")],
    ]
    if live_mode:
        rows.append([
            InlineKeyboardButton("Fetch Profile", callback_data=f"sc:fetch_profile:{target_chat_id}"),
            InlineKeyboardButton("Fetch History", callback_data=f"sc:fetch_history:{target_chat_id}"),
        ])
    rows.append([InlineKeyboardButton("Close", callback_data=f"sc:chat_close:{target_chat_id}")])
    return InlineKeyboardMarkup(rows)


def _truncate_chat_button_label(base: str, chat_id: int, max_len: int = 56) -> str:
    suffix = f" · /{chat_id}"
    compact = " ".join((base or "").split()).strip()
    if not compact:
        compact = "Unknown"
    full = f"{compact}{suffix}"
    if len(full) <= max_len:
        return full
    remaining = max_len - len(suffix)
    if remaining <= 4:
        return f"/{chat_id}"
    return f"{compact[: remaining - 3]}...{suffix}"


def _chat_button_label(store: Any, chat_id: int) -> str:
    display_name: str | None = None
    username: str | None = None
    try:
        profile = store.get_chat_profile(chat_id=chat_id)
    except Exception:
        profile = None
    if profile is not None:
        snapshot = getattr(profile, "snapshot", None)
        if isinstance(snapshot, dict):
            identity = snapshot.get("identity")
            if isinstance(identity, dict):
                candidate_display = identity.get("display_name")
                if isinstance(candidate_display, str) and candidate_display.strip():
                    display_name = candidate_display.strip()
                candidate_username = identity.get("username")
                if isinstance(candidate_username, str) and candidate_username.strip():
                    value = candidate_username.strip()
                    username = value if value.startswith("@") else f"@{value}"
    if display_name:
        base = display_name
        if username:
            base = f"{display_name} ({username})"
    elif username:
        base = username
    else:
        base = "Unknown"
    return _truncate_chat_button_label(base, chat_id)


def _known_chats_keyboard(store: Any, chat_ids: list[int], max_buttons: int = 30) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in chat_ids[:max_buttons]:
        rows.append([InlineKeyboardButton(_chat_button_label(store, item), callback_data=f"sc:selchat:{item}")])
    return InlineKeyboardMarkup(rows)


def _known_chats_card_content(store: Any, chat_ids: list[int]) -> tuple[str, InlineKeyboardMarkup]:
    shown = chat_ids[:30]
    extra = len(chat_ids) - len(shown)
    title = f"Known chat ids ({len(chat_ids)} total):\nSelect one:"
    if extra > 0:
        title += f"\n(showing first {len(shown)}, {extra} hidden)"
    return title, _known_chats_keyboard(store, chat_ids)


def _chat_card_clear_confirm_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm Delete", callback_data=f"sc:clear_history_confirm:{target_chat_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"sc:clear_history_cancel:{target_chat_id}"),
            ]
        ]
    )


def _chat_card_clear_safety_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("I Understand, Continue", callback_data=f"sc:clear_history_arm:{target_chat_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"sc:clear_history_cancel:{target_chat_id}"),
            ]
        ]
    )


def _profile_lines_from_events(events: list[Any]) -> list[str]:
    sender_user: dict[str, Any] = {}
    sender_chat: dict[str, Any] = {}
    sender_user_name: str | None = None

    def _consume_profile(event: Any) -> None:
        nonlocal sender_user, sender_chat, sender_user_name
        meta = getattr(event, "meta", None)
        if not isinstance(meta, dict):
            return
        forward_profile = meta.get("forward_profile")
        if not isinstance(forward_profile, dict):
            return
        if not sender_user:
            candidate = forward_profile.get("sender_user")
            if isinstance(candidate, dict):
                sender_user = candidate
        if not sender_chat:
            candidate = forward_profile.get("sender_chat")
            if isinstance(candidate, dict):
                sender_chat = candidate
        if sender_user_name is None:
            candidate_name = forward_profile.get("sender_user_name")
            if isinstance(candidate_name, str) and candidate_name.strip():
                sender_user_name = candidate_name.strip()

    # Prefer identity from scammer-side events to avoid showing operator profile as chat contact.
    scammer_events = [event for event in events if getattr(event, "role", None) == "scammer"]
    for event in scammer_events:
        _consume_profile(event)
        if sender_user or sender_chat or sender_user_name:
            break
    if not (sender_user or sender_chat or sender_user_name):
        for event in events:
            _consume_profile(event)
            if sender_user or sender_chat or sender_user_name:
                break
    lines: list[str] = []
    display_name = None
    if sender_chat:
        title = sender_chat.get("title")
        if isinstance(title, str) and title.strip():
            display_name = title.strip()
    if display_name is None and sender_user_name:
        display_name = sender_user_name
    if display_name is None and sender_user:
        first = sender_user.get("first_name")
        last = sender_user.get("last_name")
        parts: list[str] = []
        if isinstance(first, str) and first.strip():
            parts.append(first.strip())
        if isinstance(last, str) and last.strip():
            parts.append(last.strip())
        if parts:
            display_name = " ".join(parts)
    lines.append(f"display_name: {display_name or 'unknown'}")
    username = None
    if sender_user:
        user_name = sender_user.get("username")
        if isinstance(user_name, str) and user_name.strip():
            username = "@" + user_name.strip()
    if username is None and sender_chat:
        chat_username = sender_chat.get("username")
        if isinstance(chat_username, str) and chat_username.strip():
            username = "@" + chat_username.strip()
    lines.append(f"username: {username or 'unknown'}")
    lines.append(
        "origin_type: "
        + ("sender_user" if sender_user else "sender_chat" if sender_chat else "unknown")
    )
    lines.append("profile_photos: unknown (not exposed by BotAPI forward metadata)")
    lines.append("bio: unknown (not exposed by BotAPI forward metadata)")
    return lines


def _profile_lines_from_stored_profile(snapshot: dict[str, Any]) -> list[str]:
    identity = snapshot.get("identity") or {}
    first = identity.get("first_name", "")
    last = identity.get("last_name", "")
    display_name = " ".join(p for p in [first, last] if p).strip() or None
    username = identity.get("username")
    bio = identity.get("bio")
    profile_media = snapshot.get("profile_media") or {}
    has_photo = profile_media.get("has_profile_photo")
    return [
        f"display_name: {display_name or 'unknown'}",
        f"username: {'@' + username if username else 'unknown'}",
        f"bio: {bio if bio else 'unknown'}",
        f"profile_photo: {'yes' if has_photo else ('no' if has_photo is False else 'unknown')}",
        "source: telethon",
    ]


async def _show_user_card(
    application: Application,
    control_chat_id: int,
    store: Any,
    target_chat_id: int,
) -> None:
    chat_id = int(control_chat_id)
    last_card = _last_user_card_message(application)
    previous_id = last_card.get(chat_id)
    if isinstance(previous_id, int):
        try:
            await application.bot.delete_message(chat_id=chat_id, message_id=previous_id)
        except Exception:
            pass
    events = store.list_events(chat_id=target_chat_id, limit=200)
    last_preview: str | None = None
    if events:
        last = events[-1]
        text = getattr(last, "text", None)
        if isinstance(text, str) and text.strip():
            compact = " ".join(text.split())
            last_preview = compact[:100] + ("..." if len(compact) > 100 else "")
    stored_profile = store.get_chat_profile(target_chat_id)
    if stored_profile is not None and getattr(stored_profile, "last_source", None) == "telethon":
        profile_lines = _profile_lines_from_stored_profile(stored_profile.snapshot)
    else:
        profile_lines = _profile_lines_from_events(events)
    live_mode = application.bot_data.get("mode") == "live"
    sent = await application.bot.send_message(
        chat_id=chat_id,
        text=_render_user_card(target_chat_id, len(events), last_preview, profile_lines),
        reply_markup=_chat_card_keyboard(target_chat_id, live_mode=live_mode),
    )
    sent_messages = _sent_control_messages(application).setdefault(chat_id, [])
    sent_messages.append(int(sent.message_id))
    if len(sent_messages) > 500:
        del sent_messages[: len(sent_messages) - 500]
    last_card[chat_id] = int(sent.message_id)


def _schedule_user_card_update(
    application: Application,
    control_chat_id: int,
    store: Any,
    target_chat_id: int,
    delay_seconds: float = 1.0,
) -> None:
    tasks = _user_card_tasks(application)
    previous = tasks.get(control_chat_id)
    if previous is not None and not previous.done():
        previous.cancel()

    async def _runner() -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await _show_user_card(
                application=application,
                control_chat_id=control_chat_id,
                store=store,
                target_chat_id=target_chat_id,
            )
        except asyncio.CancelledError:
            return
        finally:
            current = tasks.get(control_chat_id)
            if current is asyncio.current_task():
                tasks.pop(control_chat_id, None)

    tasks[control_chat_id] = asyncio.create_task(_runner())


def _placeholder_for_event_type(event_type: str | None) -> str:
    label = str(event_type or "event").strip().lower()
    if not label:
        label = "event"
    return f"[{label}]"


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


PROMPT_SECTION_LABELS: dict[str, str] = {
    "messages": "messages",
    "memory": "memory",
    "system": "system",
}


def _trim_block(text: str, max_len: int = 3800) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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
        if role in ("assistant", "scammer", "scambaiter"):
            return "A"
        return "U"

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


async def _handle_prompt_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    core = getattr(service, "core", None)
    store = getattr(service, "store", None)
    if core is None or store is None:
        await query.answer("Core unavailable")
        return
    prompt_events = core.build_prompt_events(chat_id=chat_id)
    model_messages = core.build_model_messages(chat_id=chat_id)
    latest_payload, latest_raw, latest_attempt_id, latest_status = _load_latest_reply_payload(store, chat_id)
    memory = store.get_memory_context(chat_id=chat_id)
    prompt_text = _render_prompt_section_text(
        chat_id=chat_id,
        prompt_events=prompt_events,
        model_messages=model_messages,
        latest_payload=latest_payload,
        latest_raw=latest_raw,
        latest_attempt_id=latest_attempt_id,
        latest_status=latest_status,
        section="messages",
        memory=memory,
    )
    sent = await message.reply_text(
        prompt_text,
        reply_markup=_prompt_keyboard(chat_id=chat_id, active_section="messages"),
    )
    _set_prompt_card_context(app, int(sent.message_id), chat_id=chat_id, attempt_id=latest_attempt_id)
    sent_messages = _sent_control_messages(app).setdefault(int(message.chat_id), [])
    sent_messages.append(int(sent.message_id))
    if len(sent_messages) > 500:
        del sent_messages[: len(sent_messages) - 500]
    await query.answer("Prompt generated")


async def _handle_clear_history_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return

    safety_text = (
        "Safety check: destructive action\n"
        "Delete stored chat context?\n"
        f"chat_id: /{chat_id}\n"
        "This permanently deletes events, analyses, directives, attempts and profile data.\n"
        "Please confirm you understand this cannot be undone."
    )
    try:
        await query.edit_message_text(
            safety_text,
            reply_markup=_chat_card_clear_safety_keyboard(chat_id),
        )
    except Exception:
        await message.reply_text(safety_text, reply_markup=_chat_card_clear_safety_keyboard(chat_id))
    await query.answer("Safety check")


async def _handle_clear_history_arm_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return

    confirm_text = (
        "Final confirmation required\n"
        f"chat_id: /{chat_id}\n"
        "Press Confirm Delete to permanently erase this chat context."
    )
    try:
        await query.edit_message_text(
            confirm_text,
            reply_markup=_chat_card_clear_confirm_keyboard(chat_id),
        )
    except Exception:
        await message.reply_text(confirm_text, reply_markup=_chat_card_clear_confirm_keyboard(chat_id))
    await query.answer("Final confirmation")


async def _handle_clear_history_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    deleted = store.clear_chat_context(chat_id=chat_id)
    pending_messages = getattr(service, "_pending_messages", None)
    pending_removed = 0
    if isinstance(pending_messages, dict) and chat_id in pending_messages:
        pending_messages.pop(chat_id, None)
        pending_removed = 1
    await query.answer(f"Deleted context ({deleted.get('total', 0)} rows)")
    summary_lines = [
        f"Deleted context for /{chat_id}",
        f"- events: {deleted.get('events', 0)}",
        f"- analyses: {deleted.get('analyses', 0)}",
        f"- directives: {deleted.get('directives', 0)}",
        f"- attempts: {deleted.get('generation_attempts', 0)}",
        f"- profile changes: {deleted.get('profile_changes', 0)}",
        f"- profile: {deleted.get('chat_profile', 0)}",
        f"- pending runtime message: {pending_removed}",
        f"- total rows: {deleted.get('total', 0)}",
    ]
    # Delete the chat-card/confirm card itself after destructive action.
    await _delete_control_message(message)
    _last_user_card_message(app).pop(int(message.chat_id), None)
    await _send_control_text(
        application=app,
        message=message,
        text="\n".join(summary_lines),
        replace_previous_status=False,
    )


async def _handle_clear_history_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    await query.answer("Cancelled")
    await _show_user_card(
        application=app,
        control_chat_id=int(message.chat_id),
        store=store,
        target_chat_id=chat_id,
    )


async def _handle_prompt_section_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 4:
        await query.answer("Invalid section")
        return
    _, _, section, chat_raw = parts
    if section not in PROMPT_SECTION_LABELS:
        await query.answer("Invalid section")
        return
    try:
        chat_id = int(chat_raw)
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    core = getattr(service, "core", None)
    store = getattr(service, "store", None)
    if core is None or store is None:
        await query.answer("Core unavailable")
        return
    prompt_events = core.build_prompt_events(chat_id=chat_id)
    model_messages = core.build_model_messages(chat_id=chat_id)
    latest_payload, latest_raw, latest_attempt_id, latest_status = _load_latest_reply_payload(store, chat_id)
    memory = store.get_memory_context(chat_id=chat_id)
    prompt_text = _render_prompt_section_text(
        chat_id=chat_id,
        prompt_events=prompt_events,
        model_messages=model_messages,
        latest_payload=latest_payload,
        latest_raw=latest_raw,
        latest_attempt_id=latest_attempt_id,
        latest_status=latest_status,
        section=section,
        memory=memory,
    )
    try:
        await query.edit_message_text(
            prompt_text,
            reply_markup=_prompt_keyboard(chat_id=chat_id, active_section=section),
        )
        _set_prompt_card_context(app, int(message.message_id), chat_id=chat_id, attempt_id=latest_attempt_id)
    except Exception:
        sent = await message.reply_text(
            prompt_text,
            reply_markup=_prompt_keyboard(chat_id=chat_id, active_section=section),
        )
        _set_prompt_card_context(app, int(sent.message_id), chat_id=chat_id, attempt_id=latest_attempt_id)
    await query.answer(f"Section: {PROMPT_SECTION_LABELS.get(section, section)}")


async def _handle_prompt_close_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    await query.answer("Closed")
    await _show_user_card(
        application=app,
        control_chat_id=int(message.chat_id),
        store=store,
        target_chat_id=chat_id,
    )
    await _delete_control_message(message)


async def _handle_chat_close_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    chat_ids = store.list_chat_ids(limit=100)
    await query.answer("Closed")
    await _delete_control_message(message)
    _last_user_card_message(app).pop(int(message.chat_id), None)
    if not chat_ids:
        await _send_control_text(
            application=app,
            message=message,
            text="No chat history stored yet.",
            replace_previous_status=False,
        )
        return
    title, keyboard = _known_chats_card_content(store, chat_ids)
    sent = await app.bot.send_message(
        chat_id=int(message.chat_id),
        text=title,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    sent_messages = _sent_control_messages(app).setdefault(int(message.chat_id), [])
    sent_messages.append(int(sent.message_id))
    if len(sent_messages) > 500:
        del sent_messages[: len(sent_messages) - 500]


def _load_attempt_for_send(store: Any, chat_id: int, attempt_id: int) -> tuple[Any | None, Any | None, str | None]:
    attempt = store.get_generation_attempt(attempt_id)
    if attempt is None:
        return None, None, "Attempt not found."
    if int(attempt.chat_id) != int(chat_id):
        return None, None, "Attempt does not belong to this chat."
    parsed = parse_structured_model_output(str(attempt.result_text or ""))
    if parsed is None:
        return attempt, None, "Attempt payload is invalid and cannot be sent."
    return attempt, parsed, None


async def _handle_send_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Invalid send action")
        return
    try:
        chat_id = int(parts[2])
        attempt_id = int(parts[3])
    except ValueError:
        await query.answer("Invalid ids")
        return
    if not _matches_prompt_card_context(app, int(message.message_id), chat_id=chat_id, attempt_id=attempt_id):
        await query.answer("Card outdated")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    attempt, parsed, error = _load_attempt_for_send(store, chat_id=chat_id, attempt_id=attempt_id)
    if error:
        await query.answer(error)
        return
    assert attempt is not None and parsed is not None
    preview = parsed.suggestion.strip()
    if len(preview) > 700:
        preview = preview[:697] + "..."
    action_labels = ", ".join(str(action.get("type") or "?") for action in parsed.actions if isinstance(action, dict))
    confirm_text = (
        "Confirm send via Telethon?\n"
        f"chat_id: /{chat_id}\n"
        f"attempt_id: {attempt_id}\n"
        f"actions: {action_labels or '(none)'}\n"
        "---\n"
        f"{preview or '(empty message)'}"
    )
    try:
        await query.edit_message_text(confirm_text, reply_markup=_send_confirm_keyboard(chat_id=chat_id, attempt_id=attempt_id))
        _set_prompt_card_context(app, int(message.message_id), chat_id=chat_id, attempt_id=attempt_id)
    except Exception:
        sent = await message.reply_text(confirm_text, reply_markup=_send_confirm_keyboard(chat_id=chat_id, attempt_id=attempt_id))
        _set_prompt_card_context(app, int(sent.message_id), chat_id=chat_id, attempt_id=attempt_id)
    await query.answer("Ready to send")


async def _handle_send_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    await query.answer("Send cancelled")
    try:
        await query.edit_message_text("Send cancelled.")
    except Exception:
        await message.reply_text("Send cancelled.")


async def _handle_send_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Invalid send action")
        return
    try:
        chat_id = int(parts[2])
        attempt_id = int(parts[3])
    except ValueError:
        await query.answer("Invalid ids")
        return
    if not _matches_prompt_card_context(app, int(message.message_id), chat_id=chat_id, attempt_id=attempt_id):
        await query.answer("Card outdated")
        return
    executor = app.bot_data.get("telethon_executor")
    if executor is None:
        await query.answer("Telethon sender unavailable")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    attempt, parsed, error = _load_attempt_for_send(store, chat_id=chat_id, attempt_id=attempt_id)
    if error:
        await query.answer(error)
        return
    assert parsed is not None
    await query.answer("Sending...")
    report = await executor.execute_actions(
        chat_id=chat_id,
        parsed_output={
            "message": {"text": parsed.suggestion},
            "actions": parsed.actions,
        },
    )
    if report.ok:
        lines = [
            f"Sent via Telethon for /{chat_id}",
            f"attempt_id: {attempt_id}",
            f"actions_executed: {len(report.executed_actions)}",
        ]
        if report.sent_message_id is not None:
            lines.append(f"sent_message_id: {report.sent_message_id}")
            _last_sent_by_chat(app)[chat_id] = {
                "message_id": int(report.sent_message_id),
                "attempt_id": int(attempt_id),
            }
        if report.executed_actions:
            lines.append("---")
            lines.extend(report.executed_actions[:12])
        text = "\n".join(lines)
        try:
            await query.edit_message_text(text, reply_markup=_send_result_keyboard(chat_id=chat_id))
        except Exception:
            await message.reply_text(text, reply_markup=_send_result_keyboard(chat_id=chat_id))
        return

    errors = "\n".join(report.errors) if report.errors else "unknown error"
    fail_text = (
        "Telethon send failed.\n"
        f"chat_id: /{chat_id}\n"
        f"attempt_id: {attempt_id}\n"
        f"failed_action_index: {report.failed_action_index or '?'}\n"
        "---\n"
        f"{errors}"
    )
    try:
        await query.edit_message_text(fail_text, reply_markup=_send_result_keyboard(chat_id=chat_id))
    except Exception:
        await message.reply_text(fail_text, reply_markup=_send_result_keyboard(chat_id=chat_id))


async def _handle_undo_send_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid undo action")
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid chat id")
        return
    executor = app.bot_data.get("telethon_executor")
    if executor is None:
        await query.answer("Telethon sender unavailable")
        return
    last = _last_sent_by_chat(app).get(chat_id)
    if not isinstance(last, dict) or not isinstance(last.get("message_id"), int):
        await query.answer("No sent message to delete")
        return
    message_id = int(last["message_id"])
    try:
        await executor.delete_message(chat_id=chat_id, message_id=message_id)
        _last_sent_by_chat(app).pop(chat_id, None)
        await query.answer("Deleted sent message")
        await message.reply_text(f"Deleted sent message {message_id} for /{chat_id}.")
    except Exception as exc:
        await query.answer("Delete failed")
        await message.reply_text(f"Delete failed for {message_id} on /{chat_id}: {exc}")


async def _handle_noop_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    query = update.callback_query
    if query is None:
        return
    await query.answer("Unavailable in current state")


async def _handle_dry_run_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    service = app.bot_data.get("service")
    core = getattr(service, "core", None)
    if core is None:
        await query.answer("Service unavailable")
        return
    await query.answer("Dry run started")
    run_id = _next_reply_run_id(app)
    provider = "huggingface_openai_compat"
    model = str((getattr(core.config, "hf_model", None) or "").strip())
    parsed_output: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    result_text = ""
    status = "ok"
    error_message: str | None = None
    contract_issues: list[dict[str, Any]] = []
    outcome_class = "ok"
    conflict_payload: dict[str, Any] | None = None
    pivot_payload: dict[str, Any] | None = None
    repair_available = False
    retry_context: dict[str, Any] | None = None
    try:
        dry_run_result = core.run_hf_dry_run(chat_id)
        provider = str(dry_run_result.get("provider") or provider)
        model = str(dry_run_result.get("model") or model)
        raw_response = dry_run_result.get("response_json")
        if isinstance(raw_response, dict):
            response_json = raw_response
        parsed_output = dry_run_result.get("parsed_output") if isinstance(dry_run_result.get("parsed_output"), dict) else None
        result_text = str(dry_run_result.get("result_text") or "")
        raw_issues = dry_run_result.get("contract_issues")
        if isinstance(raw_issues, list):
            contract_issues = [item for item in raw_issues if isinstance(item, dict)]
        valid_output = bool(dry_run_result.get("valid_output"))
        error_message = str(dry_run_result.get("error_message") or "").strip() or None
        outcome_class = str(dry_run_result.get("outcome_class") or "ok")
        raw_conflict = dry_run_result.get("conflict")
        if isinstance(raw_conflict, dict):
            conflict_payload = raw_conflict
        raw_pivot = dry_run_result.get("pivot")
        if isinstance(raw_pivot, dict):
            pivot_payload = raw_pivot
        repair_available = bool(dry_run_result.get("repair_available"))
        raw_retry_context = dry_run_result.get("repair_context")
        if isinstance(raw_retry_context, dict):
            retry_context = raw_retry_context
        if outcome_class == "semantic_conflict":
            status = "semantic_conflict"
            if not error_message:
                error_message = "semantic conflict detected (operator decision required)"
        elif (not valid_output) or error_message:
            status = "error"
        if status == "ok" and not result_text.strip():
            status = "error"
            error_message = "empty model result"
    except Exception as exc:
        status = "error"
        error_message = str(exc)

    if status == "ok":
        state_payload = {
            "chat_id": chat_id,
            "provider": provider,
            "model": model or "unknown",
            "parsed_output": parsed_output if isinstance(parsed_output, dict) else None,
            "result_text": result_text,
            "retry_context": None,
            "run_id": run_id,
            "status": status,
            "outcome_class": outcome_class,
            "error_message": error_message or "",
            "contract_issues": contract_issues,
            "response_json": response_json if isinstance(response_json, dict) else {},
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "active_section": "message",
        }
        card_text = _render_result_card_text(state_payload, section="message")
        card_markup = _result_card_keyboard(
            chat_id=chat_id,
            active_section="message",
            status=status,
            telethon_enabled=app.bot_data.get("mode") == "live",
            retry_enabled=False,
            has_raw=bool(response_json),
        )
        sent_card = await _send_control_text(
            application=app,
            message=message,
            text=card_text,
            replace_previous_status=False,
            reply_markup=card_markup,
        )
        _set_reply_card_state(
            app,
            int(sent_card.message_id),
            chat_id=chat_id,
            provider=provider,
            model=model or "unknown",
            parsed_output=parsed_output if isinstance(parsed_output, dict) else None,
            result_text=result_text,
            retry_context=None,
            run_id=run_id,
            status=status,
            outcome_class=outcome_class,
            error_message=error_message,
            contract_issues=contract_issues,
            response_json=response_json if isinstance(response_json, dict) else {},
            conflict=conflict_payload,
            pivot=pivot_payload,
            active_section="message",
        )
    else:
        state_payload = {
            "chat_id": chat_id,
            "provider": provider,
            "model": model or "unknown",
            "parsed_output": parsed_output if isinstance(parsed_output, dict) else None,
            "result_text": result_text,
            "retry_context": retry_context,
            "run_id": run_id,
            "status": status,
            "outcome_class": outcome_class,
            "error_message": error_message or "",
            "contract_issues": contract_issues,
            "response_json": response_json if isinstance(response_json, dict) else {},
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "active_section": "error",
        }
        card_text = _render_result_card_text(state_payload, section="error")
        card_markup = _result_card_keyboard(
            chat_id=chat_id,
            active_section="error",
            status=status,
            telethon_enabled=app.bot_data.get("mode") == "live",
            retry_enabled=repair_available,
            has_raw=bool(response_json),
        )
        sent = await _send_control_text(
            application=app,
            message=message,
            text=card_text,
            replace_previous_status=False,
            reply_markup=card_markup,
        )
        _set_reply_card_state(
            app,
            int(sent.message_id),
            chat_id=chat_id,
            provider=provider,
            model=model or "unknown",
            parsed_output=parsed_output,
            result_text=result_text,
            retry_context=retry_context,
            run_id=run_id,
            status=status,
            outcome_class=outcome_class,
            error_message=error_message,
            contract_issues=contract_issues,
            response_json=response_json if isinstance(response_json, dict) else {},
            conflict=conflict_payload,
            pivot=pivot_payload,
            active_section="error",
        )


async def _handle_dry_run_retry_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid retry action")
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid retry parameters")
        return
    service = app.bot_data.get("service")
    core = getattr(service, "core", None)
    if core is None:
        await query.answer("Service unavailable")
        return
    state = _get_reply_card_state(app, int(message.message_id))
    if not isinstance(state, dict):
        await query.answer("Card state missing")
        return
    if int(state.get("chat_id", -1)) != int(chat_id):
        await query.answer("Card chat mismatch")
        return
    retry_context = state.get("retry_context")
    if not isinstance(retry_context, dict):
        await query.answer("Retry unavailable")
        return
    failed_generation = str(retry_context.get("failed_generation_excerpt") or state.get("result_text") or "")
    reject_reason = str(retry_context.get("reject_reason") or "contract_validation_failed")
    await query.answer("Retry started")

    provider = "huggingface_openai_compat"
    model = str((getattr(core.config, "hf_model", None) or "").strip())
    parsed_output: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    result_text = ""
    status = "ok"
    error_message: str | None = None
    contract_issues: list[dict[str, Any]] = []
    outcome_class = "ok"
    conflict_payload: dict[str, Any] | None = None
    pivot_payload: dict[str, Any] | None = None
    repair_available = False
    next_retry_context: dict[str, Any] | None = None
    try:
        dry_run_result = core.run_hf_dry_run_repair(
            chat_id=chat_id,
            failed_generation=failed_generation,
            reject_reason=reject_reason,
        )
        provider = str(dry_run_result.get("provider") or provider)
        model = str(dry_run_result.get("model") or model)
        raw_response = dry_run_result.get("response_json")
        if isinstance(raw_response, dict):
            response_json = raw_response
        parsed_output = dry_run_result.get("parsed_output") if isinstance(dry_run_result.get("parsed_output"), dict) else None
        result_text = str(dry_run_result.get("result_text") or "")
        raw_issues = dry_run_result.get("contract_issues")
        if isinstance(raw_issues, list):
            contract_issues = [item for item in raw_issues if isinstance(item, dict)]
        valid_output = bool(dry_run_result.get("valid_output"))
        error_message = str(dry_run_result.get("error_message") or "").strip() or None
        outcome_class = str(dry_run_result.get("outcome_class") or "ok")
        raw_conflict = dry_run_result.get("conflict")
        if isinstance(raw_conflict, dict):
            conflict_payload = raw_conflict
        raw_pivot = dry_run_result.get("pivot")
        if isinstance(raw_pivot, dict):
            pivot_payload = raw_pivot
        repair_available = bool(dry_run_result.get("repair_available"))
        raw_retry_context = dry_run_result.get("repair_context")
        if isinstance(raw_retry_context, dict):
            next_retry_context = raw_retry_context
        if outcome_class == "semantic_conflict":
            status = "semantic_conflict"
            if not error_message:
                error_message = "semantic conflict detected (operator decision required)"
        elif (not valid_output) or error_message:
            status = "error"
        if status == "ok" and not result_text.strip():
            status = "error"
            error_message = "empty model result"
    except Exception as exc:
        status = "error"
        error_message = str(exc)

    run_id = _next_reply_run_id(app)
    if status == "ok" and isinstance(parsed_output, dict):
        state_payload = {
            "chat_id": chat_id,
            "provider": provider,
            "model": model or "unknown",
            "parsed_output": parsed_output,
            "result_text": result_text,
            "retry_context": None,
            "run_id": run_id,
            "status": status,
            "outcome_class": outcome_class,
            "error_message": error_message or "",
            "contract_issues": contract_issues,
            "response_json": response_json if isinstance(response_json, dict) else {},
            "conflict": conflict_payload,
            "pivot": pivot_payload,
            "active_section": "message",
        }
        text = _render_result_card_text(state_payload, section="message")
        card_markup = _result_card_keyboard(
            chat_id=chat_id,
            active_section="message",
            status=status,
            telethon_enabled=app.bot_data.get("mode") == "live",
            retry_enabled=False,
            has_raw=bool(response_json),
        )
        try:
            await query.edit_message_text(
                text,
                reply_markup=card_markup,
            )
        except Exception:
            await message.reply_text(
                text,
                reply_markup=card_markup,
            )
        _set_reply_card_state(
            app,
            int(message.message_id),
            chat_id=chat_id,
            provider=provider,
            model=model or "unknown",
            parsed_output=parsed_output,
            result_text=result_text,
            retry_context=None,
            run_id=run_id,
            status=status,
            outcome_class=outcome_class,
            error_message=error_message,
            contract_issues=contract_issues,
            response_json=response_json if isinstance(response_json, dict) else {},
            conflict=conflict_payload,
            pivot=pivot_payload,
            active_section="message",
        )
        return

    state_payload = {
        "chat_id": chat_id,
        "provider": provider,
        "model": model or "unknown",
        "parsed_output": parsed_output if isinstance(parsed_output, dict) else None,
        "result_text": result_text,
        "retry_context": next_retry_context,
        "run_id": run_id,
        "status": status,
        "outcome_class": outcome_class,
        "error_message": error_message or "",
        "contract_issues": contract_issues,
        "response_json": response_json if isinstance(response_json, dict) else {},
        "conflict": conflict_payload,
        "pivot": pivot_payload,
        "active_section": "error",
    }
    section_text = _render_result_card_text(state_payload, section="error")
    card_markup = _result_card_keyboard(
        chat_id=chat_id,
        active_section="error",
        status=status,
        telethon_enabled=app.bot_data.get("mode") == "live",
        retry_enabled=repair_available,
        has_raw=bool(response_json),
    )
    try:
        await query.edit_message_text(section_text, reply_markup=card_markup)
    except Exception:
        await message.reply_text(section_text, reply_markup=card_markup)
    _set_reply_card_state(
        app,
        int(message.message_id),
        chat_id=chat_id,
        provider=provider,
        model=model or "unknown",
        parsed_output=parsed_output,
        result_text=result_text,
        retry_context=next_retry_context,
        run_id=run_id,
        status=status,
        outcome_class=outcome_class,
        error_message=error_message,
        contract_issues=contract_issues,
        response_json=response_json if isinstance(response_json, dict) else {},
        conflict=conflict_payload,
        pivot=pivot_payload,
        active_section="error",
    )


def _build_raw_result_payload_from_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "scambait.result.card.v1",
        "chat_id": int(state.get("chat_id") or -1),
        "run_id": state.get("run_id"),
        "provider": state.get("provider"),
        "model": state.get("model"),
        "status": state.get("status"),
        "outcome_class": state.get("outcome_class"),
        "error_message": state.get("error_message"),
        "contract_issues": state.get("contract_issues") if isinstance(state.get("contract_issues"), list) else [],
        "parsed_output": state.get("parsed_output") if isinstance(state.get("parsed_output"), dict) else None,
        "result_text": str(state.get("result_text") or ""),
        "response_json": state.get("response_json") if isinstance(state.get("response_json"), dict) else {},
        "conflict": state.get("conflict") if isinstance(state.get("conflict"), dict) else None,
        "pivot": state.get("pivot") if isinstance(state.get("pivot"), dict) else None,
    }


async def _handle_result_section_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer("Invalid section")
        return
    _, _, section, chat_raw = parts
    if section not in RESULT_SECTION_LABELS:
        await query.answer("Invalid section")
        return
    try:
        chat_id = int(chat_raw)
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    state = _get_reply_card_state(app, int(message.message_id))
    if not isinstance(state, dict):
        await query.answer("Card state missing")
        return
    if int(state.get("chat_id", -1)) != chat_id:
        await query.answer("Card chat mismatch")
        return
    status = str(state.get("status") or "unknown")
    retry_enabled = isinstance(state.get("retry_context"), dict)
    has_raw = bool(state.get("response_json"))
    text = _render_result_card_text(state, section=section)
    keyboard = _result_card_keyboard(
        chat_id=chat_id,
        active_section=section,
        status=status,
        telethon_enabled=app.bot_data.get("mode") == "live",
        retry_enabled=retry_enabled,
        has_raw=has_raw,
    )
    target_message_id = int(message.message_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception:
        sent = await message.reply_text(text, reply_markup=keyboard)
        target_message_id = int(sent.message_id)
    _set_reply_card_state(
        app,
        target_message_id,
        chat_id=chat_id,
        provider=str(state.get("provider") or "unknown"),
        model=str(state.get("model") or "unknown"),
        parsed_output=state.get("parsed_output") if isinstance(state.get("parsed_output"), dict) else None,
        result_text=str(state.get("result_text") or ""),
        retry_context=state.get("retry_context") if isinstance(state.get("retry_context"), dict) else None,
        run_id=state.get("run_id") if isinstance(state.get("run_id"), int) else None,
        status=status,
        outcome_class=str(state.get("outcome_class") or "unknown"),
        error_message=str(state.get("error_message") or ""),
        contract_issues=state.get("contract_issues") if isinstance(state.get("contract_issues"), list) else [],
        response_json=state.get("response_json") if isinstance(state.get("response_json"), dict) else {},
        conflict=state.get("conflict") if isinstance(state.get("conflict"), dict) else None,
        pivot=state.get("pivot") if isinstance(state.get("pivot"), dict) else None,
        active_section=section,
    )
    await query.answer(f"Section: {section}")


async def _handle_result_rawfile_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid action")
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    state = _get_reply_card_state(app, int(message.message_id))
    if not isinstance(state, dict):
        await query.answer("Card state missing")
        return
    if int(state.get("chat_id", -1)) != chat_id:
        await query.answer("Card chat mismatch")
        return
    raw_output = _raw_model_output_text(state)
    if not raw_output:
        await query.answer("Raw output unavailable")
        return
    run_id = state.get("run_id")
    filename = f"dry_run_chat_{chat_id}_run_{run_id if isinstance(run_id, int) else 'unknown'}.txt"
    await message.reply_document(
        document=InputFile(io.BytesIO(raw_output.encode("utf-8")), filename=filename)
    )
    await query.answer("Raw file sent")



async def _handle_reply_send_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid send action")
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid chat id")
        return
    state = _get_reply_card_state(app, int(message.message_id))
    if not isinstance(state, dict) or int(state.get("chat_id", -1)) != chat_id:
        await query.answer("Card state missing")
        return
    parsed_output = state.get("parsed_output")
    if not isinstance(parsed_output, dict):
        await query.answer("No reply payload")
        return
    executor = app.bot_data.get("telethon_executor")
    if executor is None:
        await query.answer("Telethon sender unavailable")
        return
    actions = parsed_output.get("actions") if isinstance(parsed_output.get("actions"), list) else []
    message_text = _extract_action_message_text(parsed_output)
    if not actions and message_text:
        actions = [{"type": "send_message", "message": {"text": message_text}}]
    if not actions:
        await query.answer("No sendable action")
        return
    await query.answer("Sending...")
    report = await executor.execute_actions(
        chat_id=chat_id,
        parsed_output={"message": {"text": message_text}, "actions": actions},
    )
    if report.ok:
        service = app.bot_data.get("service")
        store = _resolve_store(service)
        if message_text:
            store.ingest_event(
                chat_id=chat_id,
                event_type="message",
                role="scambaiter",
                text=message_text,
                source_message_id=None,
                meta={"origin": "telethon_send", "control_message_id": int(message.message_id)},
            )
        lines = [
            f"Sent via Telethon for /{chat_id}",
            f"actions_executed: {len(report.executed_actions)}",
        ]
        if report.sent_message_id is not None:
            lines.append(f"sent_message_id: {report.sent_message_id}")
            _last_sent_by_chat(app)[chat_id] = {"message_id": int(report.sent_message_id), "attempt_id": 0}
        if report.executed_actions:
            lines.append("---")
            lines.extend(report.executed_actions[:12])
        try:
            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=_send_result_keyboard(chat_id=chat_id),
            )
        except Exception:
            await message.reply_text("\n".join(lines), reply_markup=_send_result_keyboard(chat_id=chat_id))
        _drop_reply_card_state(app, int(message.message_id))
        return
    errors = "\n".join(report.errors) if report.errors else "unknown error"
    fail_text = (
        "Telethon send failed.\n"
        f"chat_id: /{chat_id}\n"
        f"failed_action_index: {report.failed_action_index or '?'}\n"
        "---\n"
        f"{errors}"
    )
    try:
        await query.edit_message_text(fail_text, reply_markup=_reply_action_keyboard(chat_id, telethon_enabled=True))
    except Exception:
        await message.reply_text(fail_text, reply_markup=_reply_action_keyboard(chat_id, telethon_enabled=True))


async def _handle_reply_mark_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid mark action")
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid chat id")
        return
    state = _get_reply_card_state(app, int(message.message_id))
    if not isinstance(state, dict) or int(state.get("chat_id", -1)) != chat_id:
        await query.answer("Card state missing")
        return
    await query.answer("Marked as sent")
    text = (
        f"Marked as sent (manual path) for /{chat_id}\n"
        "Forward your sent Telegram messages to ingest them into MessageStore."
    )
    try:
        await query.edit_message_text(text, reply_markup=_reply_error_keyboard(chat_id, retry_enabled=False))
    except Exception:
        await message.reply_text(text, reply_markup=_reply_error_keyboard(chat_id, retry_enabled=False))


async def _handle_reply_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    _drop_reply_card_state(app, int(message.message_id))
    try:
        await message.delete()
    except Exception:
        pass
    await query.answer("Deleted")


async def _handle_prompt_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is not None:
        try:
            await message.delete()
        except Exception:
            pass
    await query.answer("Deleted")


async def _handle_forward_select_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid chat selection")
        return
    try:
        target_chat_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid chat id")
        return
    control_chat_id = int(message.chat_id)
    _forward_card_targets(app)[control_chat_id] = target_chat_id
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    await _update_forward_card(
        application=app,
        message=message,
        store=store,
        control_chat_id=control_chat_id,
    )
    await query.answer(f"Target /{target_chat_id} selected")


async def _handle_forward_manual_override_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    control_chat_id = int(message.chat_id)
    manual_requests = _manual_override_requests(app)
    if control_chat_id in manual_requests:
        await query.answer("Manual override already pending")
        return
    manual_labels = _manual_override_labels(app)
    if manual_labels.get(control_chat_id):
        await query.answer("Manual alias already set for this chat")
        return
    prompt = await message.reply_text(
        "Manual override: reply with a unique alias (e.g. 'scammer_alpha'). This label will be hashed into a placeholder chat id for the bot to keep using for this source.",
        reply_markup=ForceReply(selective=True),
    )
    manual_requests[control_chat_id] = int(prompt.message_id)
    await query.answer("Awaiting alias reply")


async def _handle_forward_discard_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    control_chat_id = int(message.chat_id)
    _clear_forward_session(app, control_chat_id)
    card_map = _forward_card_messages(app)
    card_map.pop(control_chat_id, None)
    try:
        await message.delete()
    except Exception:
        pass
    await query.answer("Forward batch discarded")


async def _handle_forward_insert_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("Invalid insert action")
        return
    try:
        int(parts[2])
    except ValueError:
        await query.answer("Invalid control chat")
        return
    control_chat_id = int(message.chat_id)
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    pending = _pending_forwards(app).get(control_chat_id, [])
    target_chat_id = _forward_card_targets(app).get(control_chat_id)
    if not isinstance(target_chat_id, int) or target_chat_id <= 0:
        await query.answer("No target chat selected")
        return
    allow_placeholder = target_chat_id < 0
    merge = _plan_forward_merge(store, target_chat_id, pending, allow_placeholder=allow_placeholder)
    mode = str(merge.get("mode") or "")
    insert_payloads = merge.get("insert_payloads")
    if mode not in {"append", "backfill"} or not isinstance(insert_payloads, list) or not insert_payloads:
        await _update_forward_card(
            application=app,
            message=message,
            store=store,
            control_chat_id=control_chat_id,
        )
        await query.answer("Insert blocked")
        return
    inserted = 0
    skipped = 0
    for payload in insert_payloads:
        try:
            _ingest_forward_payload(store=store, target_chat_id=target_chat_id, payload=payload)
            inserted += 1
        except Exception:
            skipped += 1
    _clear_forward_session(app, control_chat_id)
    _forward_card_messages(app).pop(control_chat_id, None)
    summary = (
        f"Forward batch inserted for /{target_chat_id}\n"
        f"mode: {mode}\n"
        f"inserted: {inserted}\n"
        f"skipped: {skipped}"
    )
    try:
        await query.edit_message_text(summary)
    except Exception:
        await message.reply_text(summary)
    # Refresh the chat card for the target chat immediately after insertion completes.
    _schedule_user_card_update(
        application=app,
        control_chat_id=control_chat_id,
        store=store,
        target_chat_id=target_chat_id,
    )
    await query.answer("Inserted")


async def _handle_manual_override_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    message = update.effective_message
    if message is None:
        return
    if message.reply_to_message is None:
        return
    if not isinstance(message.text, str) or not message.text.strip():
        await message.reply_text("Alias cannot be empty. Try again.")
        return
    control_chat_id = int(message.chat_id)
    manual_requests = _manual_override_requests(app)
    prompt_id = manual_requests.get(control_chat_id)
    if not isinstance(prompt_id, int) or int(message.reply_to_message.message_id) != prompt_id:
        return
    manual_requests.pop(control_chat_id, None)
    label = message.text.strip()
    placeholder = _manual_alias_placeholder(label)
    _manual_override_labels(app)[control_chat_id] = label
    _forward_card_targets(app)[control_chat_id] = placeholder
    _active_targets(app)[control_chat_id] = placeholder
    _auto_targets(app)[control_chat_id] = placeholder
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    await _update_forward_card(
        application=app,
        message=None,
        store=store,
        control_chat_id=control_chat_id,
    )
    await _show_user_card(
        application=app,
        control_chat_id=control_chat_id,
        store=store,
        target_chat_id=placeholder,
    )


def _is_forward_message(message: Message) -> bool:
    return bool(getattr(message, "forward_origin", None))


def _infer_event_type(message: Message) -> str:
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "text", None) or getattr(message, "caption", None):
        return "message"
    return "forward"


def _extract_text(message: Message) -> str | None:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    caption = getattr(message, "caption", None)
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    return None


def _extract_origin_message_id(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    value = getattr(origin, "message_id", None)
    return value if isinstance(value, int) else None


def _build_source_message_id(forward_identity_key: str, strategy: str, event_type: str, text: str | None) -> str:
    key_digest = hashlib.sha1(forward_identity_key.encode("utf-8")).hexdigest()[:16]
    raw = text or ""
    text_digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"fwd:v2:{strategy}:{key_digest}:{event_type}:{text_digest}"


def _extract_forward_profile_info(message: Message) -> dict[str, Any]:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return {}
    info: dict[str, Any] = {"origin_kind": type(origin).__name__}
    origin_date = getattr(origin, "date", None)
    if isinstance(origin_date, datetime):
        info["origin_date_utc"] = origin_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    origin_message_id = getattr(origin, "message_id", None)
    if isinstance(origin_message_id, int):
        info["origin_message_id"] = origin_message_id
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        user_info: dict[str, Any] = {}
        for field in ("id", "username", "first_name", "last_name", "language_code", "is_bot"):
            value = getattr(sender_user, field, None)
            if value not in (None, ""):
                user_info[field] = value
        if user_info:
            info["sender_user"] = user_info
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        # MessageOriginChannel exposes "chat" instead of "sender_chat".
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        chat_info: dict[str, Any] = {}
        for field in ("id", "type", "title", "username"):
            value = getattr(sender_chat, field, None)
            if value not in (None, ""):
                chat_info[field] = value
        if chat_info:
            info["sender_chat"] = chat_info
    for field in ("sender_user_name",):
        value = getattr(origin, field, None)
        if value not in (None, ""):
            info[field] = value
    return info


def _extract_forward_identity(
    *,
    origin: Any,
    forward_profile: dict[str, Any],
    event_type: str,
    text: str | None,
    message: Message,
) -> dict[str, Any]:
    origin_kind = str(forward_profile.get("origin_kind") or type(origin).__name__)
    origin_message_id = getattr(origin, "message_id", None)
    if isinstance(origin_message_id, int):
        sender_chat = getattr(origin, "chat", None)
        if sender_chat is None:
            sender_chat = getattr(origin, "sender_chat", None)
        sender_chat_id = getattr(sender_chat, "id", None) if sender_chat is not None else None
        if isinstance(sender_chat_id, int):
            key = f"channel:{sender_chat_id}:{origin_message_id}"
            return {"strategy": "channel_message_id", "key": key, "origin_kind": origin_kind}
    origin_date_utc = str(forward_profile.get("origin_date_utc") or "")
    sender_user = forward_profile.get("sender_user")
    sender_chat = forward_profile.get("sender_chat")
    sender_user_name = str(forward_profile.get("sender_user_name") or "")
    sender_user_id = sender_user.get("id") if isinstance(sender_user, dict) else None
    sender_chat_id = sender_chat.get("id") if isinstance(sender_chat, dict) else None
    media = getattr(message, "photo", None)
    media_marker = ""
    if isinstance(media, list) and media:
        last = media[-1]
        marker = getattr(last, "file_unique_id", None)
        if isinstance(marker, str):
            media_marker = marker
    key_payload = {
        "origin_kind": origin_kind,
        "origin_date_utc": origin_date_utc,
        "sender_user_id": sender_user_id if isinstance(sender_user_id, int) else None,
        "sender_chat_id": sender_chat_id if isinstance(sender_chat_id, int) else None,
        "sender_user_name": sender_user_name or None,
        "event_type": event_type,
        "text": text if isinstance(text, str) else None,
        "media_marker": media_marker or None,
    }
    key_json = json.dumps(key_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    key = "sig:" + hashlib.sha1(key_json.encode("utf-8")).hexdigest()
    return {"strategy": "origin_signature", "key": key, "origin_kind": origin_kind}


def _event_ts_utc_for_store(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    origin_date = getattr(origin, "date", None) if origin is not None else None
    if not isinstance(origin_date, datetime):
        return None
    # Some forwards expose forward-time only; if equal, treat it as unknown.
    if origin_date == message.date:
        return None
    return origin_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _infer_target_chat_id_from_forward(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        sender_chat_id = getattr(sender_chat, "id", None)
        if isinstance(sender_chat_id, int):
            return sender_chat_id
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_user_id = getattr(sender_user, "id", None)
        if isinstance(sender_user_id, int):
            return sender_user_id
    return None


def _should_reuse_forward_target(
    target_chat_id: int | None,
    forward_target_hint: int | None,
    control_user_id: int | None,
) -> bool:
    if not isinstance(target_chat_id, int) or target_chat_id <= 0:
        return False
    if not isinstance(forward_target_hint, int):
        return True
    if isinstance(control_user_id, int) and forward_target_hint == control_user_id:
        return True
    return forward_target_hint == target_chat_id


def _infer_role_from_forward(message: Message, target_chat_id: int) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return "manual"
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        sender_chat_id = getattr(sender_chat, "id", None)
        if isinstance(sender_chat_id, int) and sender_chat_id == target_chat_id:
            return "scammer"
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_user_id = getattr(sender_user, "id", None)
        if isinstance(sender_user_id, int) and sender_user_id == target_chat_id:
            return "scammer"
    return "manual"


def _infer_role_without_target(message: Message, control_user_id: int | None) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return "manual"
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        sender_user_id = getattr(sender_user, "id", None)
        if isinstance(sender_user_id, int) and control_user_id is not None and sender_user_id == control_user_id:
            return "manual"
        if isinstance(sender_user_id, int):
            return "scammer"
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat is None:
        sender_chat = getattr(origin, "chat", None)
    if sender_chat is not None:
        sender_chat_id = getattr(sender_chat, "id", None)
        if isinstance(sender_chat_id, int):
            return "scammer"
    return "manual"


def _resolve_target_and_role_without_active(
    message: Message,
    control_user_id: int | None,
    auto_target_chat_id: int | None,
) -> tuple[int | None, str]:
    sender_id = _infer_target_chat_id_from_forward(message)
    if sender_id is None:
        return None, "manual"   # hidden sender — always require manual override
    if control_user_id is not None and sender_id == control_user_id:
        if auto_target_chat_id is None:
            return None, "manual"
        return auto_target_chat_id, "manual"
    return sender_id, "scammer"


def _control_sender_info(message: Message) -> dict[str, Any] | None:
    sender = getattr(message, "from_user", None)
    if sender is None:
        return None
    info: dict[str, Any] = {}
    for field in ("id", "username", "first_name", "last_name"):
        value = getattr(sender, field, None)
        if value not in (None, ""):
            info[field] = value
    if info:
        return info
    return None


def _build_forward_payload(message: Message, role: str) -> dict[str, Any]:
    event_type = _infer_event_type(message)
    text = _extract_text(message)
    origin = getattr(message, "forward_origin", None)
    origin_message_id = _extract_origin_message_id(message)
    forward_profile = _extract_forward_profile_info(message)
    if origin is not None:
        forward_identity = _extract_forward_identity(
            origin=origin,
            forward_profile=forward_profile,
            event_type=event_type,
            text=text,
            message=message,
        )
    else:
        forward_identity = {
            "strategy": "origin_signature",
            "key": f"sig:missing:{message.chat_id}:{message.message_id}",
            "origin_kind": "Unknown",
        }
    source_message_id = _build_source_message_id(
        str(forward_identity.get("key") or ""),
        str(forward_identity.get("strategy") or "origin_signature"),
        event_type,
        text,
    )
    meta: dict[str, Any] = {
        "control_chat_id": int(message.chat_id),
        "control_message_id": int(message.message_id),
        "forward_profile": forward_profile,
        "forward_identity": forward_identity,
        "origin_message_id": origin_message_id,
    }
    control_sender = _control_sender_info(message)
    if control_sender:
        meta["control_sender"] = control_sender
    return {
        "event_type": event_type,
        "source_message_id": source_message_id,
        "origin_message_id": origin_message_id,
        "role": role,
        "text": text,
        "ts_utc": _event_ts_utc_for_store(message),
        "meta": meta,
    }


def _profile_patch_from_forward_profile(forward_profile: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {"identity": {}, "account": {}, "provenance": {}}
    sender_user = forward_profile.get("sender_user")
    sender_chat = forward_profile.get("sender_chat")
    sender_user_name = forward_profile.get("sender_user_name")
    if isinstance(sender_user, dict):
        value = sender_user.get("id")
        if isinstance(value, int):
            patch["identity"]["telegram_user_id"] = value
        value = sender_user.get("username")
        if isinstance(value, str) and value.strip():
            patch["identity"]["username"] = value.strip()
        first = sender_user.get("first_name")
        if isinstance(first, str) and first.strip():
            patch["identity"]["first_name"] = first.strip()
        last = sender_user.get("last_name")
        if isinstance(last, str) and last.strip():
            patch["identity"]["last_name"] = last.strip()
        is_bot = sender_user.get("is_bot")
        if isinstance(is_bot, bool):
            patch["account"]["is_bot"] = is_bot
        lang_code = sender_user.get("language_code")
        if isinstance(lang_code, str) and lang_code.strip():
            patch["account"]["lang_code"] = lang_code.strip()
    if isinstance(sender_chat, dict):
        value = sender_chat.get("id")
        if isinstance(value, int):
            patch["identity"]["telegram_chat_id"] = value
        title = sender_chat.get("title")
        if isinstance(title, str) and title.strip():
            patch["identity"]["display_name"] = title.strip()
        username = sender_chat.get("username")
        if isinstance(username, str) and username.strip():
            patch["identity"]["username"] = username.strip()
    if isinstance(sender_user_name, str) and sender_user_name.strip():
        patch["identity"]["display_name"] = sender_user_name.strip()
    # Derive display_name when only first/last exist.
    first_name = patch["identity"].get("first_name")
    last_name = patch["identity"].get("last_name")
    if "display_name" not in patch["identity"] and isinstance(first_name, str):
        if isinstance(last_name, str):
            patch["identity"]["display_name"] = f"{first_name} {last_name}".strip()
        else:
            patch["identity"]["display_name"] = first_name
    patch["provenance"]["last_source"] = "botapi_forward"
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        if isinstance(value, dict) and value:
            cleaned[key] = value
    return cleaned


def _ingest_forward_payload(store: Any, target_chat_id: int, payload: dict[str, Any]) -> Any:
    source_message_id = str(payload.get("source_message_id") or "")
    if not source_message_id:
        raise ValueError("missing source_message_id for forward ingestion")
    meta_obj = payload.get("meta")
    forward_identity = meta_obj.get("forward_identity") if isinstance(meta_obj, dict) else None
    if not isinstance(forward_identity, dict) or not isinstance(forward_identity.get("key"), str):
        raise ValueError("missing forward_identity for forward ingestion")
    record = store.ingest_user_forward(
        chat_id=target_chat_id,
        event_type=str(payload["event_type"]),
        text=payload.get("text"),
        source_message_id=source_message_id,
        role=str(payload["role"]),
        ts_utc=payload.get("ts_utc"),
        meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
    )
    meta = payload.get("meta")
    if isinstance(meta, dict):
        forward_profile = meta.get("forward_profile")
        if isinstance(forward_profile, dict) and forward_profile:
            patch = _profile_patch_from_forward_profile(forward_profile)
            if patch:
                changed_at = payload.get("ts_utc")
                store.upsert_chat_profile(
                    chat_id=target_chat_id,
                    patch=patch,
                    source="botapi_forward",
                    changed_at=changed_at if isinstance(changed_at, str) else None,
                )
    return record


def _forward_item_signature(payload: dict[str, Any]) -> tuple[str, str]:
    event_type = str(payload.get("event_type") or "")
    text = payload.get("text")
    return event_type, str(text) if isinstance(text, str) else ""


def _extract_forward_identity_key_from_event(event: Any) -> str | None:
    meta = getattr(event, "meta", None)
    if isinstance(meta, dict):
        forward_identity = meta.get("forward_identity")
        if isinstance(forward_identity, dict):
            key = forward_identity.get("key")
            if isinstance(key, str) and key:
                return key
        origin_id = meta.get("origin_message_id")
        if isinstance(origin_id, int):
            return f"legacy_origin:{origin_id}"
    source_message_id = getattr(event, "source_message_id", None)
    if isinstance(source_message_id, str) and source_message_id:
        return f"legacy_source:{source_message_id}"
    return None


def _extract_forward_identity_key_from_payload(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    if isinstance(meta, dict):
        forward_identity = meta.get("forward_identity")
        if isinstance(forward_identity, dict):
            key = forward_identity.get("key")
            if isinstance(key, str) and key:
                return key
    origin_id = payload.get("origin_message_id")
    if isinstance(origin_id, int):
        return f"legacy_origin:{origin_id}"
    source_message_id = payload.get("source_message_id")
    if isinstance(source_message_id, str) and source_message_id:
        return f"legacy_source:{source_message_id}"
    return None


def _build_existing_identity_index(events: list[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for event in events:
        key = _extract_forward_identity_key_from_event(event)
        if key is None:
            continue
        out.setdefault(key, []).append(event)
    return out


def _plan_forward_merge(
    store: Any,
    target_chat_id: int,
    payloads: list[dict[str, Any]],
    *,
    allow_placeholder: bool = False,
) -> dict[str, Any]:
    if target_chat_id <= 0 and not allow_placeholder:
        return {"mode": "unresolved", "insert_payloads": [], "reason": "target chat unresolved"}
    if not payloads:
        return {"mode": "blocked", "insert_payloads": [], "reason": "batch empty"}
    missing_identity = [p for p in payloads if not isinstance(_extract_forward_identity_key_from_payload(p), str)]
    if missing_identity:
        return {
            "mode": "blocked",
            "insert_payloads": [],
            "reason": f"{len(missing_identity)} item(s) missing forward_identity",
        }

    events = store.list_events(chat_id=target_chat_id, limit=5000)
    existing_by_identity = _build_existing_identity_index(events)
    existing_scammer_keys: list[str] = []
    seen_scammer_keys: set[str] = set()
    for event in events:
        if str(getattr(event, "role", "")) != "scammer":
            continue
        key = _extract_forward_identity_key_from_event(event)
        if not isinstance(key, str):
            continue
        if key in seen_scammer_keys:
            continue
        seen_scammer_keys.add(key)
        existing_scammer_keys.append(key)

    insert_payloads: list[dict[str, Any]] = []
    batch_scammer_keys: list[str] = []
    batch_new_scammer_keys: list[str] = []
    for payload in payloads:
        identity_key = _extract_forward_identity_key_from_payload(payload)
        if not isinstance(identity_key, str):
            continue
        existing_rows = existing_by_identity.get(identity_key, [])
        role = str(payload.get("role") or "")
        if role == "scammer":
            batch_scammer_keys.append(identity_key)
            if not existing_rows:
                batch_new_scammer_keys.append(identity_key)
        sig = _forward_item_signature(payload)
        payload_event_type = str(payload.get("event_type") or "").strip().lower()
        has_same = False
        has_changed = False
        for row in existing_rows:
            row_event_type = str(getattr(row, "event_type", "") or "")
            row_text = str(getattr(row, "text", "") or "")
            row_sig = (row_event_type, row_text)
            row_event_type_lower = row_event_type.strip().lower()
            if row_event_type_lower == "forward" and payload_event_type != "forward":
                has_changed = True
                continue
            if row_sig == sig:
                has_same = True
                break
            has_changed = True
        if has_same:
            continue
        candidate = dict(payload)
        meta = candidate.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        if has_changed:
            meta["revision_of_forward_identity_key"] = identity_key
            meta["revision_reason"] = "content_changed"
        candidate["meta"] = meta
        insert_payloads.append(candidate)

    if not insert_payloads:
        return {"mode": "blocked", "insert_payloads": [], "reason": "batch already present"}

    if batch_new_scammer_keys:
        if not existing_scammer_keys:
            return {"mode": "append", "insert_payloads": insert_payloads, "reason": f"append {len(insert_payloads)} item(s)"}
        existing_pos = {key: idx for idx, key in enumerate(existing_scammer_keys)}
        first_new_idx = next((idx for idx, key in enumerate(batch_scammer_keys) if key not in existing_pos), len(batch_scammer_keys))
        has_known_after_new = any(key in existing_pos for key in batch_scammer_keys[first_new_idx:])
        prefix_known = batch_scammer_keys[:first_new_idx]
        is_suffix_match = bool(prefix_known) and prefix_known == existing_scammer_keys[-len(prefix_known) :]
        if (not has_known_after_new) and is_suffix_match:
            return {"mode": "append", "insert_payloads": insert_payloads, "reason": f"append {len(insert_payloads)} item(s)"}
        return {"mode": "backfill", "insert_payloads": insert_payloads, "reason": f"backfill {len(insert_payloads)} item(s)"}

    return {"mode": "backfill", "insert_payloads": insert_payloads, "reason": f"backfill {len(insert_payloads)} item(s)"}


def _manual_override_requests(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("manual_override_request_by_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["manual_override_request_by_chat"] = state
    return state


def _manual_override_labels(application: Application) -> dict[int, str]:
    state = application.bot_data.setdefault("manual_override_label_by_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["manual_override_label_by_chat"] = state
    return state


def _manual_alias_placeholder(alias: str) -> int:
    normalized = alias.strip()
    if not normalized:
        raise ValueError("alias cannot be empty")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big", signed=False)
    placeholder_base = (1 << 62) - 1
    placeholder_value = value % placeholder_base
    return -1 - placeholder_value


def _forward_card_keyboard(
    *,
    control_chat_id: int,
    target_chat_id: int | None,
    mode: str,
    known_chat_ids: list[int],
    manual_alias_label: str | None,
    manual_pending: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    has_resolved_target = isinstance(target_chat_id, int) and target_chat_id > 0
    if manual_alias_label is not None:
        has_resolved_target = True
    if has_resolved_target:
        if mode == "append":
            rows.append([InlineKeyboardButton(f"Append to /{target_chat_id}", callback_data=f"sc:fwd_insert:{control_chat_id}")])
        elif mode == "backfill":
            rows.append([InlineKeyboardButton(f"Backfill to /{target_chat_id}", callback_data=f"sc:fwd_insert:{control_chat_id}")])
        else:
            rows.append([InlineKeyboardButton("Insert blocked", callback_data="sc:nop")])
    else:
        if manual_pending:
            rows.append([InlineKeyboardButton("Manual override pending (enter alias)", callback_data="sc:nop")])
        else:
            rows.append([InlineKeyboardButton("Manual override alias", callback_data=f"sc:fwd_manual:{control_chat_id}")])
        for chat_id in known_chat_ids[:8]:
            rows.append([InlineKeyboardButton(f"/{chat_id}", callback_data=f"sc:fwd_selchat:{chat_id}")])
    rows.append([InlineKeyboardButton("Discard", callback_data=f"sc:fwd_discard:{control_chat_id}")])
    return InlineKeyboardMarkup(rows)


def _render_forward_card_text(
    *,
    control_chat_id: int,
    target_chat_id: int | None,
    payloads: list[dict[str, Any]],
    merge: dict[str, Any],
    manual_alias_label: str | None,
    manual_pending: bool,
) -> str:
    total = len(payloads)
    scammer = sum(1 for p in payloads if str(p.get("role") or "") == "scammer")
    manual = sum(1 for p in payloads if str(p.get("role") or "") != "scammer")
    missing_identity = sum(1 for p in payloads if not isinstance(_extract_forward_identity_key_from_payload(p), str))
    mode = str(merge.get("mode") or "unresolved")
    reason = str(merge.get("reason") or "-")
    target_text = f"/{target_chat_id}" if isinstance(target_chat_id, int) and target_chat_id > 0 else "(unresolved)"
    alias_label = manual_alias_label or "(none)"
    pending_note = " (waiting for entry)" if manual_pending else ""
    return (
        "Forward/Insert Card\n"
        f"control_chat: {control_chat_id}\n"
        f"target_chat: {target_text}\n"
        f"manual_alias: {alias_label}{pending_note}\n"
        f"batch_items: {total}\n"
        f"scammer_items: {scammer}\n"
        f"manual_items: {manual}\n"
        f"missing_forward_identity: {missing_identity}\n"
        f"merge_mode: {mode}\n"
        f"merge_reason: {reason}"
    )


async def _update_forward_card(
    *,
    application: Application,
    message: Message | None,
    store: Any,
    control_chat_id: int,
) -> None:
    pending = _pending_forwards(application)
    payloads = pending.get(control_chat_id, [])
    target_map = _forward_card_targets(application)
    target_chat_id = target_map.get(control_chat_id)
    allow_placeholder = isinstance(target_chat_id, int) and target_chat_id < 0
    merge = _plan_forward_merge(
        store,
        target_chat_id if isinstance(target_chat_id, int) else -1,
        payloads,
        allow_placeholder=allow_placeholder,
    )
    known_chat_ids = store.list_chat_ids(limit=30)
    manual_requests = _manual_override_requests(application)
    manual_alias_label = _manual_override_labels(application).get(control_chat_id)
    manual_pending = control_chat_id in manual_requests
    text = _render_forward_card_text(
        control_chat_id=control_chat_id,
        target_chat_id=target_chat_id,
        payloads=payloads,
        merge=merge,
        manual_alias_label=manual_alias_label,
        manual_pending=manual_pending,
    )
    keyboard = _forward_card_keyboard(
        control_chat_id=control_chat_id,
        target_chat_id=target_chat_id,
        mode=str(merge.get("mode") or "unresolved"),
        known_chat_ids=known_chat_ids,
        manual_alias_label=manual_alias_label,
        manual_pending=manual_pending,
    )
    message_ids = _forward_card_messages(application)
    current_id = message_ids.get(control_chat_id)
    if isinstance(current_id, int):
        try:
            await application.bot.edit_message_text(
                chat_id=control_chat_id,
                message_id=current_id,
                text=text,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass
    if message is not None:
        sent = await message.reply_text(text, reply_markup=keyboard)
    else:
        sent = await application.bot.send_message(chat_id=control_chat_id, text=text, reply_markup=keyboard)
    message_ids[control_chat_id] = int(getattr(sent, "message_id", None) or getattr(sent, "id", None) or 0)


def _clear_forward_session(application: Application, control_chat_id: int) -> None:
    _pending_forwards(application)[control_chat_id] = []
    _forward_card_targets(application).pop(control_chat_id, None)
    _manual_override_requests(application).pop(control_chat_id, None)
    _manual_override_labels(application).pop(control_chat_id, None)


def _flush_pending_forwards(
    application: Application,
    store: Any,
    control_chat_id: int,
    target_chat_id: int,
) -> int:
    pending = _pending_forwards(application)
    queue = pending.get(control_chat_id, [])
    if not queue:
        return 0
    imported = 0
    for payload in queue:
        _ingest_forward_payload(store=store, target_chat_id=target_chat_id, payload=payload)
        imported += 1
    pending[control_chat_id] = []
    return imported


def ingest_forwarded_message(store: Any, target_chat_id: int, message: Message) -> Any:
    role = _infer_role_from_forward(message, target_chat_id=target_chat_id)
    payload = _build_forward_payload(message, role=role)
    return _ingest_forward_payload(store=store, target_chat_id=target_chat_id, payload=payload)


def _sanitize_legacy_profile_text(text: str) -> str:
    if text.startswith("profile_update:") and text.endswith("(botapi_forward)"):
        return text[: -len("(botapi_forward)")].rstrip()
    return text


def _format_history_line(event: Any) -> str:
    ts = getattr(event, "ts_utc", None)
    hhmm = "--:--"
    if isinstance(ts, str) and ts:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hhmm = parsed.astimezone().strftime("%H:%M")
        except ValueError:
            if len(ts) >= 16:
                hhmm = ts[11:16]
    role = getattr(event, "role", "unknown")
    event_type = getattr(event, "event_type", "unknown")
    text = getattr(event, "text", None)
    if not text:
        return f"{hhmm} {role}/{event_type}"
    normalized_text = _sanitize_legacy_profile_text(str(text))
    flat_text = " ".join(normalized_text.split())
    if len(flat_text) > 120:
        flat_text = flat_text[:117] + "..."
    return f"{hhmm} {role}/{event_type}: {flat_text}"


async def _delete_control_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        # Cleanup should never break ingestion flow.
        return


async def _require_allowed_chat(
    application: Application,
    update: Update,
    allowed_chat_id: int | None,
) -> bool:
    if allowed_chat_id is None:
        return True
    message = update.effective_message
    if message is None:
        return False
    if int(message.chat_id) != int(allowed_chat_id):
        await _send_control_text(
            application=application,
            message=message,
            text="Unauthorized chat.",
        )
        return False
    return True


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        return
    message = update.effective_message
    if message is None:
        return
    text = (
        "ScamBaiterControl commands:\n"
        "/whoami - show current chat/user ids and auth status\n"
        "/chat <chat_id> - set target chat for forwarded history\n"
        "/<chat_id> or /c<chat_id> - quick select chat and show Chat Card\n"
        "/chats - list known chat ids\n"
        "/history [chat_id] - show latest stored events\n"
        "Forwarded messages are auto-assigned by sender identity.\n"
        "Use /chat <chat_id> only to force an explicit target override."
    )
    await _send_control_text(application=app, message=message, text=text)


def _render_whoami_text(message: Message, user_id: int | None, allowed_chat_id: int | None) -> str:
    chat_id = int(message.chat_id)
    authorized = allowed_chat_id is None or chat_id == int(allowed_chat_id)
    expected = str(allowed_chat_id) if allowed_chat_id is not None else "(not set)"
    return (
        "Control identity\n"
        f"chat_id: {chat_id}\n"
        f"user_id: {user_id if user_id is not None else 'unknown'}\n"
        f"allowed_chat_id: {expected}\n"
        f"authorized_here: {'yes' if authorized else 'no'}"
    )


async def _cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    message = update.effective_message
    if message is None:
        return
    user = update.effective_user
    user_id = int(user.id) if user is not None else None
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    await _send_control_text(
        application=app,
        message=message,
        text=_render_whoami_text(message=message, user_id=user_id, allowed_chat_id=allowed_chat_id),
    )


async def _cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not context.args:
        await _send_control_text(application=context.application, message=message, text="Usage: /chat <chat_id>")
        return
    try:
        target_chat_id = int(context.args[0])
    except ValueError:
        await _send_control_text(application=context.application, message=message, text="Invalid chat_id.")
        return
    await _set_active_chat_from_id(update, context, target_chat_id)


async def _set_active_chat_from_id(update: Update, context: ContextTypes.DEFAULT_TYPE, target_chat_id: int) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        return
    message = update.effective_message
    if message is None:
        return
    state = _active_targets(app)
    state[int(message.chat_id)] = target_chat_id
    _auto_targets(app)[int(message.chat_id)] = target_chat_id
    store = _resolve_store(app.bot_data["service"])
    _forward_card_targets(app)[int(message.chat_id)] = target_chat_id
    await _send_control_text(application=app, message=message, text=f"Active target chat set to {target_chat_id}.")

    if not store.list_events(chat_id=target_chat_id, limit=1):
        await _send_control_text(
            application=app,
            message=message,
            text=f"No stored events for {target_chat_id}; cannot render Chat Card.",
        )
        return

    await _show_user_card(
        application=app,
        control_chat_id=int(message.chat_id),
        store=store,
        target_chat_id=target_chat_id,
    )


async def _cmd_chat_id_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return
    command_part = text[1:].split(" ", 1)[0]
    if command_part.startswith("c"):
        command_part = command_part[1:]
    if not command_part.isdigit():
        return
    target_chat_id = int(command_part)
    await _set_active_chat_from_id(update, context, target_chat_id)


async def _cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        return
    message = update.effective_message
    if message is None:
        return
    store = _resolve_store(app.bot_data["service"])
    chat_ids = store.list_chat_ids(limit=100)
    if not chat_ids:
        await _send_control_text(application=app, message=message, text="No chat history stored yet.")
        return
    title, keyboard = _known_chats_card_content(store, chat_ids)
    await _send_control_text(
        application=app,
        message=message,
        text=title,
        reply_markup=keyboard,
    )


async def _handle_select_chat_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    query = update.callback_query
    if query is None:
        return
    message = query.message
    if message is None:
        await query.answer()
        return
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    data = query.data or ""
    try:
        target_chat_id = int(data.split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat_id")
        return
    await query.answer("Chat selected")
    await _set_active_chat_from_id(update, context, target_chat_id)


async def _cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        return
    message = update.effective_message
    if message is None:
        return
    state = _active_targets(app)
    target_chat_id: int | None = None
    if context.args:
        try:
            target_chat_id = int(context.args[0])
        except ValueError:
            await _send_control_text(application=app, message=message, text="Invalid chat_id.")
            return
    if target_chat_id is None:
        target_chat_id = state.get(int(message.chat_id))
    if target_chat_id is None:
        await _send_control_text(application=app, message=message, text="No active chat. Use /chat <chat_id> first.")
        return
    store = _resolve_store(app.bot_data["service"])
    events = store.list_events(chat_id=target_chat_id, limit=25)
    if not events:
        await _send_control_text(application=app, message=message, text=f"No events stored for {target_chat_id}.")
        return
    summary_scammer = None
    summary_baiter = None
    for event in events:
        meta = getattr(event, "meta", None)
        if summary_scammer is None:
            summary_scammer = scammer_name_from_meta(meta)
        if summary_baiter is None:
            summary_baiter = baiter_name_from_meta(meta)
        if summary_scammer and summary_baiter:
            break
    summary_text = (
        f"Scammer: {summary_scammer or 'unknown'}\n"
        f"Baiter: {summary_baiter or 'unknown'}"
    )
    lines = [_format_history_line(event) for event in events[-12:]]
    await _send_control_text(
        application=app,
        message=message,
        text=f"History {target_chat_id}:\n{summary_text}\n" + "\n".join(lines),
    )


async def _handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        return
    message = update.effective_message
    if message is None:
        return
    if not _is_forward_message(message):
        return
    control_chat_id = int(message.chat_id)
    control_user = update.effective_user
    control_user_id = int(control_user.id) if control_user is not None else None
    state = _active_targets(app)
    auto_targets = _auto_targets(app)
    target_chat_id = state.get(control_chat_id)
    forward_target_hint = _infer_target_chat_id_from_forward(message)
    role: str = "manual"
    inferred_target: int | None = target_chat_id
    if _should_reuse_forward_target(target_chat_id, forward_target_hint, control_user_id):
        if isinstance(target_chat_id, int):
            role = _infer_role_from_forward(message, target_chat_id=target_chat_id)
    else:
        auto_target_chat_id = auto_targets.get(control_chat_id)
        inferred_target, role = _resolve_target_and_role_without_active(
            message=message,
            control_user_id=control_user_id,
            auto_target_chat_id=auto_target_chat_id,
        )
        if role == "scammer" and isinstance(inferred_target, int):
            auto_targets[control_chat_id] = inferred_target
    if isinstance(inferred_target, int):
        state[control_chat_id] = inferred_target
        _forward_card_targets(app)[control_chat_id] = inferred_target
    store = _resolve_store(app.bot_data["service"])
    payload = _build_forward_payload(message, role=role)
    pending = _pending_forwards(app)
    queue = pending.setdefault(control_chat_id, [])
    queue.append(payload)
    await _update_forward_card(
        application=app,
        message=message,
        store=store,
        control_chat_id=control_chat_id,
    )
    await _delete_control_message(message)


async def _handle_fetch_profile_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    query = update.callback_query
    if query is None:
        return
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    try:
        chat_id = int((query.data or "").split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat id")
        return
    executor = app.bot_data.get("telethon_executor")
    if executor is None:
        await query.answer("Live Mode not active")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    await query.answer("Fetching profile...")
    await executor.fetch_profile(chat_id, store)
    control_chat_id = int(query.message.chat_id)
    _schedule_user_card_update(app, control_chat_id, store, chat_id, delay_seconds=0.5)


async def _handle_fetch_history_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    query = update.callback_query
    if query is None:
        return
    allowed_chat_id = app.bot_data.get("allowed_chat_id")
    if not await _require_allowed_chat(app, update, allowed_chat_id):
        await query.answer("Unauthorized")
        return
    try:
        chat_id = int((query.data or "").split(":")[-1])
    except ValueError:
        await query.answer("Invalid chat id")
        return
    executor = app.bot_data.get("telethon_executor")
    if executor is None:
        await query.answer("Live Mode not active")
        return
    service = app.bot_data.get("service")
    store = _resolve_store(service)
    await query.answer("Fetching history...")
    count = await executor.fetch_history(chat_id, store, limit=200)
    message = query.message
    if message is not None:
        await message.reply_text(f"History fetch complete: {count} new events for /{chat_id}.")


async def _register_command_menu(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "show control help"),
            BotCommand("whoami", "show current chat/user ids"),
            BotCommand("chat", "set active target chat id"),
            BotCommand("chats", "list known chat ids"),
            BotCommand("history", "show history for active chat"),
        ]
    )


def create_bot_app(
    token: str,
    service: Any,
    allowed_chat_id: int | None = None,
    telethon_executor: Any | None = None,
) -> Any:
    app = Application.builder().token(token).build()
    app.bot_data["service"] = service
    app.bot_data["allowed_chat_id"] = allowed_chat_id
    app.bot_data["telethon_executor"] = telethon_executor
    app.bot_data["mode"] = "live" if telethon_executor is not None else "relay"
    app.bot_data["register_command_menu"] = lambda: _register_command_menu(app)
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("whoami", _cmd_whoami))
    app.add_handler(CommandHandler("chat", _cmd_chat))
    app.add_handler(CommandHandler("chats", _cmd_chats))
    app.add_handler(CommandHandler("history", _cmd_history))
    app.add_handler(MessageHandler(filters.Regex(r"^/(?:c)?[0-9]+(?:\s.*)?$"), _cmd_chat_id_shortcut))
    app.add_handler(CallbackQueryHandler(_handle_select_chat_button, pattern=r"^sc:selchat:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_prompt_button, pattern=r"^sc:prompt:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_chat_close_button, pattern=r"^sc:chat_close:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_clear_history_button, pattern=r"^sc:clear_history:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_clear_history_arm_button, pattern=r"^sc:clear_history_arm:[0-9]+$"))
    app.add_handler(
        CallbackQueryHandler(_handle_clear_history_confirm_button, pattern=r"^sc:clear_history_confirm:[0-9]+$")
    )
    app.add_handler(
        CallbackQueryHandler(_handle_clear_history_cancel_button, pattern=r"^sc:clear_history_cancel:[0-9]+$")
    )
    app.add_handler(CallbackQueryHandler(_handle_prompt_section_button, pattern=r"^sc:psec:(?:messages|memory|system):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_prompt_close_button, pattern=r"^sc:prompt_close:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_reply_send_button, pattern=r"^sc:reply_send:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_reply_mark_button, pattern=r"^sc:reply_mark:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_undo_send_button, pattern=r"^sc:undo_send:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_dry_run_button, pattern=r"^sc:dryrun:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_dry_run_retry_button, pattern=r"^sc:reply_retry:[0-9]+$"))
    app.add_handler(
        CallbackQueryHandler(_handle_result_section_button, pattern=r"^sc:rsec:(?:message|actions|analysis|error|response|raw):[0-9]+$")
    )
    app.add_handler(CallbackQueryHandler(_handle_result_rawfile_button, pattern=r"^sc:rawfile:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_forward_insert_button, pattern=r"^sc:fwd_insert:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_forward_discard_button, pattern=r"^sc:fwd_discard:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_forward_select_chat_button, pattern=r"^sc:fwd_selchat:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_forward_manual_override_button, pattern=r"^sc:fwd_manual:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_reply_delete_button, pattern=r"^sc:reply_delete:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_prompt_delete_button, pattern=r"^sc:prompt_delete$"))
    app.add_handler(CallbackQueryHandler(_handle_noop_button, pattern=r"^sc:nop$"))
    app.add_handler(CallbackQueryHandler(_handle_fetch_profile_button, pattern=r"^sc:fetch_profile:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_fetch_history_button, pattern=r"^sc:fetch_history:[0-9]+$"))
    app.add_handler(MessageHandler(filters.REPLY, _handle_manual_override_response))
    app.add_handler(MessageHandler(filters.ALL, _handle_forward))
    return app
