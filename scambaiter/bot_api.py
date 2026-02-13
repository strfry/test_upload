from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from scambaiter.service import BackgroundService


def create_bot_app(token: str, service: BackgroundService, allowed_chat_id: int | None = None) -> Application:
    app = Application.builder().token(token).build()
    max_message_len = 3500
    help_text = (
        "Verfügbare Kommandos:\n"
        "- /help – diese Hilfe\n"
        "- /status – Auto-Status und letzter Lauf\n"
        "- /runonce – startet sofort einen Einmaldurchlauf\n"
        "- /runonce <chat_id[,chat_id2,...]> – Einmaldurchlauf nur für bestimmte Chat-IDs\n"
        "- /startauto – startet den Auto-Modus\n"
        "- /stopauto – stoppt den Auto-Modus\n"
        "- /last – letzte Vorschläge (max. 5)\n"
        "- /history – letzte persistierte Analysen\n"
        "- /kvset <scammer_chat_id> <key> <value> – Key setzen/überschreiben\n"
        "- /kvget <scammer_chat_id> <key> – Key lesen\n"
        "- /kvdel <scammer_chat_id> <key> – Key löschen\n"
        "- /kvlist <scammer_chat_id> – Keys auflisten"
    )

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

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _guarded_reply(update, help_text)

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

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("start", help_cmd))
    app.add_handler(CommandHandler("runonce", run_once))
    app.add_handler(CommandHandler("startauto", start_auto))
    app.add_handler(CommandHandler("stopauto", stop_auto))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("kvset", kv_set))
    app.add_handler(CommandHandler("kvget", kv_get))
    app.add_handler(CommandHandler("kvdel", kv_del))
    app.add_handler(CommandHandler("kvlist", kv_list))
    return app
