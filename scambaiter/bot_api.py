from __future__ import annotations

from dataclasses import dataclass, replace

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from scambaiter.config import AppConfig
from scambaiter.core import ScambaiterCore
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore


@dataclass
class UserRuntime:
    core: ScambaiterCore
    service: BackgroundService


@dataclass
class LoginState:
    phone: str | None = None
    phone_code_hash: str | None = None
    waiting_code: bool = False
    waiting_password: bool = False


class MultiUserManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = AnalysisStore(config.analysis_db_path)
        self._runtimes: dict[int, UserRuntime] = {}
        self._login_states: dict[int, LoginState] = {}

    def _user_config(self, user_id: int) -> AppConfig:
        session = f"{self.config.telegram_session}_botuser_{user_id}"
        return replace(self.config, telegram_session=session)

    async def _runtime_for(self, user_id: int) -> UserRuntime:
        runtime = self._runtimes.get(user_id)
        if runtime:
            return runtime

        core = ScambaiterCore(self._user_config(user_id))
        await core.client.connect()
        runtime = UserRuntime(
            core=core,
            service=BackgroundService(core, interval_seconds=self.config.auto_interval_seconds, store=self.store),
        )
        self._runtimes[user_id] = runtime
        return runtime

    async def is_authorized(self, user_id: int) -> bool:
        runtime = await self._runtime_for(user_id)
        return await runtime.core.client.is_user_authorized()

    async def begin_login(self, user_id: int, phone: str) -> str:
        runtime = await self._runtime_for(user_id)
        code = await runtime.core.client.send_code_request(phone)
        self._login_states[user_id] = LoginState(phone=phone, phone_code_hash=code.phone_code_hash, waiting_code=True)
        return phone

    async def submit_code(self, user_id: int, code: str) -> str:
        state = self._login_states.get(user_id)
        if not state or not state.waiting_code or not state.phone or not state.phone_code_hash:
            return "Kein ausstehender Login-Code. Starte mit /login <telefonnummer>."

        runtime = await self._runtime_for(user_id)
        try:
            await runtime.core.client.sign_in(phone=state.phone, code=code, phone_code_hash=state.phone_code_hash)
            self._login_states[user_id] = LoginState()
            return "Login erfolgreich. Du kannst jetzt alle Bot-Kommandos verwenden."
        except SessionPasswordNeededError:
            state.waiting_code = False
            state.waiting_password = True
            return "2FA-Passwort erforderlich. Bitte sende: /password <dein_passwort>"
        except PhoneCodeInvalidError:
            return "Der eingegebene Code ist ungültig. Bitte erneut mit /code <PIN>."
        except PhoneCodeExpiredError:
            self._login_states[user_id] = LoginState()
            return "Der Code ist abgelaufen. Bitte neuen Login starten: /login <telefonnummer>."

    async def submit_password(self, user_id: int, password: str) -> str:
        state = self._login_states.get(user_id)
        if not state or not state.waiting_password:
            return "Kein ausstehender 2FA-Login. Starte mit /login <telefonnummer>."

        runtime = await self._runtime_for(user_id)
        try:
            await runtime.core.client.sign_in(password=password)
            self._login_states[user_id] = LoginState()
            return "2FA erfolgreich. Login abgeschlossen."
        except PasswordHashInvalidError:
            return "Falsches 2FA-Passwort. Bitte erneut mit /password <dein_passwort>."

    async def logout(self, user_id: int) -> None:
        runtime = self._runtimes.get(user_id)
        if not runtime:
            self._login_states[user_id] = LoginState()
            return
        await runtime.service.stop_auto()
        await runtime.core.client.log_out()
        await runtime.core.close()
        self._login_states[user_id] = LoginState()
        self._runtimes.pop(user_id, None)

    async def runtime_if_authorized(self, user_id: int) -> UserRuntime | None:
        runtime = await self._runtime_for(user_id)
        if await runtime.core.client.is_user_authorized():
            return runtime
        return None


