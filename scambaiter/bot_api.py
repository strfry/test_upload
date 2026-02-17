from __future__ import annotations

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
    base_info_keys = [
        "sprache",
        "scamtyp",
        "kontakt",
        "kontakt_art",
        "betrüger_art",
        "betrueger_art",
        "scam-verdacht",
        "typ",
    ]
    base_info_labels = {
        "sprache": "Sprache",
        "scamtyp": "Scamtyp",
        "kontakt": "Kontakt",
        "kontakt_art": "Kontakt",
        "betrüger_art": "Betrüger-Art",
        "betrueger_art": "Betrüger-Art",
        "scam-verdacht": "Scam-Verdacht",
        "typ": "Typ",
    }
    infobox_targets: dict[int, set[tuple[int, int, int, str]]] = {}

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

    def _truncate_value(value: str, max_len: int = 60) -> str:
        value = value.strip()
        if len(value) <= max_len:
            return value
        return value[: max_len - 3].rstrip() + "..."

    def _format_chat_overview(
        chat_id: int,
        title: str,
        updated_at: object,
        kv_data: dict[str, str],
        suggestion: str | None,
        pending: PendingMessage | None,
    ) -> str:
        lines = [f"{title} ({chat_id})"]
        if hasattr(updated_at, "strftime"):
            lines.append(f"Zuletzt: {updated_at:%Y-%m-%d %H:%M}")
        lines.append(f"Auto-Senden: {'AN' if service.is_chat_auto_enabled(chat_id) else 'AUS'}")
        if pending:
            lines.append(_format_pending_state(pending))
        if suggestion:
            lines.append("Vorschlag: " + _truncate_value(suggestion, max_len=900))
        if kv_data:
            seen_labels: set[str] = set()
            parts: list[str] = []
            for key in base_info_keys:
                value = kv_data.get(key)
                if not value:
                    continue
                label = base_info_labels.get(key, key)
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                parts.append(f"{label}={_truncate_value(value)}")
            if parts:
                lines.append("Info: " + ", ".join(parts))
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
                    InlineKeyboardButton("Send", callback_data=f"ma:s:{chat_id}:{page}"),
                    InlineKeyboardButton("Stop", callback_data=f"ma:x:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("Auto an", callback_data=f"ma:on:{chat_id}:{page}"),
                    InlineKeyboardButton("Auto aus", callback_data=f"ma:off:{chat_id}:{page}"),
                ],
                [
                    InlineKeyboardButton("Bilder", callback_data=f"ma:i:{chat_id}:{page}"),
                    InlineKeyboardButton("KV", callback_data=f"ma:k:{chat_id}:{page}"),
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
        lines = [f"Chats ({len(known_chats)}) | Seite {page + 1}/{total_pages}", "Status: A=kein Prozess, G/W/S/OK/X/!"]
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
        rows.append([InlineKeyboardButton("Refresh", callback_data=f"ml:{page}")])
        return InlineKeyboardMarkup(rows)

    def _format_pending_state(pending: PendingMessage | None) -> str:
        if not pending:
            return "Status: Kein Prozesszustand vorhanden."
        state_labels = {
            MessageState.GENERATING: "Vorschlag wird erzeugt",
            MessageState.WAITING: "Wartephase",
            MessageState.SENDING_TYPING: "Sendephase (Tippen)",
            MessageState.SENT: "Gesendet",
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
        lines.append(f"Trigger: {pending.trigger}")
        return "\n".join(lines)

    def _pending_state_short(pending: PendingMessage | None) -> str:
        if not pending:
            return "Kein Prozess"
        state_labels = {
            MessageState.GENERATING: "Generierung",
            MessageState.WAITING: "Wartephase",
            MessageState.SENDING_TYPING: "Senden",
            MessageState.SENT: "Gesendet",
            MessageState.CANCELLED: "Abgebrochen",
            MessageState.ERROR: "Fehler",
        }
        label = state_labels.get(pending.state, pending.state.value)
        if pending.state == MessageState.WAITING and pending.wait_until is not None:
            return f"{label} bis {pending.wait_until:%H:%M:%S}"
        return label

    async def _render_chat_detail(
        chat_id: int,
        page: int,
        heading: str | None = None,
        ensure_suggestion: bool = True,
        compact: bool = False,
    ) -> tuple[str, InlineKeyboardMarkup]:
        known = _find_known_chat(chat_id)
        pending = service.get_pending_message(chat_id)
        title = known.title if known else (pending.title if pending else str(chat_id))
        updated_at = known.updated_at if known else (pending.created_at if pending else None)

        kv_data: dict[str, str] = {}
        if service.store:
            kv_data = service.store.kv_get_many(chat_id, base_info_keys + ["antwort"])

        if ensure_suggestion and not pending and not kv_data.get("antwort"):
            await service.schedule_suggestion_generation(
                chat_id=chat_id,
                title=title,
                trigger="chat-detail-open",
                auto_send=False,
            )
            pending = service.get_pending_message(chat_id)

        suggestion = pending.suggestion if (pending and pending.suggestion) else kv_data.get("antwort")
        if compact:
            lines: list[str] = []
            if heading:
                lines.append(_truncate_value(heading, max_len=180))
            lines.append(f"{_truncate_value(title, max_len=64)} ({chat_id})")
            if hasattr(updated_at, "strftime"):
                lines.append(f"Zuletzt: {updated_at:%Y-%m-%d %H:%M}")
            lines.append(f"Status: {_pending_state_short(pending)}")
            lines.append(f"Auto: {'AN' if service.is_chat_auto_enabled(chat_id) else 'AUS'}")
            if suggestion:
                lines.append("Vorschlag: " + _truncate_value(suggestion, max_len=320))
            info_parts: list[str] = []
            for key in base_info_keys:
                value = kv_data.get(key)
                if not value:
                    continue
                label = base_info_labels.get(key, key)
                part = f"{label}={_truncate_value(value, max_len=28)}"
                if part not in info_parts:
                    info_parts.append(part)
                if len(info_parts) >= 4:
                    break
            if info_parts:
                lines.append("Info: " + ", ".join(info_parts))
            text = _limit_caption("\n".join(lines))
        else:
            body = _format_chat_overview(chat_id, title, updated_at, kv_data, suggestion, pending)
            text = f"{heading}\n\n{body}" if heading else body
            text = _limit_message(text)
        return text, _chat_detail_keyboard(chat_id, page)

    async def _send_chat_detail_card(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        page: int,
        heading: str | None = None,
        ensure_suggestion: bool = True,
    ):
        caption, keyboard = await _render_chat_detail(
            chat_id,
            page,
            heading=heading,
            ensure_suggestion=ensure_suggestion,
            compact=True,
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
                chat_id, page, heading=heading, ensure_suggestion=False, compact=compact
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
        if event_chat_id not in infobox_targets:
            return
        app.create_task(_push_infobox_update(event_chat_id), update=None)

    service.add_pending_listener(_on_pending_changed)

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        summary = service.last_summary
        if not summary:
            await _guarded_reply(update, "Noch kein Lauf ausgeführt.")
            return
        pending_count = service.pending_count()
        await _guarded_reply(
            update,
            (
                f"Letzter Lauf: {summary.finished_at:%Y-%m-%d %H:%M:%S}\n"
                f"Gefundene Chats: {summary.chat_count}\n"
                f"Gesendete Nachrichten: {summary.sent_count}\n"
                f"Nachrichtenprozesse: {pending_count}"
            ),
        )

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
                text, keyboard = await _render_chat_detail(chat_id, page, ensure_suggestion=True, compact=True)
                await _safe_edit_message(query, text, reply_markup=keyboard)
                await _register_infobox_target(message, chat_id, page, render_mode="card")
                return
            card_message = await _send_chat_detail_card(
                context,
                chat_id=chat_id,
                page=page,
                ensure_suggestion=True,
            )
            await _register_infobox_target(card_message, chat_id, page, render_mode="card")
            return

        action = ""
        page = 0
        try:
            if data.startswith("ma:"):
                _prefix, action, chat_id_raw, page_raw = data.split(":")
                chat_id = int(chat_id_raw)
                page = int(page_raw)
            else:
                action, chat_id_raw = data.split(":", maxsplit=1)
                chat_id = int(chat_id_raw)
        except (ValueError, IndexError):
            await _safe_edit_message(query, "Ungueltige Aktion.")
            return

        action_map = {
            "generate": "g",
            "send": "s",
            "stop": "x",
            "autoon": "on",
            "autooff": "off",
            "img": "i",
            "kv": "k",
        }
        action = action_map.get(action, action)

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
                    ensure_suggestion=False,
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
                chat_id, page, heading=heading, ensure_suggestion=False, compact=is_card
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            message = getattr(query, "message", None)
            if message:
                await _register_infobox_target(message, chat_id, page, render_mode=("card" if is_card else "text"))
            return

        if action == "on":
            service.set_chat_auto(chat_id, enabled=True)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Auto-Senden fuer diesen Chat aktiviert.",
                ensure_suggestion=False,
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
                ensure_suggestion=False,
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            return

        if action == "s":
            started = await service.trigger_send(chat_id, trigger="bot-send-button")
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            if not started:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Kein sendbarer Zustand vorhanden.",
                    ensure_suggestion=False,
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            text, keyboard = await _render_chat_detail(
                chat_id,
                page,
                heading="Senden ausgeloest (oder vorgemerkt).",
                ensure_suggestion=False,
                compact=is_card,
            )
            await _safe_edit_message(query, text, reply_markup=keyboard)
            message = getattr(query, "message", None)
            if message:
                await _register_infobox_target(message, chat_id, page, render_mode=("card" if is_card else "text"))
            return

        if action == "x":
            result = await service.abort_send(chat_id)
            is_card = bool(getattr(getattr(query, "message", None), "photo", None))
            text, keyboard = await _render_chat_detail(
                chat_id, page, heading=result, ensure_suggestion=False, compact=is_card
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
                    ensure_suggestion=False,
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
                ensure_suggestion=False,
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
                    ensure_suggestion=False,
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            items = service.store.kv_list(chat_id, limit=20)
            if not items:
                text, keyboard = await _render_chat_detail(
                    chat_id,
                    page,
                    heading="Keine Keys gespeichert.",
                    ensure_suggestion=False,
                    compact=is_card,
                )
                await _safe_edit_message(query, text, reply_markup=keyboard)
                return
            lines = [f"- {item.key}={item.value}" for item in items]
            text, keyboard = await _render_chat_detail(
                chat_id, page, heading="KV Store", ensure_suggestion=False, compact=is_card
            )
            if is_card:
                text = _limit_caption(text + "\n" + "\n".join(lines[:6]))
            else:
                text = _limit_message(text + "\n\n" + "\n".join(lines))
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
                parts.append(f"Analyse={item.analysis}")
            blocks.append("\n".join(parts))
        await _guarded_reply_chunks(update, "Persistierte Analysen:\n\n" + "\n\n".join(blocks))

    async def kv_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        if len(context.args) < 3:
            await _guarded_reply(update, "Nutzung: /kvset <scammer_chat_id> <key> <value>")
            return
        try:
            scammer_chat_id = int(context.args[0])
        except ValueError:
            await _guarded_reply(update, "scammer_chat_id muss eine Zahl sein.")
            return
        key = context.args[1].strip().lower()
        value = " ".join(context.args[2:]).strip()
        if not key or not value:
            await _guarded_reply(update, "Nutzung: /kvset <scammer_chat_id> <key> <value>")
            return
        service.store.kv_set(scammer_chat_id, key, value)
        await _guarded_reply(update, f"Gespeichert fuer {scammer_chat_id}: {key}={value}")

    async def kv_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        if len(context.args) != 2:
            await _guarded_reply(update, "Nutzung: /kvget <scammer_chat_id> <key>")
            return
        try:
            scammer_chat_id = int(context.args[0])
        except ValueError:
            await _guarded_reply(update, "scammer_chat_id muss eine Zahl sein.")
            return
        key = context.args[1].strip().lower()
        item = service.store.kv_get(scammer_chat_id, key)
        if not item:
            await _guarded_reply(update, "Key nicht gefunden.")
            return
        await _guarded_reply(
            update,
            f"[{item.scammer_chat_id}] {item.key}={item.value} (updated {item.updated_at:%Y-%m-%d %H:%M:%S})",
        )

    async def kv_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        if len(context.args) != 2:
            await _guarded_reply(update, "Nutzung: /kvdel <scammer_chat_id> <key>")
            return
        try:
            scammer_chat_id = int(context.args[0])
        except ValueError:
            await _guarded_reply(update, "scammer_chat_id muss eine Zahl sein.")
            return
        key = context.args[1].strip().lower()
        deleted = service.store.kv_delete(scammer_chat_id, key)
        await _guarded_reply(update, "Geloescht." if deleted else "Key nicht gefunden.")

    async def kv_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        if len(context.args) != 1:
            await _guarded_reply(update, "Nutzung: /kvlist <scammer_chat_id>")
            return
        try:
            scammer_chat_id = int(context.args[0])
        except ValueError:
            await _guarded_reply(update, "scammer_chat_id muss eine Zahl sein.")
            return
        items = service.store.kv_list(scammer_chat_id, limit=20)
        if not items:
            await _guarded_reply(update, "Keine Keys fuer diesen Scammer gespeichert.")
            return
        lines = [f"- {item.key}={item.value}" for item in items]
        await _guarded_reply(update, f"KV Store fuer {scammer_chat_id}:\n" + "\n".join(lines))

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
        await app.bot.send_message(chat_id=allowed_chat_id, text=text, reply_markup=keyboard)

    app.bot_data["send_start_menu"] = send_start_menu

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("runonce", run_once))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("kvset", kv_set))
    app.add_handler(CommandHandler("kvget", kv_get))
    app.add_handler(CommandHandler("kvdel", kv_del))
    app.add_handler(CommandHandler("kvlist", kv_list))
    app.add_handler(CommandHandler("promptpreview", prompt_preview))
    app.add_handler(CallbackQueryHandler(callback_action, pattern=r"^(ml|mc|ma|generate|send|stop|autoon|autooff|img|kv):"))
    return app
