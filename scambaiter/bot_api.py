from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from scambaiter.service import BackgroundService, MessageState, PendingMessage


def create_bot_app(token: str, service: BackgroundService, allowed_chat_id: int) -> Application:
    app = Application.builder().token(token).build()
    max_message_len = 3500
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

    async def _safe_edit_message(query, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

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
            lines.append("Vorschlag: " + suggestion)
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

    def _chat_actions_keyboard(chat_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚙️ Generate", callback_data=f"generate:{chat_id}"),
                    InlineKeyboardButton("📤 Send", callback_data=f"send:{chat_id}"),
                    InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{chat_id}"),
                ],
                [
                    InlineKeyboardButton("▶️ Auto an", callback_data=f"autoon:{chat_id}"),
                    InlineKeyboardButton("⏸ Auto aus", callback_data=f"autooff:{chat_id}"),
                ],
                [
                    InlineKeyboardButton("🖼 Bilder", callback_data=f"img:{chat_id}"),
                    InlineKeyboardButton("🗂 KV", callback_data=f"kv:{chat_id}"),
                ],
            ]
        )

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

    def _build_process_infobox(chat_id: int, heading: str) -> str:
        pending = service.get_pending_message(chat_id)
        lines = [heading, f"Chat-ID: {chat_id}", f"Auto-Senden: {'AN' if service.is_chat_auto_enabled(chat_id) else 'AUS'}"]
        lines.append(_format_pending_state(pending))
        if pending and pending.suggestion:
            lines.append("Vorschlag: " + pending.suggestion)
        return "\n".join(lines)

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

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await _guarded_reply(update, "Starte Ordner-Scan für unbeantwortete Chats...")
        created = await service.scan_folder(force=False)
        known_count = len(service.list_known_chats(limit=500))
        await _guarded_reply(update, f"Scan abgeschlossen. Neue Vorschlaege: {created}, bekannte Chats im Ordner: {known_count}")

    async def chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return

        known_chats = service.list_known_chats(limit=50)
        if not known_chats:
            await service.refresh_known_chats_from_folder()
            known_chats = service.list_known_chats(limit=50)
            if not known_chats:
                await _guarded_reply(update, "Keine Chats im konfigurierten Ordner gefunden.")
                return

        service.start_unanswered_prefetch()
        service.start_known_chats_refresh()

        for item in known_chats:
            kv_data: dict[str, str] = {}
            if service.store:
                kv_data = service.store.kv_get_many(item.chat_id, base_info_keys + ["antwort"])
            pending = service.get_pending_message(item.chat_id)
            if not pending and not kv_data.get("antwort"):
                await service.schedule_suggestion_generation(
                    chat_id=item.chat_id,
                    title=item.title,
                    trigger="chat-overview-known-chat",
                    auto_send=False,
                )
                pending = service.get_pending_message(item.chat_id)
            suggestion = pending.suggestion if pending else kv_data.get("antwort")
            keyboard = _chat_actions_keyboard(item.chat_id)
            if update.message:
                summary = _format_chat_overview(
                    item.chat_id,
                    item.title,
                    item.updated_at,
                    kv_data,
                    suggestion,
                    pending,
                )
                await update.message.reply_text(summary, reply_markup=keyboard)

    async def callback_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not update.effective_chat or update.effective_chat.id != allowed_chat_id:
            await _safe_edit_message(query, "Nicht autorisiert.")
            return

        data = query.data or ""
        try:
            action, chat_id_raw = data.split(":", maxsplit=1)
            chat_id = int(chat_id_raw)
        except ValueError:
            await _safe_edit_message(query, "Ungültige Aktion.")
            return

        keyboard = _chat_actions_keyboard(chat_id)

        if action == "generate":
            base_text = query.message.text if (query.message and query.message.text) else f"Chat {chat_id}"
            await _safe_edit_message(query, f"{base_text}\n\n⏳ Starte Generierung...", reply_markup=keyboard)

            runtime_warnings: list[str] = []

            def _on_run_warning(event_chat_id: int, message: str) -> None:
                if event_chat_id == chat_id:
                    runtime_warnings.append(message)

            try:
                summary = await service.run_once(target_chat_ids={chat_id}, on_warning=_on_run_warning)
            except Exception as exc:
                await _safe_edit_message(
                    query,
                    f"{base_text}\n\n❌ Generierung fehlgeschlagen: {exc}",
                    reply_markup=keyboard,
                )
                return

            warning_line = f"\n⚠️ {runtime_warnings[-1]}" if runtime_warnings else ""
            info = _build_process_infobox(chat_id, "✅ Generierung abgeschlossen")
            await _safe_edit_message(
                query,
                (
                    f"{info}\n"
                    f"Chats: {summary.chat_count}\n"
                    f"Gesendet: {summary.sent_count}\n"
                    f"Zeit: {summary.finished_at:%Y-%m-%d %H:%M:%S}"
                    f"{warning_line}"
                ),
                reply_markup=keyboard,
            )
            return

        if action == "autoon":
            service.set_chat_auto(chat_id, enabled=True)
            await _safe_edit_message(
                query,
                _build_process_infobox(chat_id, "▶️ Auto-Senden für diesen Chat aktiviert."),
                reply_markup=keyboard,
            )
            return

        if action == "autooff":
            service.set_chat_auto(chat_id, enabled=False)
            await _safe_edit_message(
                query,
                _build_process_infobox(chat_id, "⏸ Auto-Senden für diesen Chat deaktiviert."),
                reply_markup=keyboard,
            )
            return

        if action == "send":
            started = await service.trigger_send(chat_id, trigger="bot-send-button")
            if not started:
                await _safe_edit_message(
                    query,
                    _build_process_infobox(chat_id, "⚠️ Kein sendbarer Zustand vorhanden."),
                    reply_markup=keyboard,
                )
                return
            await _safe_edit_message(
                query,
                _build_process_infobox(chat_id, "▶️ Senden ausgelöst (oder nach Generierung vorgemerkt)."),
                reply_markup=keyboard,
            )
            return

        if action == "stop":
            result = await service.abort_send(chat_id)
            await _safe_edit_message(
                query,
                _build_process_infobox(chat_id, f"⏹ {result}"),
                reply_markup=keyboard,
            )
            return

        if action == "img":
            await _safe_edit_message(
                query,
                f"Chat {chat_id}\n\n🖼 Lade letzte Bilder und poste sie im Kontrollkanal...",
                reply_markup=keyboard,
            )
            images = await service.core.get_recent_images_with_captions_for_control_channel(chat_id, limit=3)
            if not images:
                await _safe_edit_message(
                    query,
                    f"Chat {chat_id}\n\nKeine Bilder gefunden oder keine Caption erzeugt.",
                    reply_markup=keyboard,
                )
                return
            for image_bytes, caption in images:
                await context.bot.send_photo(
                    chat_id=allowed_chat_id,
                    photo=InputFile(image_bytes, filename=f"chat_{chat_id}.jpg"),
                    caption=caption[:1024],
                )
            await _safe_edit_message(
                query,
                f"Chat {chat_id}\n\n✅ {len(images)} Bild(er) mit Caption im Kontrollkanal gepostet.",
                reply_markup=keyboard,
            )
            return

        if action == "kv":
            if not service.store:
                await _safe_edit_message(query, "Keine Datenbank konfiguriert.")
                return
            items = service.store.kv_list(chat_id, limit=20)
            if not items:
                await _safe_edit_message(query, f"Keine Keys für {chat_id} gespeichert.")
                return
            lines = [f"- {item.key}={item.value}" for item in items]
            await _safe_edit_message(query, f"KV Store fuer {chat_id}:\n" + "\n".join(lines))
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

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("runonce", run_once))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("kvset", kv_set))
    app.add_handler(CommandHandler("kvget", kv_get))
    app.add_handler(CommandHandler("kvdel", kv_del))
    app.add_handler(CommandHandler("kvlist", kv_list))
    app.add_handler(CommandHandler("promptpreview", prompt_preview))
    app.add_handler(CallbackQueryHandler(callback_action, pattern=r"^(generate|send|stop|autoon|autooff|img|kv):"))
    return app
