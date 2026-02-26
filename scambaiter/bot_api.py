from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Any

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from scambaiter.core import parse_structured_model_output
from scambaiter.forward_meta import baiter_name_from_meta, scammer_name_from_meta

_log = logging.getLogger(__name__)

# Re-export state accessors so existing ``from scambaiter.bot_api import _X`` keeps working.
from scambaiter.bot_state import (  # noqa: F401 — re-exports
    _active_targets,
    _auto_send_control_chat,
    _auto_send_enabled,
    _auto_send_skip_events,
    _auto_send_tasks,
    _auto_send_waiting_phase,
    _auto_targets,
    _drop_reply_card_state,
    _forward_card_messages,
    _forward_card_targets,
    _get_reply_card_state,
    _last_sent_by_chat,
    _last_status_message,
    _last_user_card_message,
    _manual_override_labels,
    _manual_override_requests,
    _next_reply_run_id,
    _pending_forwards,
    _prompt_card_contexts,
    _reply_card_states,
    _resolve_store,
    _sent_control_messages,
    _set_reply_card_state,
    _user_card_tasks,
)
from scambaiter.storage import StoredAnalysis

# Re-export card helpers from bot_cards.
from scambaiter.bot_cards import (  # noqa: F401 — re-exports
    RESULT_SECTION_LABELS,
    _build_raw_result_payload_from_state,
    _classify_dry_run_error,
    _compact_response_excerpt,
    _describe_parsing_error,
    _dry_run_retry_keyboard,
    _extract_action_message_text,
    _extract_error_note_from_contracts,
    _extract_partial_message_preview,
    _extract_response_debug_meta,
    _extract_textual_response_fallback,
    _find_error_context,
    _format_raw_result_snippet,
    _raw_model_output_text,
    _render_html_copy_block,
    _render_result_card_text,
    _render_result_section_actions,
    _render_result_section_analysis,
    _render_result_section_error,
    _render_result_section_message,
    _render_result_section_raw,
    _render_result_section_response,
    _reply_action_keyboard,
    _reply_error_keyboard,
    _result_card_keyboard,
    _truncate_for_card,
)

# Re-export forward helpers from bot_forward.
# Helper used for the control-chat cards.
def _analysis_lines_for_card(analysis: StoredAnalysis | None) -> list[str]:
    if analysis is None:
        return []
    notes = analysis.analysis.get("notes")
    lines: list[str] = ["Latest analysis:"]
    if analysis.title:
        lines.append(f"title: {analysis.title}")
    reason = analysis.analysis.get("reason")
    if isinstance(reason, str) and reason.strip():
        lines.append(f"reason: {reason.strip()}")
    if isinstance(notes, list) and notes:
        lines.append("notes:")
        for note in notes[:3]:
            if isinstance(note, str) and note.strip():
                lines.append(f"- {note.strip()}")
    return lines


from scambaiter.bot_forward import (  # noqa: F401 — re-exports
    _build_existing_identity_index,
    _build_forward_payload,
    _build_source_message_id,
    _clear_forward_session,
    _control_sender_info,
    _event_ts_utc_for_store,
    _extract_forward_identity,
    _extract_forward_identity_key_from_event,
    _extract_forward_identity_key_from_payload,
    _extract_forward_profile_info,
    _extract_origin_message_id,
    _extract_text,
    _flush_pending_forwards,
    _forward_card_keyboard,
    _forward_item_signature,
    _infer_event_type,
    _infer_role_from_forward,
    _infer_role_without_target,
    _infer_target_chat_id_from_forward,
    _ingest_forward_payload,
    _is_forward_message,
    _manual_alias_placeholder,
    _plan_forward_merge,
    _profile_patch_from_forward_profile,
    _render_forward_card_text,
    _resolve_target_and_role_without_active,
    _should_reuse_forward_target,
    _update_forward_card,
    ingest_forwarded_message,
)

