from __future__ import annotations

import asyncio
import html
import json
from datetime import datetime, timezone
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

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


def _render_html_copy_block(text: str) -> str:
    escaped = html.escape(text)
    if len(escaped) > 3200:
        escaped = escaped[:3197] + "..."
    return "<b>Message text (copy)</b>\n<pre>" + escaped + "</pre>"


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


def _render_html_error_block(
    *,
    attempt_id: int,
    chat_id: int,
    provider: str,
    model: str,
    error_message: str | None,
    result_text: str | None,
) -> str:
    title, hint = _classify_dry_run_error(error_message or "")
    reason = html.escape((error_message or "unknown error").strip())
    if len(reason) > 1200:
        reason = reason[:1197] + "..."

    excerpt = html.escape((result_text or "").strip())
    if len(excerpt) > 1400:
        excerpt = excerpt[:1397] + "..."

    lines = [
        f"<b>Dry run failed</b> (attempt #{attempt_id} for /{chat_id})",
        f"<b>provider:</b> <code>{html.escape(provider or 'unknown')}</code>",
        f"<b>model:</b> <code>{html.escape(model or 'unknown')}</code>",
        f"<b>class:</b> {html.escape(title)}",
        "",
        "<b>error</b>",
        f"<pre>{reason}</pre>",
    ]
    if excerpt:
        lines.extend([
            "<b>result excerpt</b>",
            f"<pre>{excerpt}</pre>",
        ])
    lines.extend([
        "<b>hint</b>",
        html.escape(hint),
    ])
    return "\n".join(lines)


async def _send_control_text(
    application: Application,
    message: Message,
    text: str,
    *,
    parse_mode: str | None = None,
    replace_previous_status: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
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


def _chat_card_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Prompt", callback_data=f"sc:prompt:{target_chat_id}")],
            [InlineKeyboardButton("Delete Context", callback_data=f"sc:clear_history:{target_chat_id}")],
        ]
    )


def _known_chats_keyboard(chat_ids: list[int], max_buttons: int = 30) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in chat_ids[:max_buttons]:
        rows.append([InlineKeyboardButton(f"/{item}", callback_data=f"sc:selchat:{item}")])
    return InlineKeyboardMarkup(rows)


