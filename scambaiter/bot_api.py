from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from scambaiter.service import BackgroundService


def create_bot_app(token: str, service: BackgroundService, allowed_chat_id: int | None = None) -> Application:
    app = Application.builder().token(token).build()
    max_message_len = 3500

    def _authorized(update: Update) -> bool:
        if allowed_chat_id is None:
            return True
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

    def _parse_chat_id_arg(args: list[str]) -> int | None:
        if len(args) != 1:
            return None
        try:
            return int(args[0].strip())
        except ValueError:
            return None

    def _command_overview() -> str:
        return (
            "ControlBot ist aktiv. Wichtigste Kommandos:\n"
            "/status - Auto-Status + letzter Lauf\n"
            "/runonce [chat_id,...] - Einmallauf\n"
            "/chats - Chats mit Klick-Buttons (Run/Variablen)\n"
            "/startauto | /stopauto - Auto-Modus steuern\n"
            "/last | /history - letzte Ergebnisse\n"
            "/kvset /kvget /kvdel /kvlist - Variablen"
        )

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _guarded_reply(update, _command_overview())

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        summary = service.last_summary
        if not summary:
            await _guarded_reply(update, "Noch kein Lauf ausgeführt.")
            return
        await _guarded_reply(
            update,
            (
                f"Auto-Modus: {'AN' if service.auto_enabled else 'AUS'}\n"
                f"Letzter Lauf: {summary.finished_at:%Y-%m-%d %H:%M:%S}\n"
                f"Gefundene Chats: {summary.chat_count}\n"
                f"Gesendete Nachrichten: {summary.sent_count}"
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
        await _guarded_reply(
            update,
            f"Fertig. Chats: {summary.chat_count}, gesendet: {summary.sent_count}",
        )

    async def chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        limit = 10
        if context.args:
            try:
                limit = max(1, min(20, int(context.args[0])))
            except ValueError:
                await _guarded_reply(update, "Nutzung: /chats [limit]")
                return

        folder_chat_ids = await service.core.get_folder_chat_ids()
        contexts = await service.core.collect_unanswered_chats(folder_chat_ids)
        if not contexts:
            await _guarded_reply(update, "Keine unbeantworteten Chats gefunden.")
            return

        selected = contexts[:limit]
        keyboard = []
        for item in selected:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=f"▶ Run {item.title[:20]}",
                        callback_data=f"run:{item.chat_id}",
                    ),
                    InlineKeyboardButton(
                        text="Variablen",
                        callback_data=f"kv:{item.chat_id}",
                    ),
                ]
            )

        await update.message.reply_text(
            "Chats auswählen (Einzellauf oder Variablen):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def chat_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        if not _authorized(update):
            await query.answer("Nicht autorisiert.", show_alert=True)
            return

        await query.answer()
        data = query.data or ""
        if ":" not in data:
            await query.edit_message_text("Ungültige Aktion.")
            return

        action, raw_chat_id = data.split(":", 1)
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            await query.edit_message_text("Ungültige Chat-ID.")
            return

        if action == "run":
            summary = await service.run_once(target_chat_ids={chat_id})
            if summary.chat_count == 0:
                await query.edit_message_text(f"Für Chat {chat_id} wurde kein Verlauf gefunden.")
                return
            suggestion_text = ""
            if service.last_results:
                suggestion_text = service.last_results[0].suggestion
            message = f"Einzellauf für {chat_id} abgeschlossen. Gesendet: {summary.sent_count}."
            if suggestion_text:
                message += f"\n\nVorschlag:\n{suggestion_text}"
            await query.edit_message_text(message[:max_message_len])
            return

        if action == "kv":
            if not service.store:
                await query.edit_message_text("Keine Datenbank konfiguriert.")
                return
            items = service.store.kv_list(chat_id, limit=20)
            if not items:
                await query.edit_message_text(f"Keine Variablen für {chat_id} gespeichert.")
                return
            lines = [f"- {item.key}={item.value}" for item in items]
            text = f"Variablen für {chat_id}:\n" + "\n".join(lines)
            await query.edit_message_text(text[:max_message_len])
            return

        await query.edit_message_text("Unbekannte Aktion.")

    async def start_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await service.start_auto()
        await _guarded_reply(update, "Auto-Modus gestartet.")

    async def stop_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await service.stop_auto()
        await _guarded_reply(update, "Auto-Modus gestoppt.")

    async def last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not service.last_results:
            await _guarded_reply(update, "Keine Analyse vorhanden.")
            return
        lines: list[str] = []
        for result in service.last_results[:5]:
            lines.append(f"- {result.context.title} ({result.context.chat_id}): {result.suggestion}")
        await _guarded_reply(update, "Letzte Vorschläge:\n" + "\n".join(lines))

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
        await _guarded_reply(update, f"Gespeichert für {scammer_chat_id}: {key}={value}")

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
        await _guarded_reply(update, "Gelöscht." if deleted else "Key nicht gefunden.")

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
            await _guarded_reply(update, "Keine Keys für diesen Scammer gespeichert.")
            return
        lines = [f"- {item.key}={item.value}" for item in items]
        await _guarded_reply(update, f"KV Store für {scammer_chat_id}:\n" + "\n".join(lines))

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

    async def test_image_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        chat_id = _parse_chat_id_arg(context.args)
        if chat_id is None:
            await _guarded_reply(update, "Nutzung: /testimagedesc <scammer_chat_id>")
            return

        await _guarded_reply(update, "Teste Bildbeschreibung für die letzten Bilder im Chat...")
        descriptions = await service.core.describe_recent_images_for_chat(chat_id, limit=3)
        if not descriptions:
            await _guarded_reply(update, "Keine Bilder gefunden oder keine Beschreibung erzeugt.")
            return
        await _guarded_reply_chunks(update, "Bildbeschreibungen:\n" + "\n".join(f"- {item}" for item in descriptions))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("runonce", run_once))
    app.add_handler(CommandHandler("chats", chats))
    app.add_handler(CommandHandler("startauto", start_auto))
    app.add_handler(CommandHandler("stopauto", stop_auto))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("kvset", kv_set))
    app.add_handler(CommandHandler("kvget", kv_get))
    app.add_handler(CommandHandler("kvdel", kv_del))
    app.add_handler(CommandHandler("kvlist", kv_list))
    app.add_handler(CommandHandler("promptpreview", prompt_preview))
    app.add_handler(CommandHandler("testimagedesc", test_image_desc))
    app.add_handler(CallbackQueryHandler(chat_action, pattern=r"^(run|kv):"))
    return app
