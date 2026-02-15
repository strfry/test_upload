from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from scambaiter.service import BackgroundService


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

    def _truncate_value(value: str, max_len: int = 60) -> str:
        value = value.strip()
        if len(value) <= max_len:
            return value
        return value[: max_len - 3].rstrip() + "..."

    def _format_chat_overview(chat_id: int, title: str, updated_at: object, kv_data: dict[str, str]) -> str:
        lines = [f"{title} ({chat_id})"]
        if hasattr(updated_at, "strftime"):
            lines.append(f"Zuletzt: {updated_at:%Y-%m-%d %H:%M}")
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
        if not service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return

        known_chats = service.store.list_known_chats(limit=50)
        if not known_chats:
            await _guarded_reply(update, "Keine Chats in der Datenbank gefunden.")
            return

        for item in known_chats:
            kv_data: dict[str, str] = {}
            if service.store:
                kv_data = service.store.kv_get_many(item.chat_id, base_info_keys)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("▶️ Run", callback_data=f"run:{item.chat_id}"),
                        InlineKeyboardButton("🗂 KV", callback_data=f"kv:{item.chat_id}"),
                    ]
                ]
            )
            if update.message:
                summary = _format_chat_overview(item.chat_id, item.title, item.updated_at, kv_data)
                await update.message.reply_text(
                    summary,
                    reply_markup=keyboard,
                )

    async def callback_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not update.effective_chat or update.effective_chat.id != allowed_chat_id:
            await query.edit_message_text("Nicht autorisiert.")
            return

        data = query.data or ""
        try:
            action, chat_id_raw = data.split(":", maxsplit=1)
            chat_id = int(chat_id_raw)
        except ValueError:
            await query.edit_message_text("Ungültige Aktion.")
            return

        if action == "run":
            await query.edit_message_text(f"Starte Einzellauf für {chat_id}...")
            summary = await service.run_once(target_chat_ids={chat_id})
            await context.bot.send_message(
                chat_id=allowed_chat_id,
                text=f"Einzellauf fuer {chat_id} abgeschlossen. Chats: {summary.chat_count}, gesendet: {summary.sent_count}",
            )
            return

        if action == "kv":
            if not service.store:
                await query.edit_message_text("Keine Datenbank konfiguriert.")
                return
            items = service.store.kv_list(chat_id, limit=20)
            if not items:
                await query.edit_message_text(f"Keine Keys für {chat_id} gespeichert.")
                return
            lines = [f"- {item.key}={item.value}" for item in items]
            await query.edit_message_text(f"KV Store fuer {chat_id}:\n" + "\n".join(lines))
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

    async def test_image_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        chat_id = _parse_chat_id_arg(context.args)
        if chat_id is None:
            await _guarded_reply(update, "Nutzung: /testimagedesc <scammer_chat_id>")
            return

        await _guarded_reply(update, "Teste Bildbeschreibung fuer die letzten Bilder im Chat...")
        descriptions = await service.core.describe_recent_images_for_chat(chat_id, limit=3)
        if not descriptions:
            await _guarded_reply(update, "Keine Bilder gefunden oder keine Beschreibung erzeugt.")
            return
        await _guarded_reply_chunks(update, "Bildbeschreibungen:\n" + "\n".join(f"- {item}" for item in descriptions))

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
    app.add_handler(CallbackQueryHandler(callback_action, pattern=r"^(run|kv):"))
    return app