# Re-export prompt helpers from bot_prompt.
from scambaiter.bot_prompt import (  # noqa: F401 — re-exports
    PROMPT_SECTION_LABELS,
    _extract_recent_messages,
    _extract_system_prompt,
    _load_latest_reply_payload,
    _matches_prompt_card_context,
    _memory_summary_prompt_lines,
    _normalize_memory_payload,
    _parse_prompt_event_content,
    _placeholder_for_event_type,
    _prompt_keyboard,
    _render_memory_compact,
    _render_messages_chat_window,
    _render_prompt_card_text,
    _render_prompt_section_text,
    _send_confirm_keyboard,
    _send_result_keyboard,
    _set_prompt_card_context,
    _trim_block,
)

# Re-export chat helpers from bot_chat.
from scambaiter.bot_chat import (  # noqa: F401 — re-exports
    _chat_button_label,
    _chat_card_clear_confirm_keyboard,
    _chat_card_clear_safety_keyboard,
    _chat_card_keyboard,
    _format_history_line,
    _known_chats_card_content,
    _known_chats_keyboard,
    _profile_lines_from_events,
    _profile_lines_from_stored_profile,
    _render_user_card,
    _render_whoami_text,
    _sanitize_legacy_profile_text,
    _truncate_chat_button_label,
)


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