def create_bot_app(token: str, config: AppConfig, allowed_chat_id: int | None = None) -> Application:
    app = Application.builder().token(token).build()
    manager = MultiUserManager(config)
    max_message_len = 3500
    help_text = (
        "Verfügbare Kommandos:\n"
        "- /help – diese Hilfe\n"
        "- /login <telefonnummer> – startet Telethon-Login für deinen User\n"
        "- /code <PIN> – bestätigt den Telegram Login-Code\n"
        "- /password <passwort> – bestätigt optionales 2FA-Passwort\n"
        "- /logout – meldet deinen User wieder ab\n"
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

    def _authorized_chat(update: Update) -> bool:
        if allowed_chat_id is None:
            return True
        return bool(update.effective_chat and update.effective_chat.id == allowed_chat_id)

    async def _guarded_reply(update: Update, text: str) -> None:
        if not _authorized_chat(update):
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

    def _user_id(update: Update) -> int | None:
        if not update.effective_user:
            return None
        return update.effective_user.id

    async def _require_runtime(update: Update) -> UserRuntime | None:
        user_id = _user_id(update)
        if user_id is None:
            await _guarded_reply(update, "User konnte nicht ermittelt werden.")
            return None
        runtime = await manager.runtime_if_authorized(user_id)
        if runtime:
            return runtime
        await _guarded_reply(
            update,
            "Du bist für Telethon noch nicht eingeloggt. Starte bitte mit /login <telefonnummer> und sende dann /code <PIN>.",
        )
        return None

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _guarded_reply(update, help_text)

    async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized_chat(update):
            return
        user_id = _user_id(update)
        if user_id is None:
            await _guarded_reply(update, "User konnte nicht ermittelt werden.")
            return
        if len(context.args) != 1:
            await _guarded_reply(update, "Nutzung: /login <telefonnummer_im_internationalen_format>")
            return
        phone = context.args[0].strip()
        await manager.begin_login(user_id, phone)
        await _guarded_reply(update, f"Code gesendet an {phone}. Bitte antworte mit /code <PIN>.")

    async def code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized_chat(update):
            return
        user_id = _user_id(update)
        if user_id is None:
            await _guarded_reply(update, "User konnte nicht ermittelt werden.")
            return
        if len(context.args) != 1:
            await _guarded_reply(update, "Nutzung: /code <PIN>")
            return
        msg = await manager.submit_code(user_id, context.args[0].strip())
        await _guarded_reply(update, msg)

    async def password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized_chat(update):
            return
        user_id = _user_id(update)
        if user_id is None:
            await _guarded_reply(update, "User konnte nicht ermittelt werden.")
            return
        if len(context.args) != 1:
            await _guarded_reply(update, "Nutzung: /password <dein_2fa_passwort>")
            return
        msg = await manager.submit_password(user_id, context.args[0].strip())
        await _guarded_reply(update, msg)

    async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized_chat(update):
            return
        user_id = _user_id(update)
        if user_id is None:
            await _guarded_reply(update, "User konnte nicht ermittelt werden.")
            return
        await manager.logout(user_id)
        await _guarded_reply(update, "Du wurdest ausgeloggt.")

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        summary = runtime.service.last_summary
        if not summary:
            await _guarded_reply(update, "Noch kein Lauf ausgeführt.")
            return
        await _guarded_reply(
            update,
            (
                f"Auto-Modus: {'AN' if runtime.service.auto_enabled else 'AUS'}\n"
                f"Letzter Lauf: {summary.finished_at:%Y-%m-%d %H:%M:%S}\n"
                f"Gefundene Chats: {summary.chat_count}\n"
                f"Gesendete Nachrichten: {summary.sent_count}"
            ),
        )

    async def run_once(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
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
        summary = await runtime.service.run_once(target_chat_ids=target_chat_ids)
        await _guarded_reply(
            update,
            f"Fertig. Chats: {summary.chat_count}, gesendet: {summary.sent_count}",
        )

    async def start_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        await runtime.service.start_auto()
        await _guarded_reply(update, "Auto-Modus gestartet.")

    async def stop_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        await runtime.service.stop_auto()
        await _guarded_reply(update, "Auto-Modus gestoppt.")

    async def last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        if not runtime.service.last_results:
            await _guarded_reply(update, "Keine Analyse vorhanden.")
            return
        lines: list[str] = []
        for result in runtime.service.last_results[:5]:
            lines.append(f"- {result.context.title} ({result.context.chat_id}): {result.suggestion}")
        await _guarded_reply(update, "Letzte Vorschläge:\n" + "\n".join(lines))

    async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        if not runtime.service.store:
            await _guarded_reply(update, "Keine Datenbank konfiguriert.")
            return
        entries = runtime.service.store.latest(limit=5)
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
        runtime = await _require_runtime(update)
        if not runtime:
            return
        if not runtime.service.store:
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
        runtime.service.store.kv_set(scammer_chat_id, key, value)
        await _guarded_reply(update, f"Gespeichert für {scammer_chat_id}: {key}={value}")

    async def kv_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        if not runtime.service.store:
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
        item = runtime.service.store.kv_get(scammer_chat_id, key)
        if not item:
            await _guarded_reply(update, "Key nicht gefunden.")
            return
        await _guarded_reply(
            update,
            f"[{item.scammer_chat_id}] {item.key}={item.value} (updated {item.updated_at:%Y-%m-%d %H:%M:%S})",
        )

    async def kv_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        if not runtime.service.store:
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
        deleted = runtime.service.store.kv_delete(scammer_chat_id, key)
        await _guarded_reply(update, "Gelöscht." if deleted else "Key nicht gefunden.")

    async def kv_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        runtime = await _require_runtime(update)
        if not runtime:
            return
        if not runtime.service.store:
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
        items = runtime.service.store.kv_list(scammer_chat_id, limit=20)
        if not items:
            await _guarded_reply(update, "Keine Keys für diesen Scammer gespeichert.")
            return
        lines = [f"- {item.key}={item.value}" for item in items]
        await _guarded_reply(update, f"KV Store für {scammer_chat_id}:\n" + "\n".join(lines))

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("start", help_cmd))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("code", code))
    app.add_handler(CommandHandler("password", password))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("status", status))
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
