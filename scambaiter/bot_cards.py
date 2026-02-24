"""Result card rendering, keyboards, formatting helpers — pure output, no side effects."""

from __future__ import annotations

import json
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


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
    from scambaiter.bot_prompt import _trim_block

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
