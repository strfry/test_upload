from __future__ import annotations

import json
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from scambaiter.service import BackgroundService, MessageState, PendingMessage


def create_bot_app(token: str, service: BackgroundService, allowed_chat_id: int) -> Application:
    app = Application.builder().token(token).build()
    max_message_len = 3500
    menu_message_len = 3900
    card_caption_len = 1024
    chats_page_size = 8
    queue_page_size = 8
    infobox_targets: dict[int, set[tuple[int, int, int, str]]] = {}
    control_cards: dict[str, tuple[int, int]] = {}

    def _authorized(update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.id == allowed_chat_id)

    async def _guarded_reply(update: Update, text: str) -> None:
        if not _authorized(update):
            if update.message:
                await update.message.reply_text("Nicht autorisiert.")
            return
        if update.message:
            await update.message.reply_text(text)

    async def _guarded_reply_chunks(update: Update, text: str) -> None:
        if len(text) <= max_message_len:
            await _guarded_reply(update, text)
            return
        lines = text.splitlines(keepends=True)
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > max_message_len and chunk:
                await _guarded_reply(update, chunk.rstrip())
                chunk = line
            else:
                chunk += line
        if chunk:
            await _guarded_reply(update, chunk.rstrip())

    def _limit_caption(text: str) -> str:
        if len(text) <= card_caption_len:
            return text
        return text[: card_caption_len - 14].rstrip() + "\n... [gekuerzt]"

    async def _safe_edit_message(query, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        message = getattr(query, "message", None)
        has_photo = bool(getattr(message, "photo", None)) if message else False
        try:
            if has_photo:
                await query.edit_message_caption(caption=_limit_caption(text), reply_markup=reply_markup)
            else:
                await query.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

    def _limit_message(text: str, max_len: int = menu_message_len) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 14].rstrip() + "\n... [gekuerzt]"

    def _drop_control_card_by_message(chat_id: int, message_id: int) -> None:
        for key, value in list(control_cards.items()):
            if value == (chat_id, message_id):
                control_cards.pop(key, None)

    async def _post_control_card(
        kind: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        bring_to_front: bool = True,
    ):
        existing = control_cards.get(kind)
        if existing:
            existing_chat_id, existing_message_id = existing
            if not bring_to_front:
                try:
                    await app.bot.edit_message_text(
                        chat_id=existing_chat_id,
                        message_id=existing_message_id,
                        text=_limit_message(text),
                        reply_markup=reply_markup,
                    )
                    return existing_chat_id, existing_message_id
                except BadRequest as exc:
                    lowered = str(exc).lower()
                    if "message to edit not found" not in lowered and "message can't be edited" not in lowered:
                        raise
                    control_cards.pop(kind, None)
            else:
                try:
                    await app.bot.delete_message(chat_id=existing_chat_id, message_id=existing_message_id)
                except BadRequest:
                    pass
                control_cards.pop(kind, None)

        message = await app.bot.send_message(
            chat_id=allowed_chat_id,
            text=_limit_message(text),
            reply_markup=reply_markup,
        )
        control_cards[kind] = (int(message.chat_id), int(message.message_id))
        return int(message.chat_id), int(message.message_id)

    def _truncate_value(value: str, max_len: int = 60) -> str:
        value = value.strip()
        if len(value) <= max_len:
            return value
        return value[: max_len - 3].rstrip() + "..."

    def _analysis_summary_parts(analysis_data: dict[str, object] | None, limit: int = 4) -> list[str]:
        if not analysis_data:
            return []
        parts: list[str] = []
        for key, value in analysis_data.items():
            if isinstance(value, (dict, list)):
                continue
            rendered = _truncate_value(str(value), max_len=40)
            parts.append(f"{key}={rendered}")
            if len(parts) >= limit:
                break
        return parts

    def _has_send_message(actions: list[dict[str, object]] | None) -> bool:
        return any(str(action.get("type", "")).strip().lower() == "send_message" for action in (actions or []))

    def _planned_send_at_utc(actions: list[dict[str, object]] | None) -> str | None:
        for action in actions or []:
            if str(action.get("type", "")).strip().lower() != "send_message":
                continue
            value = action.get("send_at_utc")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _display_queue_fallback(suggestion: str | None) -> list[dict[str, object]]:
        text = (suggestion or "").strip()
        if not text:
            return []
        typing_seconds = max(6.0, min(28.0, round(len(text) / 45.0, 1)))
        return [
            {"type": "mark_read"},
            {"type": "simulate_typing", "duration_seconds": typing_seconds},
            {"type": "send_message"},
        ]

    def _format_chat_overview(
        chat_id: int,
        title: str,
        updated_at: object,
        analysis_data: dict[str, object] | None,
        suggestion: str | None,
        pending: PendingMessage | None,
        display_actions: list[dict[str, object]] | None,
    ) -> str:
        lines = [f"{title} ({chat_id})"]
        if hasattr(updated_at, "strftime"):
            lines.append(f"Zuletzt: {updated_at:%Y-%m-%d %H:%M}")
        lines.append(f"Auto-Senden: {'AN' if service.is_chat_auto_enabled(chat_id) else 'AUS'}")
        if pending:
            lines.append(_format_pending_state(pending))
        planned_send_at = _planned_send_at_utc(display_actions)
        if planned_send_at:
            lines.append(f"Geplantes Senden: {planned_send_at}")
        if suggestion and not (pending and _has_send_message(pending.action_queue)):
            lines.append("Vorschlag: " + _truncate_value(suggestion, max_len=900))
        parts = _analysis_summary_parts(analysis_data)
        if parts:
            lines.append("Analysis: " + ", ".join(parts))
        return "\n".join(lines)

    def _parse_chat_id_arg(args: list[str]) -> int | None:
        if len(args) != 1:
            return None
        try:
            return int(args[0].strip())
        except ValueError:
            return None

    def _chat_detail_keyboard(chat_id: int, page: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Generate", callback_data=f"ma:g:{chat_id}:{page}"),
                    InlineKeyboardButton("Queue Run", callback_data=f"ma:q:{chat_id}:{page}"),
                    InlineKeyboardButton("Stop", callback_data=f"ma:x:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("Auto an", callback_data=f"ma:on:{chat_id}:{page}"),
                    InlineKeyboardButton("Auto aus", callback_data=f"ma:off:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("Bilder", callback_data=f"ma:i:{chat_id}:{page}"),
                    InlineKeyboardButton("Analysis", callback_data=f"ma:k:{chat_id}:{page}"),
                    InlineKeyboardButton("Prompt", callback_data=f"ma:p:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("Zurueck", callback_data=f"ml:{page}"),
                    InlineKeyboardButton("Aktualisieren", callback_data=f"mc:{chat_id}:{page}"),
                ],
            ]
        )

    def _state_icon(chat_id: int) -> str:
        pending = service.get_pending_message(chat_id)
        if not pending:
            return "A"
        icons = {
            MessageState.GENERATING: "G",
            MessageState.WAITING: "W",
            MessageState.SENDING_TYPING: "S",
            MessageState.SENT: "OK",
            MessageState.ESCALATED: "H",
            MessageState.CANCELLED: "X",
            MessageState.ERROR: "!",
        }
        return icons.get(pending.state, "?")

    def _get_known_chats_cached(limit: int = 80):
        known_chats = service.list_known_chats(limit=limit)
        if known_chats:
            return known_chats
        if service.store:
            return service.store.list_known_chats(limit=limit)
        return []

    def _kick_background_sync() -> None:
        service.start_known_chats_refresh()
        service.start_folder_prefetch()

    def _find_known_chat(chat_id: int):
        for item in service.list_known_chats(limit=500):
            if item.chat_id == chat_id:
                return item
        return None

    def _chats_menu_text(known_chats: list, page: int) -> str:
        total_pages = max(1, math.ceil(len(known_chats) / chats_page_size))
        page = max(0, min(page, total_pages - 1))
        start = page * chats_page_size
        current_slice = known_chats[start : start + chats_page_size]
        lines = [f"Chats ({len(known_chats)}) | Seite {page + 1}/{total_pages}", "Status: A=kein Prozess, G/W/S/OK/H/X/!"]
        if not current_slice:
            lines.append("Noch keine Chats im Cache. Hintergrund-Sync laeuft, bitte Refresh druecken.")
            return "\n".join(lines)
        for index, item in enumerate(current_slice, start=start + 1):
            stamp = item.updated_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"{index}. [{_state_icon(item.chat_id)}] {item.title} ({item.chat_id}) | {stamp}")
        lines.append("Waehle einen Chat ueber die Buttons unten.")
        return _limit_message("\n".join(lines))

    def _chats_menu_keyboard(known_chats: list, page: int) -> InlineKeyboardMarkup:
        total_pages = max(1, math.ceil(len(known_chats) / chats_page_size))
        page = max(0, min(page, total_pages - 1))
        start = page * chats_page_size
        current_slice = known_chats[start : start + chats_page_size]
        rows: list[list[InlineKeyboardButton]] = []
        for item in current_slice:
            label = f"[{_state_icon(item.chat_id)}] {_truncate_value(item.title, max_len=28)}"
            rows.append([InlineKeyboardButton(label, callback_data=f"mc:{item.chat_id}:{page}")])
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("<<", callback_data=f"ml:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(">>", callback_data=f"ml:{page + 1}"))
        if nav_row:
            rows.append(nav_row)
        rows.append(
            [
                InlineKeyboardButton("Refresh", callback_data=f"ml:{page}"),
                InlineKeyboardButton("Queue", callback_data="mq:0"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _queue_menu_text(page: int) -> str:
        pending_items = service.list_pending_messages()
        total_pages = max(1, math.ceil(len(pending_items) / queue_page_size))
        page = max(0, min(page, total_pages - 1))
        start = page * queue_page_size
        current_slice = pending_items[start : start + queue_page_size]

        lines = [f"Queue ({len(pending_items)}) | Seite {page + 1}/{total_pages}"]
        if not current_slice:
            lines.append("Keine aktiven Prozesse in der Queue.")
            return _limit_message("\n".join(lines))

        for index, item in enumerate(current_slice, start=start + 1):
            schema = _truncate_value(item.schema, max_len=24) if item.schema else "-"
            queue_lines: list[str] = []
            for q_idx, action in enumerate((item.action_queue or []), start=1):
                queue_lines.append(f"{q_idx}. {_truncate_value(_format_action(action, item.suggestion), max_len=130)}")
            actions = " | ".join(queue_lines) if queue_lines else "-"
            error_part = ""
            if item.last_error:
                error_part = " | error: " + _truncate_value(item.last_error, max_len=90)
            lines.append(
                f"{index}. [{_state_icon(item.chat_id)}] {item.title} ({item.chat_id}) | "
                f"{item.state.value}{error_part} | Actions: {actions} | schema: {schema}"
            )

        lines.append("Mit den Chat-Buttons unten in die Detailansicht springen.")
        return _limit_message("\n".join(lines))

    def _queue_menu_keyboard(page: int) -> InlineKeyboardMarkup:
        pending_items = service.list_pending_messages()
        total_pages = max(1, math.ceil(len(pending_items) / queue_page_size))
        page = max(0, min(page, total_pages - 1))
        start = page * queue_page_size
        current_slice = pending_items[start : start + queue_page_size]

        rows: list[list[InlineKeyboardButton]] = []
        for item in current_slice:
            label = f"[{_state_icon(item.chat_id)}] {_truncate_value(item.title, max_len=28)}"
            rows.append([InlineKeyboardButton(label, callback_data=f"mc:{item.chat_id}:0")])

        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("<<", callback_data=f"mq:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(">>", callback_data=f"mq:{page + 1}"))
        if nav_row:
            rows.append(nav_row)
        rows.append(
            [
                InlineKeyboardButton("Queue Refresh", callback_data=f"mq:{page}"),
                InlineKeyboardButton("Zu Chats", callback_data="ml:0"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _format_pending_state(pending: PendingMessage | None) -> str:
        if not pending:
            return "Status: Kein Prozesszustand vorhanden."
        state_labels = {
            MessageState.GENERATING: "Vorschlag wird erzeugt",
            MessageState.WAITING: "Wartephase",
            MessageState.SENDING_TYPING: "Sendephase (Tippen)",
            MessageState.SENT: "Gesendet",
            MessageState.ESCALATED: "Menschliche Eskalation",
            MessageState.CANCELLED: "Abgebrochen",
            MessageState.ERROR: "Fehler",
        }
        label = state_labels.get(pending.state, pending.state.value)
        lines = [f"Status: {label}"]
        if pending.wait_until is None and pending.state == MessageState.WAITING:
            lines.append("Wartezeit: unbegrenzt")
        elif pending.wait_until is not None and pending.state == MessageState.WAITING:
            lines.append(f"Wartezeit bis: {pending.wait_until:%Y-%m-%d %H:%M:%S}")
        if pending.sent_message_id is not None:
            lines.append(f"Gesendete msg_id: {pending.sent_message_id}")
        if pending.last_error:
            lines.append(f"Fehler: {pending.last_error}")
        if pending.current_action_label:
            progress = "-"
            if pending.current_action_index and pending.current_action_total:
                progress = f"{pending.current_action_index}/{pending.current_action_total}"
            line = f"Aktueller Schritt: {progress} {pending.current_action_label}"
            if pending.current_action_until is not None:
                line += f" | bis {pending.current_action_until:%Y-%m-%d %H:%M:%S}"
            lines.append(line)
        if pending.state == MessageState.ESCALATED and pending.escalation_reason:
            lines.append(f"Frage: {pending.escalation_reason}")
        action_queue = pending.action_queue or []
        if action_queue:
            lines.append("Actions:")
            for idx, action in enumerate(action_queue, start=1):
                lines.append(f"{idx}. {_format_action(action, pending.suggestion)}")
        else:
            lines.append("Actions: -")
        lines.append(f"Trigger: {pending.trigger or '-'}")
        return "\n".join(lines)

    def _pending_state_short(pending: PendingMessage | None) -> str:
        if not pending:
            return "Kein Prozess"
        state_labels = {
            MessageState.GENERATING: "Generierung",
            MessageState.WAITING: "Wartephase",
            MessageState.SENDING_TYPING: "Senden",
            MessageState.SENT: "Gesendet",
            MessageState.ESCALATED: "Eskalation",
            MessageState.CANCELLED: "Abgebrochen",
            MessageState.ERROR: "Fehler",
        }
        label = state_labels.get(pending.state, pending.state.value)
        action_queue = pending.action_queue or []
        action_hint = f", Actions={len(action_queue)}" if action_queue else ", Actions=0"
        current = ""
        if pending.current_action_label:
            current = f", Step={pending.current_action_label}"
            if pending.current_action_until is not None:
                current += f" bis {pending.current_action_until:%H:%M:%S}"
        if pending.state == MessageState.WAITING and pending.wait_until is not None:
            return f"{label} bis {pending.wait_until:%H:%M:%S}{action_hint}{current} | Trigger: {pending.trigger or '-'}"
        return f"{label}{action_hint}{current} | Trigger: {pending.trigger or '-'}"

    def _format_action(action: dict[str, object], suggestion: str | None = None) -> str:
        action_type = str(action.get("type", "?"))
        if action_type == "send_message":
            send_at = action.get("send_at_utc")
            send_at_part = f", send_at_utc={send_at}" if isinstance(send_at, str) and send_at.strip() else ""
            text = _truncate_value(suggestion or "", max_len=240) if suggestion else "-"
            return f"send_message (text={text}{send_at_part})"
        fields = [f"{k}={v}" for k, v in action.items() if k != "type"]
        if not fields:
            return action_type
        rendered = ", ".join(_truncate_value(str(item), max_len=48) for item in fields)
        return f"{action_type} ({rendered})"

    async def _render_chat_detail(
        chat_id: int,
        page: int,
        heading: str | None = None,
        compact: bool = False,
    ) -> tuple[str, InlineKeyboardMarkup]:
        known = _find_known_chat(chat_id)
        pending = service.get_pending_message(chat_id)
        title = known.title if known else (pending.title if pending else str(chat_id))
        updated_at = known.updated_at if known else (pending.created_at if pending else None)

        latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
        analysis_data = latest_entry.analysis if latest_entry else None

        suggestion = pending.suggestion if (pending and pending.suggestion) else (latest_entry.suggestion if latest_entry else None)
        display_actions = (pending.action_queue if pending else None) or (latest_entry.actions if latest_entry else None)
        if not display_actions:
            display_actions = _display_queue_fallback(suggestion)
        if compact:
            lines: list[str] = []
            if heading:
                lines.append(_truncate_value(heading, max_len=180))
            lines.append(f"{_truncate_value(title, max_len=64)} ({chat_id})")
            if hasattr(updated_at, "strftime"):
                lines.append(f"Zuletzt: {updated_at:%Y-%m-%d %H:%M}")
            lines.append(f"Status: {_pending_state_short(pending)}")
            lines.append(f"Auto: {'AN' if service.is_chat_auto_enabled(chat_id) else 'AUS'}")
            planned_send_at = _planned_send_at_utc(display_actions)
            if planned_send_at:
                lines.append(f"Sendet um: {planned_send_at}")
            info_parts = _analysis_summary_parts(analysis_data, limit=4)
            if info_parts:
                lines.append("Analysis: " + ", ".join(info_parts))
            if display_actions:
                lines.append("Queue:")
                source_text = pending.suggestion if pending else suggestion
                for idx, action in enumerate(display_actions, start=1):
                    lines.append(f"{idx}. {_truncate_value(_format_action(action, source_text), max_len=220)}")
            if suggestion and not _has_send_message(display_actions):
                lines.append("Vorschlag: " + _truncate_value(suggestion, max_len=180))
            text = _limit_caption("\n".join(lines))
        else:
            body = _format_chat_overview(chat_id, title, updated_at, analysis_data, suggestion, pending, display_actions)
            if display_actions:
                queue_lines = ["Queue:"]
                source_text = pending.suggestion if pending else suggestion
                for idx, action in enumerate(display_actions, start=1):
                    queue_lines.append(f"{idx}. {_format_action(action, source_text)}")
                body = body + "\n" + "\n".join(queue_lines)
            text = f"{heading}\n\n{body}" if heading else body
            text = _limit_message(text)
        return text, _chat_detail_keyboard(chat_id, page)

    async def _send_chat_detail_card(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        page: int,
        heading: str | None = None,
    ):
        caption, keyboard = await _render_chat_detail(
            chat_id,
            page,
            heading=heading,
            compact=True,
        )
        # Telegram photo captions are heavily limited; fall back to text card when content is long.
        if len(caption) > card_caption_len:
            return await context.bot.send_message(
                chat_id=allowed_chat_id,
                text=_limit_message(caption),
                reply_markup=keyboard,
            )
        profile_photo = await service.core.get_chat_profile_photo(chat_id)
        if profile_photo:
            return await context.bot.send_photo(
                chat_id=allowed_chat_id,
                photo=InputFile(profile_photo, filename=f"profile_{chat_id}.jpg"),
                caption=caption,
                reply_markup=keyboard,
            )
        return await context.bot.send_message(
            chat_id=allowed_chat_id,
            text=caption,
            reply_markup=keyboard,
        )

    def _remove_message_target(chat_id: int, message_id: int) -> None:
        _drop_control_card_by_message(chat_id, message_id)
        for target_chat_id in list(infobox_targets.keys()):
            targets = infobox_targets[target_chat_id]
            filtered = {entry for entry in targets if not (entry[0] == chat_id and entry[1] == message_id)}
            if filtered:
                infobox_targets[target_chat_id] = filtered
            else:
                infobox_targets.pop(target_chat_id, None)

    async def _register_infobox_target(message, chat_id: int, page: int, render_mode: str) -> None:
        if not message:
            return

        message_chat_id = int(message.chat_id)
        message_id = int(message.message_id)
        _remove_message_target(message_chat_id, message_id)

        new_target = (message_chat_id, message_id, page, render_mode)
        previous_targets = infobox_targets.get(chat_id, set())
        stale_targets = [target for target in previous_targets if target[:2] != new_target[:2]]

        for stale_chat_id, stale_message_id, _stale_page, _stale_mode in stale_targets:
            try:
                await app.bot.delete_message(chat_id=stale_chat_id, message_id=stale_message_id)
            except BadRequest as exc:
                error_text = str(exc).lower()
                if "message to delete not found" in error_text or "message can't be deleted" in error_text:
                    continue
                raise

        infobox_targets[chat_id] = {new_target}

    async def _push_infobox_update(chat_id: int, heading: str = "ℹ️ Prozess-Update") -> None:
        targets = infobox_targets.get(chat_id)
        if not targets:
            return
        stale: set[tuple[int, int, int, str]] = set()
        for target_chat_id, target_message_id, page, render_mode in list(targets):
            compact = render_mode == "card"
            text, reply_markup = await _render_chat_detail(
                chat_id, page, heading=heading, compact=compact
            )
            try:
                if render_mode == "card":
                    await app.bot.edit_message_caption(
                        chat_id=target_chat_id,
                        message_id=target_message_id,
                        caption=_limit_caption(text),
                        reply_markup=reply_markup,
                    )
                else:
                    await app.bot.edit_message_text(
                        chat_id=target_chat_id,
                        message_id=target_message_id,
                        text=text,
                        reply_markup=reply_markup,
                    )
            except BadRequest as exc:
                message = str(exc).lower()
                if "message is not modified" in message:
                    continue
                if "message to edit not found" in message or "message can't be edited" in message:
                    stale.add((target_chat_id, target_message_id, page, render_mode))
                    continue
                raise
        for target in stale:
            targets.discard(target)
        if not targets:
            infobox_targets.pop(chat_id, None)

    def _on_pending_changed(event_chat_id: int, _pending: PendingMessage | None) -> None:
        pending = service.get_pending_message(event_chat_id)
        if pending and pending.state == MessageState.ESCALATED and not pending.escalation_notified:
            if service.mark_escalation_notified(event_chat_id):
                async def _send_escalation() -> None:
                    reason = (pending.escalation_reason or "").strip() or "Keine Begründung angegeben."
                    text = (
                        f"🧑‍💼 Human Escalation\n"
                        f"Chat: {pending.title} ({event_chat_id})\n\n"
                        f"Frage:\n{reason}"
                    )
                    message_chat_id, message_id = await _post_control_card(
                        kind=f"escalation:{event_chat_id}",
                        text=_limit_message(text, max_len=3000),
                        reply_markup=_chat_detail_keyboard(event_chat_id, 0),
                        bring_to_front=True,
                    )
                    class _Msg:
                        def __init__(self, c_id: int, m_id: int) -> None:
                            self.chat_id = c_id
                            self.message_id = m_id
                    await _register_infobox_target(_Msg(message_chat_id, message_id), event_chat_id, 0, render_mode="text")
                app.create_task(_send_escalation(), update=None)
        if event_chat_id in infobox_targets:
            app.create_task(_push_infobox_update(event_chat_id), update=None)

    def _on_warning(message: str) -> None:
        async def _post_warning() -> None:
            text = "⚠️ Warnung\n" + message
            await _post_control_card(
                kind="warning",
                text=_limit_message(text, max_len=3000),
                bring_to_front=True,
            )

        app.create_task(_post_warning(), update=None)

    service.add_pending_listener(_on_pending_changed)
    service.add_warning_listener(_on_warning)

    async def run_once(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        target_chat_ids: set[int] | None = None
        if context.args:
            target_chat_ids = set()
            for arg in context.args:
                for token in arg.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    try:
                        target_chat_ids.add(int(token))
                    except ValueError:
                        await _guarded_reply(
                            update,
                            "Ungültige Chat-ID. Nutzung: /runonce oder /runonce <chat_id[,chat_id2,...]>",
                        )
                        return

        if target_chat_ids:
            await _guarded_reply(update, f"Starte Einmaldurchlauf für {len(target_chat_ids)} Chat-ID(s)...")
        else:
            await _guarded_reply(update, "Starte Einmaldurchlauf...")
        summary = await service.run_once(target_chat_ids=target_chat_ids)
        await _guarded_reply(update, f"Fertig. Chats: {summary.chat_count}, gesendet: {summary.sent_count}")

    async def chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return

        _kick_background_sync()
        known_chats = _get_known_chats_cached(limit=80)
        text = _chats_menu_text(known_chats, page=0)
        keyboard = _chats_menu_keyboard(known_chats, page=0)
        if update.message:
            await update.message.reply_text(text, reply_markup=keyboard)

    async def callback_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not update.effective_chat or update.effective_chat.id != allowed_chat_id:
            await _safe_edit_message(query, "Nicht autorisiert.")
            return

        data = query.data or ""

        if data.startswith("ml:"):
            try:
                page = int(data.split(":")[1])
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            _kick_background_sync()
            known_chats = _get_known_chats_cached(limit=80)
            text = _chats_menu_text(known_chats, page=page)
            keyboard = _chats_menu_keyboard(known_chats, page=page)
            message = getattr(query, "message", None)
            if message:
                _remove_message_target(int(message.chat_id), int(message.message_id))
            if message and getattr(message, "photo", None):
                await context.bot.send_message(chat_id=allowed_chat_id, text=text, reply_markup=keyboard)
                try:
                    await context.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                except BadRequest:
                    pass
            else:
                await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if data.startswith("mq:"):
            try:
                page = int(data.split(":")[1])
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            text = _queue_menu_text(page)
            keyboard = _queue_menu_keyboard(page)
            message = getattr(query, "message", None)
            if message and getattr(message, "photo", None):
                await context.bot.send_message(chat_id=allowed_chat_id, text=text, reply_markup=keyboard)
                try:
                    await context.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                except BadRequest:
                    pass
            else:
                await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if data.startswith("mc:"):
            try:
                _prefix, chat_id_raw, page_raw = data.split(":")
                chat_id = int(chat_id_raw)
                page = int(page_raw)
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            message = getattr(query, "message", None)
            if message and getattr(message, "photo", None):
                text, keyboard = await _render_chat_detail(chat_id, page, compact=True)
                if len(text) > card_caption_len:
                    replacement = await context.bot.send_message(
                        chat_id=allowed_chat_id,
                        text=_limit_message(text),
                        reply_markup=keyboard,
                    )
                    try:
                        await context.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                    except BadRequest:
                        pass
                    await _register_infobox_target(replacement, chat_id, page, render_mode="text")
                else:
                    await _safe_edit_message(query, text, reply_markup=keyboard)
                    await _register_infobox_target(message, chat_id, page, render_mode="card")
                return
            card_message = await _send_chat_detail_card(
                context,
                chat_id=chat_id,
                page=page,
            )
            await _register_infobox_target(card_message, chat_id, page, render_mode="card")
            return

        try:
            if not data.startswith("ma:"):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            _prefix, action, chat_id_raw, page_raw = data.split(":")
            chat_id = int(chat_id_raw)
            page = int(page_raw)
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Ungueltige Aktion.")
            return

        if action == "g":
            keyboard = _chat_detail_keyboard(chat_id, page)
            await _safe_edit_message(query, f"Chat {chat_id}\n\nStarte Generierung...", reply_markup=keyboard)

            runtime_warnings: list[str] = []

            def _on_run_warning(event_chat_id: int, message: str) -> None:
                if event_chat_id == chat_id:
                    runtime_warnings.append(message)

            try:
                summary = await service.run_once(target_chat_ids={chat_id}, on_warning=_on_run_warning)
            except Exception as exc:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading=f"Fehler bei Generierung: {exc}",
                    compact=bool(getattr(getattr(query, "message", None), "photo", None)),
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return

            heading = "Generierung abgeschlossen"
            if runtime_warnings:
                heading = heading + f" | Warnung: {runtime_warnings[-1]}"
            heading = heading + (
                f" | Chats: {summary.chat_count}, Gesendet: {summary.sent_count}, Zeit: {summary.finished_at:%Y-%m-%d %H:%M:%S}"
            )
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            text, keyboard = await _render_chat_detail(
                chat_id, page, heading=heading, compact=is_card
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            message = getattr(query, "message", None)
            if message:
                await _register_infobox_target(message, chat_id, page, render_mode=("card" if is_card else "text"))
            return

        if action == "on":
            service.set_chat_auto(chat_id, enabled=True)
            pending = service.get_pending_message(chat_id)
            if not pending:
                known = _find_known_chat(chat_id)
                await service.schedule_suggestion_generation(
                    chat_id=chat_id,
                    title=(known.title if known else str(chat_id)),
                    trigger="bot-auto-on",
                    auto_send=False,
                )
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Auto-Senden fuer diesen Chat aktiviert.",
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            message = getattr(query, "message", None)
            if message:
                await _register_infobox_target(message, chat_id, page, render_mode=("card" if is_card else "text"))
            return

        if action == "off":
            service.set_chat_auto(chat_id, enabled=False)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Auto-Senden fuer diesen Chat deaktiviert.",
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "q":
            started = await service.trigger_send(chat_id, trigger="bot-queue-run-button")
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not started:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine ausführbare Queue vorhanden.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Queue-Ausführung ausgelöst (bis zum derzeit sichtbaren Queue-Ende).",
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            message = getattr(query, "message", None)
            if message:
                await _register_infobox_target(message, chat_id, page, render_mode=("card" if is_card else "text"))
            return

        if action == "x":
            result = await service.abort_send(chat_id, trigger="bot-stop-button")
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            text, keyboard = await _render_chat_detail(
                chat_id, page, heading=result, compact=is_card
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "i":
            keyboard = _chat_detail_keyboard(chat_id, page)
            await _safe_edit_message(query, f"Chat {chat_id}\n\nLade letzte Bilder...", reply_markup=keyboard)
            images = await service.core.get_recent_images_with_captions_for_control_channel(chat_id, limit=3)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not images:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine Bilder gefunden oder keine Caption erzeugt.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            for image_bytes, caption in images:
                await context.bot.send_photo(
                    chat_id=allowed_chat_id,
                    photo=InputFile(image_bytes, filename=f"chat_{chat_id}.jpg"),
                    caption=caption[:1024],
                )
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading=f"{len(images)} Bild(er) mit Caption im Kontrollkanal gepostet.",
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "k":
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not service.store:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine Datenbank konfiguriert.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            latest_entry = service.store.latest_for_chat(chat_id)
            if not latest_entry or not latest_entry.analysis:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine Analysis gespeichert.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            rendered = _limit_message(json.dumps(latest_entry.analysis, ensure_ascii=False, indent=2), max_len=2200)
            text, keyboard = await _render_chat_detail(
                chat_id, page, heading="Analysis-Objekt", compact=is_card
            )
            if is_card:
                text = _limit_caption(text + "\n" + rendered[:700])
            else:
                text = _limit_message(text + "\n\n" + rendered)
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "p":
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            chat_context = await service.core.build_chat_context(chat_id)
            if not chat_context:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Kein Chatverlauf gefunden.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            preview = service.core.build_prompt_debug_summary(chat_context, max_lines=8)
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Prompt-Preview",
                compact=is_card,
            )
            if is_card:
                text = _limit_caption(text + "\n" + _truncate_value(preview, max_len=700))
            else:
                text = _limit_message(text + "\n\nPrompt-Preview:\n" + preview)
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        await _safe_edit_message(query, "Unbekannte Aktion.")

    async def last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.last_results:
            await _guarded_reply(update, "Keine Analyse vorhanden.")
            return
        lines: list[str] = []
        for result in service.last_results[:5]:
            lines.append(f"- {result.context.title} ({result.context.chat_id}): {result.suggestion}")
        await _guarded_reply(update, "Letzte Vorschlaege:\n" + "\n".join(lines))

    async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        entries = service.store.latest(limit=5)
        if not entries:
            await _guarded_reply(update, "Keine gespeicherten Analysen vorhanden.")
            return
        blocks: list[str] = []
        for item in entries:
            parts = [f"- {item.created_at:%Y-%m-%d %H:%M} | {item.title} ({item.chat_id})"]
            if item.metadata:
                parts.append("Meta=" + ",".join(f"{k}={v}" for k, v in item.metadata.items()))
            if item.analysis:
                parts.append("Analyse=" + json.dumps(item.analysis, ensure_ascii=False))
            blocks.append("\n".join(parts))
        await _guarded_reply_chunks(update, "Persistierte Analysen:\n\n" + "\n\n".join(blocks))

    async def analysis_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        if len(context.args) != 1:
            await _guarded_reply(update, "Nutzung: /analysisget <scammer_chat_id>")
            return
        try:
            scammer_chat_id = int(context.args[0])
        except ValueError:
            await _guarded_reply(update, "scammer_chat_id muss eine Zahl sein.")
            return
        entry = service.store.latest_for_chat(scammer_chat_id)
        if not entry:
            await _guarded_reply(update, "Keine Analyse fuer diesen Chat gefunden.")
            return
        analysis_json = json.dumps(entry.analysis or {}, ensure_ascii=False, indent=2)
        await _guarded_reply_chunks(update, f"Analyse fuer {scammer_chat_id}:\n{analysis_json}")

    async def analysis_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        if len(context.args) < 2:
            await _guarded_reply(update, "Nutzung: /analysisset <scammer_chat_id> <json_objekt>")
            return
        try:
            scammer_chat_id = int(context.args[0])
        except ValueError:
            await _guarded_reply(update, "scammer_chat_id muss eine Zahl sein.")
            return
        raw_json = " ".join(context.args[1:]).strip()
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            await _guarded_reply(update, f"Ungueltiges JSON: {exc}")
            return
        if not isinstance(parsed, dict):
            await _guarded_reply(update, "analysis muss ein JSON-Objekt sein.")
            return
        updated = service.store.update_latest_analysis(scammer_chat_id, parsed)
        if not updated:
            await _guarded_reply(update, "Keine Analyse fuer diesen Chat gefunden.")
            return
        await _guarded_reply(update, f"Analyse fuer {scammer_chat_id} aktualisiert.")

    async def prompt_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        chat_id = _parse_chat_id_arg(context.args)
        if chat_id is None:
            await _guarded_reply(update, "Nutzung: /promptpreview <scammer_chat_id>")
            return

        chat_context = await service.core.build_chat_context(chat_id)
        if not chat_context:
            await _guarded_reply(update, "Kein Chatverlauf gefunden.")
            return

        preview = service.core.build_prompt_debug_summary(chat_context, max_lines=8)
        await _guarded_reply_chunks(update, "Prompt-Preview:\n" + preview)

    async def send_start_menu() -> None:
        _kick_background_sync()
        known_chats = _get_known_chats_cached(limit=80)
        text = _chats_menu_text(known_chats, page=0)
        keyboard = _chats_menu_keyboard(known_chats, page=0)
        await _post_control_card(
            kind="menu:chats",
            text=text,
            reply_markup=keyboard,
            bring_to_front=True,
        )

    app.bot_data["send_start_menu"] = send_start_menu

    app.add_handler(CommandHandler("runonce", run_once))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("analysisget", analysis_get))
    app.add_handler(CommandHandler("analysisset", analysis_set))
    app.add_handler(CommandHandler("promptpreview", prompt_preview))
    app.add_handler(CallbackQueryHandler(callback_action, pattern=r"^(ml|mq|mc|ma|generate|send|stop|autoon|autooff|img|kv):"))
    return app
