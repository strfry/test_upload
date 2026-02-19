from __future__ import annotations

import base64
import json
import math
import re

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from scambaiter.service import BackgroundService, MessageState, PendingMessage


def create_bot_app(token: str, service: BackgroundService, allowed_chat_id: int) -> Application:
    app = Application.builder().token(token).build()
    max_message_len = 3500
    menu_message_len = 3900
    card_caption_len = 900
    chats_page_size = 8
    queue_page_size = 8
    directive_input_state = 1
    analysis_input_state = 2
    directive_context_messages_key = "directive_context_messages"
    analysis_context_messages_key = "analysis_context_messages"
    analysis_reply_kb_keyvalue = "🧩 key=value"
    analysis_reply_kb_json = "🧱 JSON merge"
    analysis_reply_kb_cancel = "❌ Abbrechen"
    directive_reply_kb_cancel = "❌ Abbrechen"
    infobox_targets: dict[int, set[tuple[int, int, int, str]]] = {}
    control_cards: dict[str, tuple[int, int]] = {}
    prompt_preview_cache: dict[int, tuple[int, list[str]]] = {}
    directive_delete_batches: dict[str, list[tuple[int, int]]] = {}
    directive_delete_batch_seq = {"value": 0}
    anti_loop_preset_text = (
        "Allgemeine Anti-Loop-Regel: "
        "Ermittle pro Turn den Kern-Intent der letzten Assistant-Nachricht "
        "(analysis.last_assistant_intent) und vergleiche ihn mit dem geplanten neuen Intent. "
        "Wenn identisch oder semantisch gleich in den letzten 2 Assistant-Turns, ist Wiederholung verboten. "
        "In diesem Fall muss die nächste Nachricht zuerst auf die jüngste User-Aussage reagieren und "
        "einen neuen Fortschrittsschritt liefern (neues Subziel, neues Belegdetail oder neue konkrete Aktion). "
        "Setze in analysis: loop_guard_active=true, repeated_intent=<intent>, next_intent=<new_intent>, "
        "blocked_intents_next_turns=[<intent>] für 2 Turns. "
        "Wenn User sagt, dass ein Nachweis nicht verfügbar ist (z.B. nicht auf der Website), "
        "darf genau dieser Nachweis nicht erneut als Hauptfrage angefordert werden; "
        "stattdessen alternatives verifizierbares Detail aus vorhandenem Material anfordern."
    )
    role_consistency_preset_text = (
        "Rollenkonsistenz: Sender und Empfaenger niemals verwechseln. "
        "Nicht behaupten, dass ich einen Link oder ein Dokument gesendet habe, wenn es vom User kam. "
        "Vor Versand pruefen: Wer lieferte die letzte relevante Information, und stimmen "
        "ich/du sowie mein/dein-Referenzen mit dem Verlauf ueberein."
    )
    last_user_priority_preset_text = (
        "Last-User-Priority: Die naechste Nachricht muss primaer auf die juengste User-Aussage reagieren "
        "und darf nicht auf ein altes Hauptthema zurueckfallen."
    )
    no_repeat_public_page_preset_text = (
        "Wenn User sagt, dass bestimmte Details nicht auf der Website stehen, dieselbe Frage nach "
        "oeffentlicher Seite/Link/fee page nicht erneut als Hauptfrage stellen. "
        "Stattdessen ein anderes verifizierbares Detail anfordern."
    )
    contract_detail_drilldown_preset_text = (
        "Bei fehlender Website-Transparenz auf Vertragsdetails wechseln: "
        "genau eine konkrete Klausel/Zeile anfordern (Gebuehr, Laufzeit, Auszahlung, Kuendigung, Haftung), "
        "nicht erneut allgemeine Link-Anfrage."
    )
    no_premature_registration_preset_text = (
        "Keine fruehe Zustimmung zu Registrierung/Einzahlung. "
        "Wenn User zum sofortigen Registrieren draengt, zuerst ein konkretes pruefbares Detail anfordern "
        "und den naechsten Schritt klar absichern."
    )
    analysis_memory_preset_text = (
        "Analysis-Memory nutzen: Bei Wiederholungsgefahr in analysis setzen "
        "loop_guard_active=true, repeated_intent, next_intent und blocked_intents_next_turns (2 Turns)."
    )

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

    def _track_context_message(
        context: ContextTypes.DEFAULT_TYPE,
        storage_key: str,
        message_obj: object | None,
    ) -> None:
        if message_obj is None:
            return
        chat_id = getattr(message_obj, "chat_id", None)
        message_id = getattr(message_obj, "message_id", None)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        items = context.user_data.get(storage_key)
        if not isinstance(items, list):
            items = []
            context.user_data[storage_key] = items
        items.append((chat_id, message_id))

    async def _cleanup_context_messages(
        context: ContextTypes.DEFAULT_TYPE,
        storage_key: str,
    ) -> None:
        items = context.user_data.pop(storage_key, None)
        if not isinstance(items, list):
            return
        for chat_id, message_id in items:
            if not isinstance(chat_id, int) or not isinstance(message_id, int):
                continue
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except BadRequest:
                pass

    def _tg_units(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def _trim_to_tg_units(text: str, max_units: int) -> str:
        if max_units <= 0:
            return ""
        if _tg_units(text) <= max_units:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _tg_units(text[:mid]) <= max_units:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]

    def _is_caption_too_long_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "media_caption_too_long" in msg
            or "caption is too long" in msg
            or "message caption is too long" in msg
        )

    def _limit_caption(text: str) -> str:
        suffix = "\n... [gekuerzt]"
        if _tg_units(text) <= card_caption_len:
            return text
        budget = card_caption_len - _tg_units(suffix)
        trimmed = _trim_to_tg_units(text, budget).rstrip()
        return trimmed + suffix

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
            if has_photo and message and _is_caption_too_long_error(exc):
                replacement = await app.bot.send_message(
                    chat_id=int(message.chat_id),
                    text=_limit_message(text),
                    reply_markup=reply_markup,
                )
                try:
                    await app.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                except BadRequest:
                    pass
                _drop_control_card_by_message(int(message.chat_id), int(message.message_id))
                _drop_control_card_by_message(int(replacement.chat_id), int(replacement.message_id))
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

    def _paginate_text(text: str, max_len: int) -> list[str]:
        clean = text.strip()
        if not clean:
            return [""]
        if len(clean) <= max_len:
            return [clean]
        lines = clean.splitlines(keepends=True)
        pages: list[str] = []
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > max_len and chunk:
                pages.append(chunk.rstrip())
                chunk = line
            else:
                chunk += line
        if chunk:
            pages.append(chunk.rstrip())
        return pages or [clean[:max_len]]

    async def _send_paged_text(
        bot,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        page_len: int = max_message_len,
    ):
        pages = _paginate_text(text, max_len=page_len)
        first_message = None
        for idx, page in enumerate(pages):
            message = await bot.send_message(
                chat_id=chat_id,
                text=page,
                reply_markup=(reply_markup if idx == 0 else None),
            )
            if first_message is None:
                first_message = message
        return first_message

    def _prompt_preview_keyboard(chat_id: int, chat_page: int, page: int, total: int) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️", callback_data=f"pp:{chat_id}:{chat_page}:{page - 1}"))
        if page < total - 1:
            nav_row.append(InlineKeyboardButton("▶️", callback_data=f"pp:{chat_id}:{chat_page}:{page + 1}"))
        if nav_row:
            rows.append(nav_row)
        rows.append([InlineKeyboardButton("⬅️ Zurueck", callback_data=f"mc:{chat_id}:{chat_page}")])
        return InlineKeyboardMarkup(rows)

    async def _render_prompt_preview(
        chat_id: int,
        chat_page: int,
        page: int,
        is_card: bool,
    ) -> tuple[str, InlineKeyboardMarkup]:
        cached = prompt_preview_cache.get(chat_id)
        if not cached:
            return "Prompt-Preview nicht verfügbar.", _prompt_preview_keyboard(chat_id, chat_page, 0, 1)
        cached_chat_page, pages = cached
        effective_chat_page = cached_chat_page if isinstance(cached_chat_page, int) else chat_page
        total = max(1, len(pages))
        page = max(0, min(page, total - 1))
        header = f"Prompt-Preview {page + 1}/{total} | Chat {chat_id}"
        body = pages[page]
        text = header + "\n\n" + body
        if is_card:
            text = _limit_caption(text)
        else:
            text = _limit_message(text)
        return text, _prompt_preview_keyboard(chat_id, effective_chat_page, page, total)

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

    def _directive_preview_parts(chat_id: int, limit: int = 2) -> list[str]:
        directives = service.list_chat_directives(chat_id, active_only=True, limit=limit)
        return [f"#{item.id}: {_truncate_value(item.text, max_len=60)}" for item in directives]

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
        directive_parts = _directive_preview_parts(chat_id, limit=2)
        if directive_parts:
            lines.append("Direktiven: " + " | ".join(directive_parts))
        retry_lines = _generation_attempt_lines(chat_id, limit=3)
        if retry_lines:
            lines.append("Retries:")
            lines.extend(retry_lines)
        return "\n".join(lines)

    def _parse_chat_id_arg(args: list[str]) -> int | None:
        if len(args) != 1:
            return None
        try:
            return int(args[0].strip())
        except ValueError:
            return None

    def _history_overview_lines(limit: int = 8) -> list[str]:
        if not service.store:
            return []
        known = service.store.list_known_chats(limit=limit)
        lines: list[str] = []
        for item in known:
            latest = service.store.latest_for_chat(item.chat_id)
            if not latest:
                continue
            schema = latest.metadata.get("schema", "-") if isinstance(latest.metadata, dict) else "-"
            action_count = len(latest.actions or [])
            lines.append(
                _truncate_value(
                    f"- {latest.created_at:%Y-%m-%d %H:%M:%S} | {latest.title} ({latest.chat_id}) | "
                    f"schema={schema} | actions={action_count}",
                    max_len=260,
                )
            )
            parts = _analysis_summary_parts(latest.analysis, limit=4)
            if parts:
                lines.append(_truncate_value("  Analysis: " + ", ".join(parts), max_len=260))
            if latest.suggestion:
                lines.append(_truncate_value("  Suggestion: " + latest.suggestion, max_len=260))
        return lines

    def _chat_detail_keyboard(chat_id: int, page: int) -> InlineKeyboardMarkup:
        pending = service.get_pending_message(chat_id)
        is_running = bool(pending and pending.state == MessageState.SENDING_TYPING)
        auto_enabled = service.is_chat_auto_enabled(chat_id)
        run_label = "⏭️ Skip" if is_running else "▶️ Queue Run"
        run_action = "sk" if is_running else "q"
        auto_on_label = "🟢 Auto an" if auto_enabled else "⚪ Auto an"
        auto_off_label = "⚪ Auto aus" if auto_enabled else "🔴 Auto aus"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🧠 Generate", callback_data=f"ma:g:{chat_id}:{page}"),
                    InlineKeyboardButton(run_label, callback_data=f"ma:{run_action}:{chat_id}:{page}"),
                    InlineKeyboardButton("⏹️ Stop", callback_data=f"ma:x:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton(auto_on_label, callback_data=f"ma:on:{chat_id}:{page}"),
                    InlineKeyboardButton(auto_off_label, callback_data=f"ma:off:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("🖼️ Bilder", callback_data=f"ma:i:{chat_id}:{page}"),
                    InlineKeyboardButton("📊 Analysis", callback_data=f"ma:k:{chat_id}:{page}"),
                    InlineKeyboardButton("🧾 Prompt", callback_data=f"ma:p:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("🔁 Retries", callback_data=f"ma:r:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("➕ Direktive aktivieren", callback_data=f"ma:da:{chat_id}:{page}"),
                    InlineKeyboardButton("🗂️ Direktiven anzeigen", callback_data=f"ma:dl:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("✏️ An Edit", callback_data=f"ma:ae:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("⬅️ Zurueck", callback_data=f"ml:{page}"),
                    InlineKeyboardButton("🔄 Aktualisieren", callback_data=f"mc:{chat_id}:{page}"),
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
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"ml:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"ml:{page + 1}"))
        if nav_row:
            rows.append(nav_row)
        rows.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"ml:{page}"),
                InlineKeyboardButton("📦 Queue", callback_data="mq:0"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("🧠 Run Once", callback_data="mt:runonce"),
                InlineKeyboardButton("🔁 Retries", callback_data="mt:retries"),
                InlineKeyboardButton("📚 History", callback_data="mt:history"),
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
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"mq:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"mq:{page + 1}"))
        if nav_row:
            rows.append(nav_row)
        rows.append(
            [
                InlineKeyboardButton("🔄 Queue Refresh", callback_data=f"mq:{page}"),
                InlineKeyboardButton("💬 Zu Chats", callback_data="ml:0"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _directive_preset_catalog() -> list[tuple[str, str]]:
        return [
            ("Anti-Loop (General)", anti_loop_preset_text),
            ("Role Consistency", role_consistency_preset_text),
            ("Latest User First", last_user_priority_preset_text),
            ("No Re-Ask Public Page", no_repeat_public_page_preset_text),
            ("Contract Detail Drilldown", contract_detail_drilldown_preset_text),
            ("No Early Registration", no_premature_registration_preset_text),
            ("Analysis Loop Memory", analysis_memory_preset_text),
        ]

    def _directive_preset_by_index(index: int) -> tuple[str, str] | None:
        presets = _directive_preset_catalog()
        if index < 1 or index > len(presets):
            return None
        return presets[index - 1]

    def _directive_preset_keyboard(chat_id: int, page: int, preset_index: int, dryrun_running: bool = False) -> InlineKeyboardMarkup:
        dryrun_label = "⏳ Dry-Run läuft" if dryrun_running else "Dry-Run"
        dryrun_cb = f"md:busy:{chat_id}:{page}:{preset_index}" if dryrun_running else f"md:pd:{chat_id}:{page}:{preset_index}"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Add", callback_data=f"md:pa:{chat_id}:{page}:{preset_index}"),
                    InlineKeyboardButton(dryrun_label, callback_data=dryrun_cb),
                    InlineKeyboardButton("Once", callback_data=f"md:po:{chat_id}:{page}:{preset_index}"),
                ]
            ]
        )

    async def _build_directive_presets(chat_id: int, limit: int = 7) -> list[tuple[str, str]]:
        _ = chat_id
        presets = _directive_preset_catalog()
        return presets[: max(1, min(limit, 7))]

    def _encode_key_token(key: str) -> str:
        raw = key.encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _decode_key_token(token: str) -> str | None:
        clean = token.strip()
        if not clean:
            return None
        padding = "=" * ((4 - len(clean) % 4) % 4)
        try:
            return base64.urlsafe_b64decode((clean + padding).encode("ascii")).decode("utf-8")
        except Exception:
            return None

    def _analysis_delete_keyboard(chat_id: int, page: int) -> InlineKeyboardMarkup:
        latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
        analysis_data = latest_entry.analysis if latest_entry else None
        rows: list[list[InlineKeyboardButton]] = []
        if isinstance(analysis_data, dict):
            for key in list(analysis_data.keys())[:12]:
                key_name = str(key)
                token = _encode_key_token(key_name)
                label = f"Loeschen {key_name}"
                rows.append([InlineKeyboardButton(_truncate_value(label, max_len=34), callback_data=f"me:del:{chat_id}:{page}:{token}")])
        rows.append([InlineKeyboardButton("⬅️ Zurueck", callback_data=f"mc:{chat_id}:{page}")])
        return InlineKeyboardMarkup(rows)

    def _analysis_editor_keyboard(chat_id: int, page: int) -> InlineKeyboardMarkup:
        latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
        analysis_data = latest_entry.analysis if latest_entry else None
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton("➕ Neu", callback_data=f"ma:ak:{chat_id}:{page}"),
                InlineKeyboardButton("🗑️ Loeschen", callback_data=f"ma:ad:{chat_id}:{page}"),
            ]
        ]
        if isinstance(analysis_data, dict):
            for key in list(analysis_data.keys())[:10]:
                key_name = str(key)
                token = _encode_key_token(key_name)
                rows.append(
                    [
                        InlineKeyboardButton(
                            _truncate_value(f"Edit {key_name}", max_len=34),
                            callback_data=f"me:edit:{chat_id}:{page}:{token}",
                        )
                    ]
                )
        rows.append([InlineKeyboardButton("⬅️ Zurueck", callback_data=f"mc:{chat_id}:{page}")])
        return InlineKeyboardMarkup(rows)

    def _analysis_demo_keyboard(
        chat_id: int,
        page: int,
        mode: str,
        keys: list[str],
        key_offset: int,
        page_size: int = 6,
    ) -> InlineKeyboardMarkup:
        normalized_mode = mode if mode in {"edit", "delete"} else "edit"
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton("➕ Neu", callback_data=f"ma:ak:{chat_id}:{page}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"ax:edit:{chat_id}:{page}:0"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"ax:delete:{chat_id}:{page}:0"),
            ]
        ]

        start = max(0, key_offset)
        total = len(keys)
        current = keys[start : start + page_size]
        for key_name in current:
            token = _encode_key_token(key_name)
            if normalized_mode == "delete":
                label = _truncate_value(f"Del {key_name}", max_len=30)
                callback = f"me:del:{chat_id}:{page}:{token}"
            else:
                label = _truncate_value(f"Edit {key_name}", max_len=30)
                callback = f"me:edit:{chat_id}:{page}:{token}"
            rows.append([InlineKeyboardButton(label, callback_data=callback)])

        nav_row: list[InlineKeyboardButton] = []
        if start > 0:
            prev_offset = max(0, start - page_size)
            nav_row.append(InlineKeyboardButton("⬅️ Keys", callback_data=f"ax:{normalized_mode}:{chat_id}:{page}:{prev_offset}"))
        if start + page_size < total:
            next_offset = start + page_size
            nav_row.append(InlineKeyboardButton("➡️ Keys", callback_data=f"ax:{normalized_mode}:{chat_id}:{page}:{next_offset}"))
        if nav_row:
            rows.append(nav_row)

        rows.append([InlineKeyboardButton("⬅️ Zurueck", callback_data=f"mc:{chat_id}:{page}")])
        return InlineKeyboardMarkup(rows)

    def _analysis_demo_text(chat_id: int, mode: str, keys: list[str], key_offset: int, page_size: int = 6) -> str:
        normalized_mode = mode if mode in {"edit", "delete"} else "edit"
        mode_label = "Edit" if normalized_mode == "edit" else "Delete"
        total = len(keys)
        start = max(0, key_offset)
        end = min(total, start + page_size)
        lines = [
            f"Chat {chat_id}",
            "",
            "Analysis Editor Demo (Inline Buttons)",
            f"Modus: {mode_label}",
            f"Keys: {total} | Seite: {((start // page_size) + 1) if total else 1}",
            "",
            "Buttons testen:",
            "- `Neu` startet key=value / JSON Eingabe",
            "- `Edit/Delete` schaltet den Modus",
            "- Key-Buttons arbeiten direkt auf vorhandenen Keys",
        ]
        if total:
            lines.append("")
            lines.append("Sichtbare Keys:")
            for key_name in keys[start:end]:
                lines.append(f"- {key_name}")
        else:
            lines.append("")
            lines.append("Noch keine Analysis-Keys vorhanden.")
        return _limit_message("\n".join(lines))

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

    def _generation_attempt_lines(chat_id: int, limit: int = 3) -> list[str]:
        attempts = service.list_generation_attempts(chat_id, limit=limit)
        lines: list[str] = []
        for item in attempts:
            status = "ok" if item.accepted else "reject"
            reason = f", reason={item.reject_reason}" if item.reject_reason else ""
            lines.append(
                _truncate_value(
                    f"{item.created_at:%H:%M:%S} a{item.attempt_no}/{item.phase} {status}{reason}",
                    max_len=140,
                )
            )
        return lines

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
            directive_parts = _directive_preview_parts(chat_id, limit=2)
            if directive_parts:
                lines.append("Direktiven: " + " | ".join(directive_parts))
            retry_lines = _generation_attempt_lines(chat_id, limit=3)
            if retry_lines:
                lines.append("Retries:")
                lines.extend(retry_lines)
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
            try:
                return await context.bot.send_photo(
                    chat_id=allowed_chat_id,
                    photo=InputFile(profile_photo, filename=f"profile_{chat_id}.jpg"),
                    caption=_limit_caption(caption),
                    reply_markup=keyboard,
                )
            except BadRequest as exc:
                if not _is_caption_too_long_error(exc):
                    raise
                return await _send_paged_text(
                    context.bot,
                    chat_id=allowed_chat_id,
                    text=_limit_message(caption),
                    reply_markup=keyboard,
                    page_len=menu_message_len,
                )
        return await context.bot.send_message(
            chat_id=allowed_chat_id,
            text=caption,
            reply_markup=keyboard,
        )

    async def _send_chat_detail_via_app(
        chat_id: int,
        page: int,
        heading: str | None,
        preferred_render_mode: str,
    ):
        prefer_card = preferred_render_mode == "card"
        if prefer_card:
            caption, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading=heading,
                compact=True,
            )
            if len(caption) <= card_caption_len:
                profile_photo = await service.core.get_chat_profile_photo(chat_id)
                if profile_photo:
                    try:
                        message = await app.bot.send_photo(
                            chat_id=allowed_chat_id,
                            photo=InputFile(profile_photo, filename=f"profile_{chat_id}.jpg"),
                            caption=_limit_caption(caption),
                            reply_markup=keyboard,
                        )
                        return message, "card"
                    except BadRequest as exc:
                        if not _is_caption_too_long_error(exc):
                            raise
            message = await app.bot.send_message(
                chat_id=allowed_chat_id,
                text=_limit_message(caption),
                reply_markup=keyboard,
            )
            return message, "text"

        text, keyboard = await _render_chat_detail(
            chat_id,
            page,
            heading=heading,
            compact=False,
        )
        message = await app.bot.send_message(
            chat_id=allowed_chat_id,
            text=_limit_message(text),
            reply_markup=keyboard,
        )
        return message, "text"

    def _remove_message_target(chat_id: int, message_id: int) -> None:
        _drop_control_card_by_message(chat_id, message_id)
        for target_chat_id in list(infobox_targets.keys()):
            targets = infobox_targets[target_chat_id]
            filtered = {entry for entry in targets if not (entry[0] == chat_id and entry[1] == message_id)}
            if filtered:
                infobox_targets[target_chat_id] = filtered
            else:
                infobox_targets.pop(target_chat_id, None)

    def _is_infobox_message(chat_id: int, message_id: int) -> bool:
        for targets in infobox_targets.values():
            for target_chat_id, target_message_id, _page, _render_mode in targets:
                if target_chat_id == chat_id and target_message_id == message_id:
                    return True
        return False

    async def _register_infobox_target(message, chat_id: int, page: int, render_mode: str) -> None:
        if not message:
            return

        message_chat_id = int(message.chat_id)
        message_id = int(message.message_id)
        has_photo = bool(getattr(message, "photo", None))
        normalized_mode = "card" if has_photo else "text"
        if render_mode == "text":
            normalized_mode = "text"
        elif render_mode == "card" and has_photo:
            normalized_mode = "card"
        _remove_message_target(message_chat_id, message_id)

        new_target = (message_chat_id, message_id, page, normalized_mode)
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
        replacements: list[tuple[tuple[int, int, int, str], tuple[int, int, int, str]]] = []
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
                if "caption" in message or "text" in message:
                    replacement_message, replacement_mode = await _send_chat_detail_via_app(
                        chat_id=chat_id,
                        page=page,
                        heading=heading,
                        preferred_render_mode=render_mode,
                    )
                    try:
                        await app.bot.delete_message(chat_id=target_chat_id, message_id=target_message_id)
                    except BadRequest:
                        pass
                    replacements.append(
                        (
                            (target_chat_id, target_message_id, page, render_mode),
                            (
                                int(replacement_message.chat_id),
                                int(replacement_message.message_id),
                                page,
                                replacement_mode,
                            ),
                        )
                    )
                    continue
                raise
        for target in stale:
            targets.discard(target)
        for old_target, new_target in replacements:
            targets.discard(old_target)
            targets.add(new_target)
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
        register_command_menu = app.bot_data.get("register_command_menu")
        if callable(register_command_menu):
            await register_command_menu()
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

        if data.startswith("mt:"):
            action = data.split(":", 1)[1].strip().lower()
            if action == "runonce":
                await _safe_edit_message(query, "Starte Einmaldurchlauf...")
                summary = await service.run_once()
                _kick_background_sync()
                known_chats = _get_known_chats_cached(limit=80)
                text = _chats_menu_text(known_chats, page=0)
                text = _limit_message(
                    f"Run Once abgeschlossen: Chats={summary.chat_count}, Gesendet={summary.sent_count}\n\n{text}"
                )
                keyboard = _chats_menu_keyboard(known_chats, page=0)
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            if action == "retries":
                attempts = service.list_recent_generation_attempts(limit=20)
                if not attempts:
                    await _safe_edit_message(query, "Keine Retry-/Attempt-Daten vorhanden.")
                    return
                lines = ["Letzte Retries/Attempts (chatübergreifend):"]
                for item in attempts:
                    status = "ok" if item.accepted else "reject"
                    reason = f", reason={item.reject_reason}" if item.reject_reason else ""
                    lines.append(
                        _truncate_value(
                            f"- {item.created_at:%Y-%m-%d %H:%M:%S} | {item.title} ({item.chat_id}) | "
                            f"a{item.attempt_no}/{item.phase} {status}{reason}",
                            max_len=260,
                        )
                    )
                known_chats = _get_known_chats_cached(limit=80)
                await _safe_edit_message(
                    query,
                    _limit_message("\n".join(lines)),
                    reply_markup=_chats_menu_keyboard(known_chats, page=0),
                )
                return
            if action == "history":
                if not service.store:
                    await _safe_edit_message(query, "Keine Datenbank konfiguriert.")
                    return
                history_lines = _history_overview_lines(limit=6)
                if not history_lines:
                    await _safe_edit_message(query, "Keine gespeicherten Analysen vorhanden.")
                    return
                lines = ["History (letzter Stand pro Chat):"] + history_lines
                known_chats = _get_known_chats_cached(limit=80)
                await _safe_edit_message(
                    query,
                    _limit_message("\n".join(lines)),
                    reply_markup=_chats_menu_keyboard(known_chats, page=0),
                )
                return
            await _safe_edit_message(query, "Unbekannte Tool-Aktion.")
            return

        if data.startswith("ax:"):
            try:
                _prefix, mode_raw, chat_id_raw, page_raw, offset_raw = data.split(":")
                mode = mode_raw.strip().lower()
                chat_id = int(chat_id_raw)
                page = int(page_raw)
                key_offset = int(offset_raw)
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Analysis-Demo-Aktion.")
                return
            latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
            analysis_data = latest_entry.analysis if latest_entry else None
            keys = [str(key) for key in analysis_data.keys()] if isinstance(analysis_data, dict) else []
            text = _analysis_demo_text(chat_id=chat_id, mode=mode, keys=keys, key_offset=key_offset)
            keyboard = _analysis_demo_keyboard(
                chat_id=chat_id,
                page=page,
                mode=mode,
                keys=keys,
                key_offset=key_offset,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

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
            if message and _is_infobox_message(int(message.chat_id), int(message.message_id)):
                _remove_message_target(int(message.chat_id), int(message.message_id))
                try:
                    await context.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                except BadRequest:
                    pass
                await _post_control_card(
                    kind="menu:chats",
                    text=text,
                    reply_markup=keyboard,
                    bring_to_front=True,
                )
                return
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

        if data.startswith("pp:"):
            try:
                _prefix, chat_id_raw, chat_page_raw, preview_page_raw = data.split(":")
                chat_id = int(chat_id_raw)
                chat_page = int(chat_page_raw)
                preview_page = int(preview_page_raw)
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            text, keyboard = await _render_prompt_preview(
                chat_id=chat_id,
                chat_page=chat_page,
                page=preview_page,
                is_card=False,
            )
            message = getattr(query, "message", None)
            if message and getattr(message, "photo", None):
                await context.bot.send_message(
                    chat_id=allowed_chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
                try:
                    await context.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                except BadRequest:
                    pass
            else:
                await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if data.startswith("md:"):
            try:
                _prefix, op, chat_id_raw, page_raw, target_raw = data.split(":")
                chat_id = int(chat_id_raw)
                page = int(page_raw)
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return

            if op == "busy":
                await query.answer("Dry-Run läuft bereits…")
                return

            if op in {"pa", "pd", "po"}:
                try:
                    preset_index = int(target_raw)
                except ValueError:
                    await _safe_edit_message(query, "Ungueltiges Preset.")
                    return
                preset = _directive_preset_by_index(preset_index)
                if not preset:
                    await _safe_edit_message(query, "Preset nicht gefunden.")
                    return
                preset_label, preset_text = preset
                if op == "pa":
                    created = service.add_chat_directive(chat_id=chat_id, text=preset_text, scope="session")
                    if not created:
                        await _safe_edit_message(query, f"⚠️ Preset konnte nicht hinzugefuegt werden: {preset_label}")
                        return
                    await _safe_edit_message(query, f"✅ Added #{created.id} ({created.scope}): {preset_label}")
                    if chat_id in infobox_targets:
                        app.create_task(_push_infobox_update(chat_id, heading="Direktive hinzugefügt."), update=None)
                    return
                if op == "po":
                    created = service.add_chat_directive(chat_id=chat_id, text=preset_text, scope="once")
                    if not created:
                        await _safe_edit_message(query, f"⚠️ Preset konnte nicht hinzugefuegt werden: {preset_label}")
                        return
                    await _safe_edit_message(query, f"✅ Added once #{created.id}: {preset_label}")
                    if chat_id in infobox_targets:
                        app.create_task(_push_infobox_update(chat_id, heading="Direktive hinzugefügt (once)."), update=None)
                    return
                created = service.add_chat_directive(chat_id=chat_id, text=preset_text, scope="dryrun")
                if not created:
                    await _safe_edit_message(query, f"⚠️ Dry-Run konnte nicht vorbereitet werden: {preset_label}")
                    return

                source_message = getattr(query, "message", None)
                base_text = ""
                if source_message:
                    base_text = str(getattr(source_message, "text", "") or getattr(source_message, "caption", "") or "")
                if not base_text:
                    base_text = f"{preset_index}. {preset_label}\n\n{preset_text}"
                started_text = base_text + "\n\n⏳ Dry-Run läuft…"
                await _safe_edit_message(
                    query,
                    _limit_message(started_text, max_len=3200),
                    reply_markup=_directive_preset_keyboard(chat_id, page, preset_index, dryrun_running=True),
                )

                target_chat_id = int(getattr(source_message, "chat_id", allowed_chat_id)) if source_message else allowed_chat_id
                target_message_id = int(getattr(source_message, "message_id", 0)) if source_message else 0

                async def _finish_dry_run() -> None:
                    final_text: str
                    try:
                        summary = await service.run_once(target_chat_ids={chat_id})
                        suggestion = ""
                        if service.store:
                            latest = service.store.latest_for_chat(chat_id)
                            if latest and isinstance(latest.suggestion, str):
                                suggestion = latest.suggestion.strip()
                        final_lines = [
                            base_text,
                            "",
                            f"✅ Dry-Run fertig: Chats={summary.chat_count}, Gesendet={summary.sent_count}",
                        ]
                        if suggestion:
                            final_lines.extend(["", "Ergebnis:", _truncate_value(suggestion, max_len=900)])
                        final_text = _limit_message("\n".join(final_lines), max_len=3200)
                    except Exception as exc:
                        final_text = _limit_message(base_text + f"\n\n⚠️ Dry-Run fehlgeschlagen: {exc}", max_len=3200)
                    finally:
                        service.delete_chat_directive(chat_id=chat_id, directive_id=created.id)
                        if chat_id in infobox_targets:
                            app.create_task(_push_infobox_update(chat_id, heading="Dry-Run ausgeführt."), update=None)

                    if target_message_id > 0:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=target_chat_id,
                                message_id=target_message_id,
                                text=final_text,
                                reply_markup=_directive_preset_keyboard(chat_id, page, preset_index, dryrun_running=False),
                            )
                        except BadRequest:
                            pass

                app.create_task(_finish_dry_run())
                return

            if op == "cancel":
                batch_id = target_raw.strip()
                entries = directive_delete_batches.pop(batch_id, [])
                if not entries:
                    await _safe_edit_message(query, "Direktiven-Liste bereits geschlossen oder abgelaufen.")
                    return
                for msg_chat_id, msg_id in entries:
                    try:
                        await context.bot.delete_message(chat_id=int(msg_chat_id), message_id=int(msg_id))
                    except BadRequest:
                        pass
                return

            if op != "del":
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            try:
                directive_id = int(target_raw)
            except ValueError:
                await _safe_edit_message(query, "Ungueltige Direktiven-ID.")
                return
            deleted = service.delete_chat_directive(chat_id=chat_id, directive_id=directive_id)
            if deleted:
                await _safe_edit_message(query, f"✅ Direktive #{directive_id} geloescht.")
            else:
                await _safe_edit_message(query, f"⚠️ Direktive #{directive_id} nicht gefunden.")
            if chat_id in infobox_targets:
                app.create_task(_push_infobox_update(chat_id, heading="Direktive aktualisiert."), update=None)
            return

        if data.startswith("me:"):
            try:
                _prefix, op, chat_id_raw, page_raw, key_token = data.split(":")
                chat_id = int(chat_id_raw)
                page = int(page_raw)
            except (ValueError, IndexError):
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            if op != "del":
                await _safe_edit_message(query, "Ungueltige Aktion.")
                return
            key_name = _decode_key_token(key_token)
            if not key_name:
                await _safe_edit_message(query, "Ungueltiger Key.")
                return
            latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
            current = dict(latest_entry.analysis or {}) if latest_entry and isinstance(latest_entry.analysis, dict) else None
            if current is None:
                await _safe_edit_message(query, "Keine Analysis zum Bearbeiten vorhanden.")
                return
            removed = key_name in current
            if removed:
                current.pop(key_name, None)
                service.store.update_latest_analysis(chat_id, current)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            heading = (
                f"Analysis-Key gelöscht: {key_name}"
                if removed
                else f"Analysis-Key nicht gefunden: {key_name}"
            )
            text, keyboard = await _render_chat_detail(chat_id, page, heading=heading, compact=is_card)
            await _safe_edit_message(query, text, reply_markup=keyboard)
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

        if action == "sk":
            skipped = service.request_skip_current_action(chat_id)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not skipped:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine überspringbare Wartezeit aktiv.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Aktuelle Wartezeit wird übersprungen.",
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
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
                caption_pages = _paginate_text(caption, max_len=card_caption_len)
                first_caption = _limit_caption(caption_pages[0] if caption_pages else "")
                try:
                    await context.bot.send_photo(
                        chat_id=allowed_chat_id,
                        photo=InputFile(image_bytes, filename=f"chat_{chat_id}.jpg"),
                        caption=first_caption,
                    )
                except BadRequest as exc:
                    if not _is_caption_too_long_error(exc):
                        raise
                    await context.bot.send_photo(
                        chat_id=allowed_chat_id,
                        photo=InputFile(image_bytes, filename=f"chat_{chat_id}.jpg"),
                    )
                for idx, tail_page in enumerate(caption_pages[1:], start=2):
                    await context.bot.send_message(
                        chat_id=allowed_chat_id,
                        text=f"Bild-Caption {idx}/{len(caption_pages)}:\n{tail_page}",
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

        if action in {"dl", "d-"}:
            directives = service.list_chat_directives(chat_id=chat_id, active_only=True, limit=50)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not directives:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine aktiven Direktiven vorhanden.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            directive_delete_batch_seq["value"] += 1
            # Keep batch_id delimiter-safe for callback_data parsing (":" is used as separator).
            batch_id = f"{chat_id}-{directive_delete_batch_seq['value']}"
            created_messages: list[tuple[int, int]] = []
            target_chat_id = int(getattr(getattr(query, "message", None), "chat_id", allowed_chat_id))

            cancel_message = await context.bot.send_message(
                chat_id=target_chat_id,
                text=_limit_message(
                    f"Direktiven fuer Chat {chat_id} ({len(directives)} Eintraege).\n"
                    "Abbrechen loescht alle unten erzeugten Direktiven-Nachrichten."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Abbrechen", callback_data=f"md:cancel:{chat_id}:{page}:{batch_id}")]]
                ),
            )
            created_messages.append((int(cancel_message.chat_id), int(cancel_message.message_id)))

            for item in directives:
                msg = await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=_limit_message(f"#{item.id} ({item.scope})\n{item.text}", max_len=3200),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Löschen", callback_data=f"md:del:{chat_id}:{page}:{item.id}")]]
                    ),
                )
                created_messages.append((int(msg.chat_id), int(msg.message_id)))

            directive_delete_batches[batch_id] = created_messages
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading=f"Direktiven als Einzelnachrichten gepostet ({len(directives)}).",
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action in {"ad", "a-"}:
            latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
            analysis_data = latest_entry.analysis if latest_entry else None
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not isinstance(analysis_data, dict) or not analysis_data:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine Analysis-Keys zum Loeschen vorhanden.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            lines = [f"Chat {chat_id}", "", "Analysis-Key löschen:"]
            for key in list(analysis_data.keys())[:12]:
                lines.append(f"- {key}")
            text = _limit_message("\n".join(lines))
            await _safe_edit_message(query, text, reply_markup=_analysis_delete_keyboard(chat_id, page))
            return

        if action in {"ae"}:
            latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
            analysis_data = latest_entry.analysis if latest_entry else None
            keys = [str(key) for key in analysis_data.keys()] if isinstance(analysis_data, dict) else []
            text = _analysis_demo_text(chat_id=chat_id, mode="edit", keys=keys, key_offset=0)
            keyboard = _analysis_demo_keyboard(
                chat_id=chat_id,
                page=page,
                mode="edit",
                keys=keys,
                key_offset=0,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "p":
            chat_context = await service.core.build_chat_context(chat_id)
            if not chat_context:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Kein Chatverlauf gefunden.",
                    compact=bool(getattr(getattr(query, "message", None), "photo", None)),
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            prompt_context, language_hint, _previous_analysis = service.build_prompt_context_for_chat(chat_id)
            model_messages = service.core.build_model_messages(
                chat_context,
                language_hint=language_hint,
                prompt_context=prompt_context,
            )
            preview_raw = json.dumps(model_messages, ensure_ascii=False, indent=2)
            preview_pages = _paginate_text(preview_raw, max_len=3000)
            prompt_preview_cache[chat_id] = (page, preview_pages)
            text, keyboard = await _render_prompt_preview(
                chat_id=chat_id,
                chat_page=page,
                page=0,
                is_card=False,
            )
            message = getattr(query, "message", None)
            if message and getattr(message, "photo", None):
                await context.bot.send_message(
                    chat_id=allowed_chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
                try:
                    await context.bot.delete_message(chat_id=int(message.chat_id), message_id=int(message.message_id))
                except BadRequest:
                    pass
            else:
                await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "r":
            attempts = service.list_generation_attempts(chat_id, limit=12)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not attempts:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine Retry-/Attempt-Daten vorhanden.",
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            lines = [f"Chat {chat_id}", "", f"Retries ({len(attempts)}):"]
            for idx, item in enumerate(attempts, start=1):
                status = "ok" if item.accepted else "reject"
                reason = f" | reason={item.reject_reason}" if item.reject_reason else ""
                schema = f" | schema={item.schema}" if item.schema else ""
                lines.append(
                    _truncate_value(
                        f"{idx}. {item.created_at:%Y-%m-%d %H:%M:%S} | a{item.attempt_no}/{item.phase} | "
                        f"{status}{reason}{schema}",
                        max_len=220,
                    )
                )
                if item.raw_excerpt:
                    lines.append(_truncate_value(f"   raw={item.raw_excerpt}", max_len=220))
            text = _limit_message("\n".join(lines), max_len=3300)
            await _safe_edit_message(query, text, reply_markup=_chat_detail_keyboard(chat_id, page))
            return

        self_warning = f"Unbekannte Callback-Aktion: data={data}"
        service.add_general_warning(self_warning)
        await _safe_edit_message(query, f"Unbekannte Aktion: {data}")

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
        lines = _history_overview_lines(limit=8)
        if not lines:
            await _guarded_reply(update, "Keine gespeicherten Analysen vorhanden.")
            return
        await _guarded_reply_chunks(update, "History (letzter Stand pro Chat):\n" + "\n".join(lines))

    async def retries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        attempts = service.list_recent_generation_attempts(limit=20)
        if not attempts:
            await _guarded_reply(update, "Keine Retry-/Attempt-Daten vorhanden.")
            return
        lines = ["Letzte Retries/Attempts (chatübergreifend):"]
        for item in attempts:
            status = "ok" if item.accepted else "reject"
            reason = f", reason={item.reject_reason}" if item.reject_reason else ""
            lines.append(
                _truncate_value(
                    f"- {item.created_at:%Y-%m-%d %H:%M:%S} | {item.title} ({item.chat_id}) | "
                    f"a{item.attempt_no}/{item.phase} {status}{reason}",
                    max_len=260,
                )
            )
        await _guarded_reply_chunks(update, "\n".join(lines))

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

        prompt_context, language_hint, _previous_analysis = service.build_prompt_context_for_chat(chat_id)
        model_messages = service.core.build_model_messages(
            chat_context,
            language_hint=language_hint,
            prompt_context=prompt_context,
        )
        preview = json.dumps(model_messages, ensure_ascii=False, indent=2)
        await _guarded_reply_chunks(update, "Prompt-Preview (tatsaechlicher Modell-Input):\n" + preview)

    async def directive_input_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if not query:
            return ConversationHandler.END
        await query.answer()
        if not update.effective_chat or update.effective_chat.id != allowed_chat_id:
            await _safe_edit_message(query, "Nicht autorisiert.")
            return ConversationHandler.END
        data = query.data or ""
        try:
            _prefix, action, chat_id_raw, page_raw = data.split(":")
            chat_id = int(chat_id_raw)
            page = int(page_raw)
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Ungueltige Aktion.")
            return ConversationHandler.END
        if action not in {"da", "d+"}:
            return ConversationHandler.END
        await _cleanup_context_messages(context, directive_context_messages_key)
        presets = await _build_directive_presets(chat_id, limit=7)
        context.user_data["directive_target"] = {
            "chat_id": chat_id,
            "page": page,
        }
        is_card = bool(getattr(getattr(query, "message", None), "photo", None))
        text, keyboard = await _render_chat_detail(
            chat_id,
            page,
            heading="Direktive aktivieren: Preset per Inline-Button oder frei tippen.",
            compact=is_card,
        )
        await _safe_edit_message(query, text, reply_markup=keyboard)
        rows: list[list[KeyboardButton]] = [[KeyboardButton(directive_reply_kb_cancel)]]
        reply_keyboard = ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            one_time_keyboard=False,
            input_field_placeholder="Direktive eingeben…",
        )
        target_chat_id = int(getattr(getattr(query, "message", None), "chat_id", allowed_chat_id))
        info_lines = ["Direktiven-Presets (Inline):"]
        for idx, (label, preset_text) in enumerate(presets, start=1):
            sent_preset = await context.bot.send_message(
                chat_id=target_chat_id,
                text=_limit_message(f"{idx}. {label}\n\n{preset_text}", max_len=3200),
                reply_markup=_directive_preset_keyboard(chat_id, page, idx, dryrun_running=False),
            )
            _track_context_message(context, directive_context_messages_key, sent_preset)
        info_lines.append("- Add: speichert als Session-Direktive")
        info_lines.append("- Dry-Run: einmalige Generierung mit temporaerer Direktive")
        info_lines.append("- Once: loescht sich nach erster Anwendung (operator_applied)")
        info_lines.append("- Frei tippen: eigene Direktive senden")
        info_lines.append("- ❌ Abbrechen: Eingabe beenden")
        sent_info = await context.bot.send_message(
            chat_id=target_chat_id,
            text=_limit_message("\n".join(info_lines), max_len=3000),
            reply_markup=reply_keyboard,
        )
        _track_context_message(context, directive_context_messages_key, sent_info)
        return directive_input_state

    async def directive_input_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not _authorized(update):
            return ConversationHandler.END
        await _cleanup_context_messages(context, directive_context_messages_key)
        context.user_data.pop("directive_target", None)
        if update.message:
            await update.message.reply_text("Direktiven-Eingabe abgebrochen.", reply_markup=ReplyKeyboardRemove())
        else:
            await _guarded_reply(update, "Direktiven-Eingabe abgebrochen.")
        return ConversationHandler.END

    async def directive_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not _authorized(update):
            return ConversationHandler.END
        message = update.message
        if not message or not message.text:
            return directive_input_state
        target = context.user_data.get("directive_target")
        if not isinstance(target, dict):
            return ConversationHandler.END
        raw_chat_id = target.get("chat_id")
        raw_page = target.get("page")
        if not isinstance(raw_chat_id, int) or not isinstance(raw_page, int):
            await _cleanup_context_messages(context, directive_context_messages_key)
            context.user_data.pop("directive_target", None)
            return ConversationHandler.END
        text = message.text.strip()
        if text == directive_reply_kb_cancel:
            await _cleanup_context_messages(context, directive_context_messages_key)
            context.user_data.pop("directive_target", None)
            await message.reply_text("Direktiven-Editor beendet.", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        if not text or text.startswith("/"):
            await _guarded_reply(update, "Bitte Direktive als normalen Text senden (ohne Slash-Befehl).")
            return directive_input_state
        chat_id = raw_chat_id
        page = raw_page
        created = service.add_chat_directive(chat_id=chat_id, text=text, scope="session")
        await _cleanup_context_messages(context, directive_context_messages_key)
        context.user_data.pop("directive_target", None)
        if not created:
            await _guarded_reply(update, "Direktive konnte nicht gespeichert werden.")
            return ConversationHandler.END
        await _guarded_reply(
            update,
            f"Direktive gespeichert: #{created.id} ({created.scope}) {created.text}",
        )
        await message.reply_text("Reply-Keyboard geschlossen.", reply_markup=ReplyKeyboardRemove())
        if chat_id in infobox_targets:
            app.create_task(_push_infobox_update(chat_id, heading="Direktive hinzugefügt."), update=None)
        return ConversationHandler.END

    async def analysis_input_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if not query:
            return ConversationHandler.END
        await query.answer()
        if not update.effective_chat or update.effective_chat.id != allowed_chat_id:
            await _safe_edit_message(query, "Nicht autorisiert.")
            return ConversationHandler.END
        data = query.data or ""
        try:
            _prefix, action, chat_id_raw, page_raw = data.split(":")
            chat_id = int(chat_id_raw)
            page = int(page_raw)
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Ungueltige Aktion.")
            return ConversationHandler.END
        if action not in {"ak", "a+", "ae"}:
            return ConversationHandler.END
        await _cleanup_context_messages(context, analysis_context_messages_key)
        mode = "quick" if action == "ae" else "new"
        context.user_data["analysis_target"] = {"chat_id": chat_id, "page": page, "mode": mode}
        is_card = bool(getattr(getattr(query, "message", None), "photo", None))
        heading_text = (
            "Analysis Editor (Reply-Keyboard Demo): nutze unten die Tastatur oder sende direkt key=value bzw. JSON."
            if action == "ae"
            else "Analysis bearbeiten: sende jetzt `key=value` oder ein JSON-Objekt `{...}` zum Mergen (oder /cancel)."
        )
        text, keyboard = await _render_chat_detail(
            chat_id,
            page,
            heading=heading_text,
            compact=is_card,
        )
        await _safe_edit_message(query, text, reply_markup=keyboard)
        if action == "ae":
            reply_keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(analysis_reply_kb_keyvalue), KeyboardButton(analysis_reply_kb_json)],
                    [KeyboardButton(analysis_reply_kb_cancel)],
                ],
                resize_keyboard=True,
                one_time_keyboard=False,
                input_field_placeholder="key=value oder JSON senden…",
            )
            sent = await context.bot.send_message(
                chat_id=allowed_chat_id,
                text=(
                    "Reply-Keyboard aktiv (Demo).\n"
                    f"- {analysis_reply_kb_keyvalue}: Hinweis für Key-Value\n"
                    f"- {analysis_reply_kb_json}: Hinweis für JSON-Merge\n"
                    f"- {analysis_reply_kb_cancel}: Editor beenden"
                ),
                reply_markup=reply_keyboard,
            )
            _track_context_message(context, analysis_context_messages_key, sent)
        return analysis_input_state

    async def analysis_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        if not query:
            return ConversationHandler.END
        await query.answer()
        if not update.effective_chat or update.effective_chat.id != allowed_chat_id:
            await _safe_edit_message(query, "Nicht autorisiert.")
            return ConversationHandler.END
        data = query.data or ""
        try:
            _prefix, op, chat_id_raw, page_raw, key_token = data.split(":")
            chat_id = int(chat_id_raw)
            page = int(page_raw)
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Ungueltige Aktion.")
            return ConversationHandler.END
        if op != "edit":
            return ConversationHandler.END
        key_name = _decode_key_token(key_token)
        if not key_name:
            await _safe_edit_message(query, "Ungueltiger Key.")
            return ConversationHandler.END
        latest_entry = service.store.latest_for_chat(chat_id) if service.store else None
        analysis_data = latest_entry.analysis if latest_entry else None
        if not isinstance(analysis_data, dict) or key_name not in analysis_data:
            await _safe_edit_message(query, f"Key nicht gefunden: {key_name}")
            return ConversationHandler.END
        context.user_data["analysis_target"] = {
            "chat_id": chat_id,
            "page": page,
            "mode": "edit",
            "key": key_name,
        }
        is_card = bool(getattr(getattr(query, "message", None), "photo", None))
        current_value = _truncate_value(str(analysis_data.get(key_name)), max_len=120)
        text, keyboard = await _render_chat_detail(
            chat_id,
            page,
            heading=f"Analysis editieren: {key_name} (alt={current_value})\nSende jetzt den neuen Wert (oder /cancel).",
            compact=is_card,
        )
        await _safe_edit_message(query, text, reply_markup=keyboard)
        return analysis_input_state

    async def analysis_input_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not _authorized(update):
            return ConversationHandler.END
        await _cleanup_context_messages(context, analysis_context_messages_key)
        context.user_data.pop("analysis_target", None)
        if update.message:
            await update.message.reply_text("Analysis-Eingabe abgebrochen.", reply_markup=ReplyKeyboardRemove())
        else:
            await _guarded_reply(update, "Analysis-Eingabe abgebrochen.")
        return ConversationHandler.END

    async def analysis_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not _authorized(update):
            return ConversationHandler.END
        message = update.message
        if not message or not message.text:
            return analysis_input_state
        target = context.user_data.get("analysis_target")
        if not isinstance(target, dict):
            return ConversationHandler.END
        raw_chat_id = target.get("chat_id")
        edit_mode = str(target.get("mode", "new")).strip().lower()
        edit_key = target.get("key")
        if not isinstance(raw_chat_id, int):
            await _cleanup_context_messages(context, analysis_context_messages_key)
            context.user_data.pop("analysis_target", None)
            return ConversationHandler.END
        text = message.text.strip()
        if text == analysis_reply_kb_cancel:
            await _cleanup_context_messages(context, analysis_context_messages_key)
            context.user_data.pop("analysis_target", None)
            await message.reply_text("Analysis-Editor beendet.", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        if text == analysis_reply_kb_keyvalue:
            sent = await message.reply_text("Format: key=value (Beispiel: loop_guard_active=true)")
            _track_context_message(context, analysis_context_messages_key, sent)
            return analysis_input_state
        if text == analysis_reply_kb_json:
            sent = await message.reply_text("Format: JSON-Objekt zum Mergen, z.B. {\"loop_guard_active\": true}")
            _track_context_message(context, analysis_context_messages_key, sent)
            return analysis_input_state
        if edit_mode != "edit":
            try:
                parsed_obj = json.loads(text)
            except Exception:
                parsed_obj = None
            if isinstance(parsed_obj, dict):
                if not service.store:
                    await _guarded_reply(update, "Keine Datenbank konfiguriert.")
                    context.user_data.pop("analysis_target", None)
                    return ConversationHandler.END
                latest_entry = service.store.latest_for_chat(raw_chat_id)
                if not latest_entry:
                    await _guarded_reply(update, "Keine Analyse fuer diesen Chat gefunden.")
                    context.user_data.pop("analysis_target", None)
                    return ConversationHandler.END
                current = dict(latest_entry.analysis or {})
                for key_obj, value_obj in parsed_obj.items():
                    current[str(key_obj)] = value_obj
                updated = service.store.update_latest_analysis(raw_chat_id, current)
                await _cleanup_context_messages(context, analysis_context_messages_key)
                context.user_data.pop("analysis_target", None)
                if not updated:
                    await _guarded_reply(update, "Analysis konnte nicht aktualisiert werden.")
                    return ConversationHandler.END
                await _guarded_reply(update, f"Analysis gemerged: {len(parsed_obj)} Key(s).")
                await message.reply_text("Reply-Keyboard geschlossen.", reply_markup=ReplyKeyboardRemove())
                if raw_chat_id in infobox_targets:
                    app.create_task(_push_infobox_update(raw_chat_id, heading="Analysis-JSON gemerged"), update=None)
                return ConversationHandler.END

        if edit_mode == "edit":
            if not isinstance(edit_key, str) or not edit_key.strip():
                await _guarded_reply(update, "Ungueltiger Edit-Key.")
                return analysis_input_state
            key = edit_key.strip()
            raw_value = text
        else:
            sep = "=" if "=" in text else ":" if ":" in text else None
            if not sep:
                await _guarded_reply(update, "Format: key=value oder JSON-Objekt `{...}`")
                return analysis_input_state
            key, raw_value = text.split(sep, 1)
            key = key.strip()
            raw_value = raw_value.strip()
            if not key:
                await _guarded_reply(update, "Key darf nicht leer sein.")
                return analysis_input_state

        value: object
        try:
            value = json.loads(raw_value)
        except Exception:
            value = raw_value

        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            await _cleanup_context_messages(context, analysis_context_messages_key)
            context.user_data.pop("analysis_target", None)
            return ConversationHandler.END
        latest_entry = service.store.latest_for_chat(raw_chat_id)
        if not latest_entry:
            await _guarded_reply(update, "Keine Analyse fuer diesen Chat gefunden.")
            await _cleanup_context_messages(context, analysis_context_messages_key)
            context.user_data.pop("analysis_target", None)
            return ConversationHandler.END
        current = dict(latest_entry.analysis or {})
        current[key] = value
        updated = service.store.update_latest_analysis(raw_chat_id, current)
        await _cleanup_context_messages(context, analysis_context_messages_key)
        context.user_data.pop("analysis_target", None)
        if not updated:
            await _guarded_reply(update, "Analysis konnte nicht aktualisiert werden.")
            return ConversationHandler.END
        await _guarded_reply(update, f"Analysis aktualisiert: {key}={value}")
        await message.reply_text("Reply-Keyboard geschlossen.", reply_markup=ReplyKeyboardRemove())
        if raw_chat_id in infobox_targets:
            app.create_task(_push_infobox_update(raw_chat_id, heading=f"Analysis-Key gesetzt: {key}"), update=None)
        return ConversationHandler.END

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

    def _command_shortcuts(limit: int = 7) -> tuple[list[BotCommand], dict[str, tuple[int, int]]]:
        known_chats = _get_known_chats_cached(limit=80)
        total_pages = max(1, math.ceil(len(known_chats) / chats_page_size))
        chat_commands: list[BotCommand] = []
        mapping: dict[str, tuple[int, int]] = {}
        for idx, item in enumerate(known_chats[: max(0, min(limit, 7))], start=1):
            page = min(total_pages - 1, (idx - 1) // chats_page_size)
            command = f"c{idx}"
            title = _truncate_value(str(item.title), max_len=22)
            chat_commands.append(BotCommand(command, f"Chat: {title}"))
            mapping[command] = (int(item.chat_id), int(page))
        return chat_commands, mapping

    async def register_command_menu() -> None:
        base_commands = [
            BotCommand("chats", "Chat-Übersicht öffnen"),
            BotCommand("runonce", "Einmaldurchlauf starten"),
            BotCommand("retries", "Letzte Retries anzeigen"),
            BotCommand("history", "Persistierte History anzeigen"),
            BotCommand("last", "Letzte Vorschläge anzeigen"),
            BotCommand("promptpreview", "Prompt-Preview für Chat-ID"),
            BotCommand("analysisget", "Analysis für Chat-ID anzeigen"),
            BotCommand("analysisset", "Analysis für Chat-ID setzen"),
        ]
        chat_commands, mapping = _command_shortcuts(limit=7)
        commands = base_commands + chat_commands
        app.bot_data["chat_shortcut_map"] = mapping
        try:
            await app.bot.set_my_commands(commands=commands, scope=BotCommandScopeChat(chat_id=allowed_chat_id))
        except Exception as exc:
            service.add_general_warning(f"Command-Menü konnte nicht gesetzt werden: {exc}")

    async def open_chat_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        message = update.message
        if not message or not message.text:
            return
        token = message.text.strip().split()[0]
        if token.startswith("/"):
            token = token[1:]
        token = token.split("@", 1)[0].lower()
        shortcut_map = app.bot_data.get("chat_shortcut_map")
        if not isinstance(shortcut_map, dict):
            register_command_menu = app.bot_data.get("register_command_menu")
            if callable(register_command_menu):
                await register_command_menu()
            shortcut_map = app.bot_data.get("chat_shortcut_map")
        if not isinstance(shortcut_map, dict) or token not in shortcut_map:
            await _guarded_reply(update, "Unbekannter Chat-Shortcut. Bitte /chats nutzen.")
            return
        raw = shortcut_map.get(token)
        if not (isinstance(raw, tuple) and len(raw) == 2):
            await _guarded_reply(update, "Shortcut-Mapping ungültig. Bitte /chats nutzen.")
            return
        chat_id, page = raw
        text, keyboard = await _render_chat_detail(int(chat_id), int(page), compact=False)
        await update.message.reply_text(text, reply_markup=keyboard)

    app.bot_data["send_start_menu"] = send_start_menu
    app.bot_data["register_command_menu"] = register_command_menu

    app.add_handler(CommandHandler("runonce", run_once))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler(["c1", "c2", "c3", "c4", "c5", "c6", "c7"], open_chat_shortcut))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("retries", retries))
    app.add_handler(CommandHandler("analysisget", analysis_get))
    app.add_handler(CommandHandler("analysisset", analysis_set))
    app.add_handler(CommandHandler("promptpreview", prompt_preview))
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(directive_input_start, pattern=r"^ma:(da|d\+):")],
            states={
                directive_input_state: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, directive_input),
                ],
            },
            fallbacks=[CommandHandler("cancel", directive_input_cancel)],
            allow_reentry=True,
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(analysis_input_start, pattern=r"^ma:(ak|a\+):"),
                CallbackQueryHandler(analysis_input_start, pattern=r"^ma:ae:"),
                CallbackQueryHandler(analysis_edit_start, pattern=r"^me:edit:"),
            ],
            states={
                analysis_input_state: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, analysis_input),
                ],
            },
            fallbacks=[CommandHandler("cancel", analysis_input_cancel)],
            allow_reentry=True,
        )
    )
    app.add_handler(CallbackQueryHandler(callback_action, pattern=r"^(ml|mq|mc|ma|md|me|ax|pp|mt|generate|send|stop|autoon|autooff|img|kv):"))
    return app
