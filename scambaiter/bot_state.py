"""Bot data state accessors — thin wrappers around application.bot_data dicts."""

from __future__ import annotations

import asyncio
from typing import Any

from telegram.ext import Application


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


def _last_sent_by_chat(application: Application) -> dict[int, dict[str, int]]:
    state = application.bot_data.setdefault("last_sent_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["last_sent_by_target_chat"] = state
    return state


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


def _auto_send_enabled(application: Application) -> dict[int, bool]:
    state = application.bot_data.setdefault("auto_send_enabled_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["auto_send_enabled_by_target_chat"] = state
    return state


def _auto_send_tasks(application: Application) -> dict[int, asyncio.Task[Any]]:
    state = application.bot_data.setdefault("auto_send_task_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["auto_send_task_by_target_chat"] = state
    return state


def _auto_send_control_chat(application: Application) -> dict[int, int]:
    state = application.bot_data.setdefault("auto_send_control_chat_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["auto_send_control_chat_by_target_chat"] = state
    return state


def _auto_send_skip_events(application: Application) -> dict[int, asyncio.Event]:
    """asyncio.Event pro target_chat_id — wird gesetzt um aktuellen Warte-Schritt zu überspringen."""
    state = application.bot_data.setdefault("auto_send_skip_event_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["auto_send_skip_event_by_target_chat"] = state
    return state


def _auto_send_waiting_phase(application: Application) -> dict[int, str | None]:
    """Aktuelle Warte-Phase pro target_chat_id: 'reading', 'typing' oder None."""
    state = application.bot_data.setdefault("auto_send_waiting_phase_by_target_chat", {})
    if not isinstance(state, dict):
        state = {}
        application.bot_data["auto_send_waiting_phase_by_target_chat"] = state
    return state