def _chat_card_clear_confirm_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm Delete", callback_data=f"sc:clear_history_confirm:{target_chat_id}"),
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
    profile_lines = _profile_lines_from_events(events)
    sent = await application.bot.send_message(
        chat_id=chat_id,
        text=_render_user_card(target_chat_id, len(events), last_preview, profile_lines),
        reply_markup=_chat_card_keyboard(target_chat_id),
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


def _render_prompt_card_text(chat_id: int, prompt_events: list[dict[str, Any]], max_lines: int = 24) -> str:
    lines = [f"Prompt Card", f"chat_id: /{chat_id}", f"events_in_prompt: {len(prompt_events)}", "---"]
    tail = prompt_events[-max_lines:]
    for item in tail:
        role = str(item.get("role", "unknown"))
        time = str(item.get("time") or "--:--")
        text = str(item.get("text") or "")
        compact = " ".join(text.split())
        if len(compact) > 220:
            compact = compact[:217] + "..."
        lines.append(f"{time} {role}: {compact}")
    return "\n".join(lines)


PROMPT_SECTION_LABELS: dict[str, str] = {
    "prompt": "prompt",
    "schema": "schema",
    "analysis": "analysis",
    "message": "message",
    "actions": "actions",
    "raw": "raw",
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


def _prompt_keyboard(chat_id: int, active_section: str = "prompt") -> InlineKeyboardMarkup:
    def _btn(code: str) -> InlineKeyboardButton:
        label = PROMPT_SECTION_LABELS.get(code, code)
        if code == active_section:
            label = f"• {label}"
        return InlineKeyboardButton(label, callback_data=f"sc:psec:{code}:{chat_id}")

    return InlineKeyboardMarkup(
        [
            [_btn("prompt"), _btn("schema")],
            [_btn("analysis"), _btn("message")],
            [_btn("actions"), _btn("raw")],
            [
                InlineKeyboardButton("Dry Run", callback_data=f"sc:dryrun:{chat_id}"),
                InlineKeyboardButton("Delete", callback_data="sc:prompt_delete"),
            ],
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


def _render_prompt_section_text(
    *,
    chat_id: int,
    prompt_events: list[dict[str, Any]],
    model_messages: list[dict[str, str]],
    latest_payload: dict[str, Any] | None,
    latest_raw: str,
    latest_attempt_id: int | None,
    latest_status: str | None,
    section: str,
    memory: dict[str, Any] | None = None,
) -> str:
    attempt_meta = f"attempt #{latest_attempt_id}" if isinstance(latest_attempt_id, int) else "attempt ?"
    status_meta = latest_status or "unknown"
    if section == "prompt":
        payload = {
            "memory_summary": memory or {},
            "messages": model_messages,
        }
        lines = [
            "Prompt JSON",
            f"chat_id: /{chat_id}",
            f"{attempt_meta}, status={status_meta}",
            f"events_in_prompt: {len(prompt_events)}",
            "---",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
        return _trim_block("\n".join(lines))

    if latest_payload is None:
        lines = [
            "Prompt Card",
            f"chat_id: /{chat_id}",
            f"events_in_prompt: {len(prompt_events)}",
            "latest_reply: unavailable (run Dry Run)",
            "---",
        ]
        lines.extend(_memory_summary_prompt_lines(memory))
        tail = prompt_events[-12:]
        for item in tail:
            role = str(item.get("role", "unknown"))
            time = str(item.get("time") or "--:--")
            text = str(item.get("text") or "")
            compact = " ".join(text.split())
            if len(compact) > 160:
                compact = compact[:157] + "..."
            lines.append(f"{time} {role}: {compact}")
        return _trim_block("\n".join(lines))

    if section == "schema":
        schema = latest_payload.get("schema")
        keys = ", ".join(str(k) for k in latest_payload.keys())
        lines = [
            "Reply JSON Section: schema",
            f"chat_id: /{chat_id}",
            f"{attempt_meta}, status={status_meta}",
            "---",
            f"schema: {schema}",
            f"top_level_keys: {keys}",
        ]
        return _trim_block("\n".join(lines))

    if section == "analysis":
        analysis = latest_payload.get("analysis")
        if isinstance(analysis, dict):
            body = json.dumps(analysis, ensure_ascii=False, indent=2)
        else:
            body = str(analysis)
        lines = [
            "Reply JSON Section: analysis",
            f"chat_id: /{chat_id}",
            f"{attempt_meta}, status={status_meta}",
            "---",
            body,
        ]
        return _trim_block("\n".join(lines))

    if section == "message":
        message_obj = latest_payload.get("message")
        text_value = ""
        if isinstance(message_obj, dict):
            text_value = str(message_obj.get("text") or "")
        lines = [
            "Reply JSON Section: message",
            f"chat_id: /{chat_id}",
            f"{attempt_meta}, status={status_meta}",
            "---",
            text_value or "(empty)",
        ]
        return _trim_block("\n".join(lines))

    if section == "actions":
        actions = latest_payload.get("actions")
        lines = [
            "Reply JSON Section: actions",
            f"chat_id: /{chat_id}",
            f"{attempt_meta}, status={status_meta}",
            "---",
        ]
        if isinstance(actions, list) and actions:
            for idx, action in enumerate(actions, start=1):
                if isinstance(action, dict):
                    lines.append(f"[{idx}] " + json.dumps(action, ensure_ascii=False))
                else:
                    lines.append(f"[{idx}] {action}")
        else:
            lines.append("(none)")
        return _trim_block("\n".join(lines))

    # raw
    lines = [
        "Reply JSON Section: raw",
        f"chat_id: /{chat_id}",
        f"{attempt_meta}, status={status_meta}",
        "---",
        latest_raw or "(empty)",
    ]
    return _trim_block("\n".join(lines))


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
        section="schema",
        memory=memory,
    )
    sent = await message.reply_text(
        prompt_text,
        reply_markup=_prompt_keyboard(chat_id=chat_id, active_section="schema"),
    )
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

    confirm_text = (
        "Delete stored chat context?\n"
        f"chat_id: /{chat_id}\n"
        "This deletes events, analyses, directives, attempts and profile data for this chat."
    )
    try:
        await query.edit_message_text(
            confirm_text,
            reply_markup=_chat_card_clear_confirm_keyboard(chat_id),
        )
    except Exception:
        await message.reply_text(confirm_text, reply_markup=_chat_card_clear_confirm_keyboard(chat_id))
    await query.answer("Confirm deletion")


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
    except Exception:
        await message.reply_text(
            prompt_text,
            reply_markup=_prompt_keyboard(chat_id=chat_id, active_section=section),
        )
    await query.answer(f"Section: {PROMPT_SECTION_LABELS.get(section, section)}")


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
    store = getattr(service, "store", None)
    if core is None or store is None:
        await query.answer("Service unavailable")
        return
    await query.answer("Dry run started")
    provider = "huggingface_openai_compat"
    model = str((getattr(core.config, "hf_model", None) or "").strip())
    prompt_json: dict[str, Any] = {}
    response_json: dict[str, Any] = {}
    parsed_output: dict[str, Any] | None = None
    result_text = ""
    status = "ok"
    error_message: str | None = None
    attempt_records: list[dict[str, Any]] = []
    try:
        dry_run_result = core.run_hf_dry_run(chat_id)
        provider = str(dry_run_result.get("provider") or provider)
        model = str(dry_run_result.get("model") or model)
        prompt_json = dry_run_result.get("prompt_json") if isinstance(dry_run_result.get("prompt_json"), dict) else {}
        response_json = dry_run_result.get("response_json") if isinstance(dry_run_result.get("response_json"), dict) else {}
        parsed_output = dry_run_result.get("parsed_output") if isinstance(dry_run_result.get("parsed_output"), dict) else None
        result_text = str(dry_run_result.get("result_text") or "")
        valid_output = bool(dry_run_result.get("valid_output"))
        error_message = str(dry_run_result.get("error_message") or "").strip() or None
        records = dry_run_result.get("attempts")
        if isinstance(records, list):
            attempt_records = [item for item in records if isinstance(item, dict)]
        if (not valid_output) or error_message:
            status = "error"
    except Exception as exc:
        status = "error"
        error_message = str(exc)

    base_attempt_no = store.next_attempt_no(chat_id)
    saved_attempt = None
    if attempt_records:
        for idx, rec in enumerate(attempt_records):
            rec_prompt = rec.get("prompt_json") if isinstance(rec.get("prompt_json"), dict) else prompt_json
            rec_response = rec.get("response_json") if isinstance(rec.get("response_json"), dict) else response_json
            rec_text = str(rec.get("result_text") or "")
            rec_status = str(rec.get("status") or ("ok" if bool(rec.get("accepted")) else "invalid"))
            rec_error = rec.get("error_message")
            if rec_error is not None:
                rec_error = str(rec_error)
            rec_phase = str(rec.get("phase") or "initial")
            rec_accepted = bool(rec.get("accepted"))
            rec_reject_reason = rec.get("reject_reason")
            if rec_reject_reason is not None:
                rec_reject_reason = str(rec_reject_reason)
            saved_attempt = store.save_generation_attempt(
                chat_id=chat_id,
                provider=provider,
                model=model or "unknown",
                prompt_json=rec_prompt,
                response_json=rec_response,
                result_text=rec_text,
                status=rec_status,
                error_message=rec_error,
                attempt_no=base_attempt_no + idx,
                phase=rec_phase,
                accepted=rec_accepted,
                reject_reason=rec_reject_reason,
            )
    else:
        saved_attempt = store.save_generation_attempt(
            chat_id=chat_id,
            provider=provider,
            model=model or "unknown",
            prompt_json=prompt_json,
            response_json=response_json,
            result_text=result_text,
            status=status,
            error_message=error_message,
            attempt_no=base_attempt_no,
            phase="initial",
            accepted=(status == "ok"),
            reject_reason=None if status == "ok" else "provider_error",
        )

    attempt = saved_attempt

    if status == "ok":
        if isinstance(parsed_output, dict):
            message_obj = parsed_output.get("message") if isinstance(parsed_output.get("message"), dict) else {}
            message_text = str(message_obj.get("text") or "").strip()
            actions = parsed_output.get("actions") if isinstance(parsed_output.get("actions"), list) else []
            analysis = parsed_output.get("analysis") if isinstance(parsed_output.get("analysis"), dict) else {}

            action_labels: list[str] = []
            for idx, action in enumerate(actions, start=1):
                if isinstance(action, dict):
                    action_type = str(action.get("type") or "unknown")
                    action_labels.append(f"{idx}. {action_type}")
            action_block = "\n".join(action_labels) if action_labels else "(none)"

            analysis_keys = sorted(str(key) for key in analysis.keys())
            analysis_preview = ", ".join(analysis_keys[:8]) if analysis_keys else "(none)"
            if len(analysis_keys) > 8:
                analysis_preview += ", ..."

            summary_lines = [
                f"Dry run saved as attempt #{attempt.id} for /{chat_id}",
                f"schema: {parsed_output.get('metadata', {}).get('schema', 'unknown')}",
                f"actions: {len(actions)}",
                f"analysis_keys: {analysis_preview}",
                "",
                "Action queue:",
                action_block,
            ]
            await _send_control_text(
                application=app,
                message=message,
                text="\n".join(summary_lines),
            )

            if message_text:
                copy_block = _render_html_copy_block(message_text)
                await _send_control_text(
                    application=app,
                    message=message,
                    text=copy_block,
                    parse_mode=ParseMode.HTML,
                    replace_previous_status=False,
                )
        else:
            preview = (result_text or "<empty-result>").strip()
            if len(preview) > 500:
                preview = preview[:497] + "..."
            await _send_control_text(
                application=app,
                message=message,
                text=f"Dry run saved as attempt #{attempt.id} for /{chat_id}\n{preview}",
            )
    else:
        error_block = _render_html_error_block(
            attempt_id=attempt.id,
            chat_id=chat_id,
            provider=provider,
            model=model or "unknown",
            error_message=error_message,
            result_text=result_text,
        )
        await _send_control_text(
            application=app,
            message=message,
            text=error_block,
            parse_mode=ParseMode.HTML,
        )


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


def _is_forward_message(message: Message) -> bool:
    return bool(getattr(message, "forward_origin", None))


def _infer_event_type(message: Message) -> str:
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


def _build_source_message_id(message: Message) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return f"control:{message.chat_id}:{message.message_id}"
    date_value = getattr(origin, "date", None)
    date_part = ""
    if isinstance(date_value, datetime):
        date_part = date_value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    sender_user = getattr(origin, "sender_user", None)
    sender_chat = getattr(origin, "sender_chat", None)
    chat_id = getattr(sender_chat, "id", "") if sender_chat is not None else ""
    user_id = getattr(sender_user, "id", "") if sender_user is not None else ""
    message_id = getattr(origin, "message_id", "")
    kind = type(origin).__name__
    return f"fwd:{kind}:{chat_id}:{user_id}:{message_id}:{date_part}"


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


def _event_ts_utc_for_store(message: Message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    origin_date = getattr(origin, "date", None) if origin is not None else None
    origin_message_id = getattr(origin, "message_id", None) if origin is not None else None
    if not isinstance(origin_date, datetime):
        return None
    # Some forwards only expose forward-time or coarse time; treat those as unknown.
    if origin_message_id in (None, ""):
        return None
    if origin_date == message.date:
        return None
    return origin_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _infer_target_chat_id_from_forward(message: Message) -> int | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    sender_chat = getattr(origin, "sender_chat", None)
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


def _infer_role_from_forward(message: Message, target_chat_id: int) -> str:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return "manual"
    sender_chat = getattr(origin, "sender_chat", None)
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
        if auto_target_chat_id is None:
            return None, "manual"
        return auto_target_chat_id, "manual"
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
    source_message_id = _build_source_message_id(message)
    meta: dict[str, Any] = {
        "control_chat_id": int(message.chat_id),
        "control_message_id": int(message.message_id),
        "forward_profile": _extract_forward_profile_info(message),
    }
    control_sender = _control_sender_info(message)
    if control_sender:
        meta["control_sender"] = control_sender
    return {
        "event_type": event_type,
        "source_message_id": source_message_id,
        "role": role,
        "text": _extract_text(message),
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
    record = store.ingest_user_forward(
        chat_id=target_chat_id,
        event_type=str(payload["event_type"]),
        text=payload.get("text"),
        source_message_id=str(payload["source_message_id"]),
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
    ingested = _flush_pending_forwards(
        application=app,
        store=store,
        control_chat_id=int(message.chat_id),
        target_chat_id=target_chat_id,
    )
    if ingested:
        await _send_control_text(
            application=app,
            message=message,
            text=f"Active target chat set to {target_chat_id}. Imported {ingested} buffered forwards.",
        )
    else:
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
    shown = chat_ids[:30]
    extra = len(chat_ids) - len(shown)
    title = f"Known chat ids ({len(chat_ids)} total):\nSelect one:"
    if extra > 0:
        title += f"\n(showing first {len(shown)}, {extra} hidden)"
    await _send_control_text(
        application=app,
        message=message,
        text=title,
        reply_markup=_known_chats_keyboard(chat_ids),
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
    role: str
    if target_chat_id is not None:
        role = _infer_role_from_forward(message, target_chat_id=target_chat_id)
    else:
        auto_target_chat_id = auto_targets.get(control_chat_id)
        target_chat_id, role = _resolve_target_and_role_without_active(
            message=message,
            control_user_id=control_user_id,
            auto_target_chat_id=auto_target_chat_id,
        )
        if target_chat_id is None:
            pending = _pending_forwards(app)
            queue = pending.setdefault(control_chat_id, [])
            queue.append(_build_forward_payload(message, role="manual"))
            await _send_control_text(
                application=app,
                message=message,
                text=f"Buffered {len(queue)} manual forward(s). Forward a scammer message from same chat or set /chat <chat_id>.",
            )
            await _delete_control_message(message)
            return
        if role == "scammer":
            auto_targets[control_chat_id] = target_chat_id
    state[control_chat_id] = target_chat_id
    store = _resolve_store(app.bot_data["service"])
    imported = _flush_pending_forwards(
        application=app,
        store=store,
        control_chat_id=control_chat_id,
        target_chat_id=target_chat_id,
    )
    payload = _build_forward_payload(message, role=role)
    record = _ingest_forward_payload(store=store, target_chat_id=target_chat_id, payload=payload)
    if imported:
        await _send_control_text(
            application=app,
            message=message,
            text=f"Imported {imported} buffered forward(s). Ingested #{record.id} as {record.event_type}/{record.role} for chat {target_chat_id}.",
        )
    else:
        await _send_control_text(
            application=app,
            message=message,
            text=f"Ingested #{record.id} as {record.event_type}/{record.role} for chat {target_chat_id}.",
        )
    _schedule_user_card_update(
        application=app,
        control_chat_id=control_chat_id,
        store=store,
        target_chat_id=target_chat_id,
        delay_seconds=1.0,
    )
    await _delete_control_message(message)


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


def create_bot_app(token: str, service: Any, allowed_chat_id: int | None = None) -> Any:
    app = Application.builder().token(token).build()
    app.bot_data["service"] = service
    app.bot_data["allowed_chat_id"] = allowed_chat_id
    app.bot_data["register_command_menu"] = lambda: _register_command_menu(app)
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("whoami", _cmd_whoami))
    app.add_handler(CommandHandler("chat", _cmd_chat))
    app.add_handler(CommandHandler("chats", _cmd_chats))
    app.add_handler(CommandHandler("history", _cmd_history))
    app.add_handler(MessageHandler(filters.Regex(r"^/(?:c)?[0-9]+(?:\s.*)?$"), _cmd_chat_id_shortcut))
    app.add_handler(CallbackQueryHandler(_handle_select_chat_button, pattern=r"^sc:selchat:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_prompt_button, pattern=r"^sc:prompt:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_clear_history_button, pattern=r"^sc:clear_history:[0-9]+$"))
    app.add_handler(
        CallbackQueryHandler(_handle_clear_history_confirm_button, pattern=r"^sc:clear_history_confirm:[0-9]+$")
    )
    app.add_handler(
        CallbackQueryHandler(_handle_clear_history_cancel_button, pattern=r"^sc:clear_history_cancel:[0-9]+$")
    )
    app.add_handler(CallbackQueryHandler(_handle_prompt_section_button, pattern=r"^sc:psec:(?:schema|analysis|message|actions|raw):[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_dry_run_button, pattern=r"^sc:dryrun:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(_handle_prompt_delete_button, pattern=r"^sc:prompt_delete$"))
    app.add_handler(MessageHandler(filters.ALL, _handle_forward))
    return app