async def _delete_control_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        # Cleanup should never break ingestion flow.
        return


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
    analysis_record = store.latest_for_chat(target_chat_id)
    analysis_lines = _analysis_lines_for_card(analysis_record)
    if analysis_lines:
        profile_lines = profile_lines + [""] + analysis_lines
    live_mode = application.bot_data.get("mode") == "live"
    auto_on = _auto_send_enabled(application).get(target_chat_id, False)
    current_phase = _auto_send_waiting_phase(application).get(target_chat_id)
    sent = await application.bot.send_message(
        chat_id=chat_id,
        text=_render_user_card(target_chat_id, len(events), last_preview, profile_lines),
        reply_markup=_chat_card_keyboard(target_chat_id, live_mode=live_mode, auto_send_on=auto_on, waiting_phase=current_phase),
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


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Callback query handlers
# ---------------------------------------------------------------------------

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
    memory = store.get_summary(chat_id=chat_id)
    total_event_count = store.count_events(chat_id)
    prompt_text = _render_prompt_section_text(
        chat_id=chat_id,
        prompt_events=prompt_events,
        model_messages=model_messages,
        latest_payload=latest_payload,
        latest_raw=latest_raw,
        latest_attempt_id=latest_attempt_id,
        latest_status=latest_status,
        section="overview",
        memory=memory,
        total_event_count=total_event_count,
    )
    sent = await message.reply_text(
        prompt_text,
        reply_markup=_prompt_keyboard(chat_id=chat_id, active_section="overview"),
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
    memory = store.get_summary(chat_id=chat_id)
    total_event_count = store.count_events(chat_id)
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
        total_event_count=total_event_count,
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


async def _run_send_task(
    application: Application,
    chat_id: int,
    parsed_output: dict,
    control_chat_id: int,
    control_msg_id: int,
    origin: str = "send",
) -> None:
    """Asynchrone Task zum Senden einer Nachricht und Speichern in History."""
    executor = application.bot_data.get("telethon_executor")
    service = application.bot_data.get("service")
    store = _resolve_store(service)
    if executor is None:
        return

    message_text = _extract_action_message_text(parsed_output)
    if not message_text:
        message_text = str((parsed_output.get("message") or {}).get("text") or "").strip()
    report = await executor.execute_actions(chat_id=chat_id, parsed_output=parsed_output)

    # Nachricht in History speichern wenn erfolgreich
    if report.ok and message_text:
        try:
            store.ingest_event(
                chat_id=chat_id, event_type="message", role="scambaiter",
                text=message_text,
                source_message_id=str(report.sent_message_id) if report.sent_message_id else None,
                meta={"origin": origin},
            )
        except Exception:
            pass

    # Tracking aktualisieren
    if report.ok and report.sent_message_id:
        _last_sent_by_chat(application)[chat_id] = {
            "message_id": int(report.sent_message_id), "attempt_id": 0,
        }

    # Antwort im Control-Chat anzeigen
    if report.ok:
        lines = [f"Sent via Telethon for /{chat_id}"]
        if report.sent_message_id:
            lines.append(f"sent_message_id: {report.sent_message_id}")
        if report.executed_actions:
            lines.extend(report.executed_actions[:12])
        result_text = "\n".join(lines)
    else:
        errors = "\n".join(report.errors) if report.errors else "unknown error"
        result_text = f"Telethon send failed.\nchat_id: /{chat_id}\n---\n{errors}"

    try:
        await application.bot.edit_message_text(
            result_text, chat_id=control_chat_id, message_id=control_msg_id,
            reply_markup=_send_result_keyboard(chat_id=chat_id),
        )
    except Exception:
        pass


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
    asyncio.create_task(_run_send_task(
        application=app, chat_id=chat_id,
        parsed_output={"message": {"text": parsed.suggestion}, "actions": parsed.actions},
        control_chat_id=int(message.chat_id), control_msg_id=int(message.message_id),
        origin="send_confirm",
    ))


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
    asyncio.create_task(_run_send_task(
        application=app, chat_id=chat_id,
        parsed_output={"message": {"text": message_text}, "actions": actions},
        control_chat_id=int(message.chat_id), control_msg_id=int(message.message_id),
        origin="telethon_send",
    ))
    _drop_reply_card_state(app, int(message.message_id))


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


# ---------------------------------------------------------------------------
# Forward handlers
# ---------------------------------------------------------------------------

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


def _cancel_auto_send_task(application: Application, target_chat_id: int) -> None:
    tasks = _auto_send_tasks(application)
    existing = tasks.pop(target_chat_id, None)
    if existing is not None and not existing.done():
        existing.cancel()
    # Phase auf None setzen (sync, keine await nötig)
    _auto_send_waiting_phase(application).pop(target_chat_id, None)
    # Keyboard aktualisieren (fire-and-forget, Fehler ignorieren)
    asyncio.create_task(_set_auto_send_phase(application, target_chat_id, None))


def _start_auto_send_task(application: Application, target_chat_id: int) -> None:
    control_chat_id = _auto_send_control_chat(application).get(target_chat_id)
    if control_chat_id is None:
        return
    task = asyncio.create_task(
        _run_auto_send_loop(application, target_chat_id, control_chat_id)
    )
    _auto_send_tasks(application)[target_chat_id] = task


def _cancel_and_restart_auto_send(application: Application, target_chat_id: int) -> None:
    if not _auto_send_enabled(application).get(target_chat_id, False):
        return
    _cancel_auto_send_task(application, target_chat_id)
    _start_auto_send_task(application, target_chat_id)


async def _skippable_sleep(seconds: float, skip_event: asyncio.Event) -> None:
    """Schläft für 'seconds' Sekunden, abbrechbar durch skip_event."""
    try:
        await asyncio.wait_for(asyncio.shield(skip_event.wait()), timeout=seconds)
        skip_event.clear()
    except asyncio.TimeoutError:
        pass


async def _set_auto_send_phase(
    application: Application, target_chat_id: int, phase: str | None, attempt_no: int | None = None
) -> None:
    """Setzt die aktuelle Warte-Phase und aktualisiert die Chat-Card-Tastatur."""
    _auto_send_waiting_phase(application)[target_chat_id] = phase
    control_chat_id = _auto_send_control_chat(application).get(target_chat_id)
    if control_chat_id is None:
        return
    card_msg_id = _last_user_card_message(application).get(control_chat_id)
    if card_msg_id is None:
        return
    mode = application.bot_data.get("mode")
    auto_on = _auto_send_enabled(application).get(target_chat_id, False)
    new_kb = _chat_card_keyboard(
        target_chat_id,
        live_mode=(mode == "live"),
        auto_send_on=auto_on,
        waiting_phase=phase,
        attempt_no=attempt_no,
    )
    try:
        await application.bot.edit_message_reply_markup(
            chat_id=control_chat_id, message_id=card_msg_id, reply_markup=new_kb
        )
    except Exception:
        pass


async def _handle_autosend_skip_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    query = update.callback_query
    if query is None:
        return
    try:
        target_chat_id = int((query.data or "").split(":")[-1])
    except ValueError:
        await query.answer()
        return
    # Guard: nur wenn eine aktive Warte-Phase läuft
    current_phase = _auto_send_waiting_phase(app).get(target_chat_id)
    if current_phase is None:
        await query.answer("Keine aktive Warte-Phase")
        return
    skip_events = _auto_send_skip_events(app)
    event = skip_events.get(target_chat_id)
    if event is not None:
        event.set()
    await query.answer("Warte-Schritt übersprungen")


async def _handle_autosend_toggle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    query = update.callback_query
    if query is None or query.message is None:
        return
    if app.bot_data.get("mode") != "live":
        await query.answer("Auto-Send nur im Live-Modus verfügbar")
        return
    try:
        target_chat_id = int((query.data or "").split(":")[-1])
    except ValueError:
        await query.answer()
        return

    enabled_map = _auto_send_enabled(app)
    currently_on = enabled_map.get(target_chat_id, False)
    new_state = not currently_on
    enabled_map[target_chat_id] = new_state

    control_chat_id = int(query.message.chat_id)
    _auto_send_control_chat(app)[target_chat_id] = control_chat_id

    if not new_state:
        _cancel_auto_send_task(app, target_chat_id)
        await query.answer("Auto-Send deaktiviert")
    else:
        await query.answer("Auto-Send aktiviert")
        # Sofort starten falls letzter Event vom Scammer
        _start_auto_send_task(app, target_chat_id)

    service = app.bot_data.get("service")
    store = _resolve_store(service)
    # Chat Card mit neuem Toggle-Status neu rendern
    try:
        await query.message.delete()
    except Exception:
        pass
    await _show_user_card(app, control_chat_id, store, target_chat_id)


async def _run_auto_send_loop(
    application: Application,
    target_chat_id: int,
    control_chat_id: int,
    max_retries: int = 7,
) -> None:
    service = application.bot_data.get("service")
    store = _resolve_store(service)
    core = getattr(service, "core", None)
    executor = application.bot_data.get("telethon_executor")
    if core is None or executor is None:
        return

    # Guard: nur senden wenn letzte Nachricht vom Scammer
    events = store.list_events(chat_id=target_chat_id, limit=5000)
    if not events or getattr(events[-1], "role", None) != "scammer":
        return

    result_card_msg: Any = None
    result_card_msg_id: int | None = None

    async def _render_auto_send_error_card(card_state: dict[str, Any]) -> None:
        nonlocal result_card_msg, result_card_msg_id
        card_state = {**card_state, "active_section": card_state.get("active_section") or "error"}
        card_text = _render_result_card_text(card_state, section="error")
        has_raw = bool(card_state.get("response_json"))
        card_markup = _result_card_keyboard(
            chat_id=target_chat_id,
            active_section=card_state["active_section"],
            status=card_state.get("status") or "error",
            telethon_enabled=True,
            retry_enabled=False,
            has_raw=has_raw,
        )
        if result_card_msg is None:
            result_card_msg = await application.bot.send_message(
                chat_id=control_chat_id, text=card_text, reply_markup=card_markup
            )
            result_card_msg_id = int(result_card_msg.message_id)
        else:
            try:
                await result_card_msg.edit_text(card_text, reply_markup=card_markup)
            except Exception:
                pass
        target_message_id = result_card_msg_id if result_card_msg_id is not None else (
            int(result_card_msg.message_id) if result_card_msg is not None else None
        )
        if target_message_id is not None:
            _set_reply_card_state(application, target_message_id, **card_state)

    # Phase 1: Lesezeit (200 Zeichen/Min = 3.33 Zeichen/Sek)
    incoming_text = getattr(events[-1], "text", None) or ""
    READ_CHARS_PER_SEC = 200.0 / 60.0
    read_seconds = max(2.0, min(len(incoming_text) / READ_CHARS_PER_SEC, 120.0))
    skip_events_map = _auto_send_skip_events(application)
    if target_chat_id not in skip_events_map:
        skip_events_map[target_chat_id] = asyncio.Event()
    skip_event = skip_events_map[target_chat_id]
    await _set_auto_send_phase(application, target_chat_id, "reading")
    skip_event.clear()
    try:
        await _skippable_sleep(read_seconds, skip_event)
    finally:
        await _set_auto_send_phase(application, target_chat_id, None)
    try:
        await executor.mark_read(target_chat_id)
    except Exception as exc:
        _log.debug("_run_auto_send_loop: mark_read fehlgeschlagen für %d: %s", target_chat_id, exc)

    for attempt_no in range(1, max_retries + 1):
        try:
            dry_run_result = None
            # -- Generierungsphase --
            await _set_auto_send_phase(application, target_chat_id, "generating", attempt_no=attempt_no)
            loop = asyncio.get_event_loop()
            dry_run_result = await loop.run_in_executor(None, core.run_hf_dry_run, target_chat_id)
            outcome_class = str(dry_run_result.get("outcome_class") or "")
            parsed_output = dry_run_result.get("parsed_output")
            valid_output = bool(dry_run_result.get("valid_output")) and isinstance(parsed_output, dict)
            error_message = str(dry_run_result.get("error_message") or "").strip()
            status = "ok" if valid_output and not error_message else "error"
            if outcome_class == "semantic_conflict":
                status = "semantic_conflict"

            if status != "ok":
                await _set_auto_send_phase(application, target_chat_id, None)
                card_state = {
                    "chat_id": target_chat_id,
                    "provider": str(dry_run_result.get("provider") or "unknown"),
                    "model": str(dry_run_result.get("model") or "unknown"),
                    "parsed_output": parsed_output if isinstance(parsed_output, dict) else None,
                    "result_text": str(dry_run_result.get("result_text") or ""),
                    "retry_context": None,
                    "run_id": _next_reply_run_id(application),
                    "status": status,
                    "outcome_class": outcome_class or "unknown",
                    "error_message": f"Auto-Send Versuch {attempt_no}/{max_retries}: {error_message or 'Generierung fehlgeschlagen'}",
                    "contract_issues": [i for i in (dry_run_result.get("contract_issues") or []) if isinstance(i, dict)],
                    "response_json": dry_run_result.get("response_json") if isinstance(dry_run_result.get("response_json"), dict) else {},
                    "conflict": dry_run_result.get("conflict") if isinstance(dry_run_result.get("conflict"), dict) else None,
                    "pivot": dry_run_result.get("pivot") if isinstance(dry_run_result.get("pivot"), dict) else None,
                    "active_section": "error",
                }
                await _render_auto_send_error_card(card_state)
                if attempt_no < max_retries:
                    await asyncio.sleep(2.0)
                continue

            # -- Ausführungsphase --
            message_text = _extract_action_message_text(parsed_output)
            actions = parsed_output.get("actions") if isinstance(parsed_output.get("actions"), list) else []
            if not actions and not message_text:
                await _set_auto_send_phase(application, target_chat_id, None)
                if attempt_no < max_retries:
                    await asyncio.sleep(2.0)
                continue

            # Phase 2: Tippzeit mit Pausen zwischen Sätzen
            await _set_auto_send_phase(application, target_chat_id, "typing")
            skip_event.clear()
            try:
                await executor.simulate_typing_with_pauses(target_chat_id, message_text, skip_event)
            finally:
                # Phase wird nach dem Senden auf None gesetzt, nicht hier
                pass

            # Phase 3: Sendet
            await _set_auto_send_phase(application, target_chat_id, "sending")
            try:
                report = await executor.execute_actions(
                    chat_id=target_chat_id,
                    parsed_output={"message": {"text": message_text}, "actions": actions},
                    skip_event=skip_event,
                )
            finally:
                await _set_auto_send_phase(application, target_chat_id, None)

            if not report.ok:
                errors = "; ".join(str(e) for e in (report.errors if hasattr(report, "errors") else []))
                run_id = _next_reply_run_id(application)
                err_state = {
                    "chat_id": target_chat_id,
                    "provider": str(dry_run_result.get("provider") or "unknown"),
                    "model": str(dry_run_result.get("model") or "unknown"),
                    "parsed_output": parsed_output,
                    "result_text": "",
                    "retry_context": None,
                    "run_id": run_id,
                    "status": "error",
                    "outcome_class": "send_failed",
                    "error_message": f"Auto-Send Versuch {attempt_no}/{max_retries}: Telethon-Fehler: {errors}",
                    "contract_issues": [],
                    "response_json": {},
                    "conflict": None,
                    "pivot": None,
                    "active_section": "error",
                }
                card_text = _render_result_card_text(err_state, section="error")
                card_markup = _result_card_keyboard(
                    chat_id=target_chat_id, active_section="error", status="error",
                    telethon_enabled=True, retry_enabled=False, has_raw=False,
                )
                if result_card_msg is None:
                    result_card_msg = await application.bot.send_message(
                        chat_id=control_chat_id, text=card_text, reply_markup=card_markup
                    )
                    result_card_msg_id = int(result_card_msg.message_id)
                else:
                    try:
                        await result_card_msg.edit_text(card_text, reply_markup=card_markup)
                    except Exception:
                        pass
                if attempt_no < max_retries:
                    await asyncio.sleep(2.0)
                continue

            # -- Erfolg --
            if message_text:
                store.ingest_event(
                    chat_id=target_chat_id,
                    event_type="message",
                    role="scambaiter",
                    text=message_text,
                    source_message_id=str(report.sent_message_id) if report.sent_message_id else None,
                    meta={"origin": "auto_send"},
                )
            if report.sent_message_id is not None:
                _last_sent_by_chat(application)[target_chat_id] = {
                    "message_id": int(report.sent_message_id),
                    "attempt_id": 0,
                }
            if result_card_msg is not None:
                try:
                    await result_card_msg.delete()
                except Exception:
                    pass
                if result_card_msg_id is not None:
                    _drop_reply_card_state(application, result_card_msg_id)
            _auto_send_tasks(application).pop(target_chat_id, None)
            return

        except asyncio.CancelledError:
            raise  # immer re-raisen!

        except Exception as exc:
            await _set_auto_send_phase(application, target_chat_id, None)
            line_state = {
                "chat_id": target_chat_id,
                "provider": "unknown",
                "model": "unknown",
                "parsed_output": None,
                "result_text": "",
                "retry_context": None,
                "run_id": _next_reply_run_id(application),
                "status": "error",
                "outcome_class": "exception",
                "error_message": f"Auto-Send Versuch {attempt_no}/{max_retries}: {exc}",
                "contract_issues": [],
                "response_json": {},
                "conflict": None,
                "pivot": None,
                "active_section": "error",
            }
            await _render_auto_send_error_card(line_state)
            if attempt_no < max_retries:
                await asyncio.sleep(2.0)

    # Alle Versuche erschöpft — Error-Card bleibt sichtbar
    _auto_send_tasks(application).pop(target_chat_id, None)


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
    app.add_handler(CallbackQueryHandler(_handle_noop_button, pattern=r"^sc:noop:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_fetch_profile_button, pattern=r"^sc:fetch_profile:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_fetch_history_button, pattern=r"^sc:fetch_history:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_autosend_toggle_button, pattern=r"^sc:autosend_toggle:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_autosend_skip_button, pattern=r"^sc:autosend_skip:[0-9]+$"))
    app.add_handler(MessageHandler(filters.REPLY, _handle_manual_override_response))
    app.add_handler(MessageHandler(filters.ALL, _handle_forward))

    # Register service callback for auto-send (Live Mode only)
    if app.bot_data.get("mode") == "live":
        service = app.bot_data.get("service")
        if service is not None and hasattr(service, "set_new_message_callback"):
            service.set_new_message_callback(
                lambda chat_id: _cancel_and_restart_auto_send(app, chat_id)
            )

    return app
