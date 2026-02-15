#!/usr/bin/env python3
"""Telegram Scambaiter suggestion tool.

Batch mode (default): one run with optional interactive send.
Bot mode: Telegram Bot API control for run/start/stop/status/insights.
"""

from __future__ import annotations

import asyncio
import shlex

from scambaiter.bot_api import create_bot_app
from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore


COMMAND_OVERVIEW = (
    "ControlBot ist aktiv. Wichtigste Kommandos:\n"
    "/status - Auto-Status + letzter Lauf\n"
    "/runonce [chat_id,...] - Einmallauf\n"
    "/chats [limit] - unbeantwortete Chats zeigen\n"
    "/startauto | /stopauto - Auto-Modus steuern\n"
    "/last | /history - letzte Ergebnisse\n"
    "/kvset /kvget /kvdel /kvlist - Variablen\n"
    "Zusätzlich auf STDIO: start/help, exit/quit"
)


async def run_batch(core: ScambaiterCore, store: AnalysisStore) -> None:
    folder_chat_ids = await core.get_folder_chat_ids()
    contexts = await core.collect_unanswered_chats(folder_chat_ids)

    if not contexts:
        print("Keine unbeantworteten Chats im Ordner gefunden.")
        return

    print(f"Gefundene unbeantwortete Chats: {len(contexts)}\n")
    for index, context in enumerate(contexts, start=1):
        language_hint = None
        lang_item = store.kv_get(context.chat_id, "sprache")
        if lang_item:
            language_hint = lang_item.value
        output = core.generate_output(context, language_hint=language_hint)
        store.save(
            chat_id=context.chat_id,
            title=context.title,
            suggestion=output.suggestion,
            analysis=output.analysis,
            metadata=output.metadata,
        )
        print(f"=== Vorschlag {index}: {context.title} (ID: {context.chat_id}) ===")
        print(output.suggestion)
        if output.metadata:
            meta = ", ".join(f"{k}={v}" for k, v in output.metadata.items())
            print(f"[Meta] {meta}")
        print()
        handled = await core.maybe_interactive_console_reply(context, output.suggestion)
        if not handled:
            await core.maybe_send_suggestion(context, output.suggestion)


async def _run_stdio_command_loop(service: BackgroundService) -> None:
    print("STDIO-Steuerung aktiv. 'help' für Befehle, 'exit' zum Beenden.")
    while True:
        try:
            line = await asyncio.to_thread(input, "control> ")
        except EOFError:
            print("STDIO beendet (EOF).")
            return

        raw = line.strip()
        if not raw:
            continue

        if raw.startswith("/"):
            raw = raw[1:]

        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Ungültige Eingabe: {exc}")
            continue

        if not parts:
            continue

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in {"exit", "quit"}:
            print("Beende ControlBot...")
            return

        if cmd in {"start", "help"}:
            print(COMMAND_OVERVIEW)
            continue

        if cmd == "status":
            summary = service.last_summary
            if not summary:
                print("Noch kein Lauf ausgeführt.")
                continue
            print(
                f"Auto-Modus: {'AN' if service.auto_enabled else 'AUS'}\n"
                f"Letzter Lauf: {summary.finished_at:%Y-%m-%d %H:%M:%S}\n"
                f"Gefundene Chats: {summary.chat_count}\n"
                f"Gesendete Nachrichten: {summary.sent_count}"
            )
            continue

        if cmd == "runonce":
            target_chat_ids: set[int] | None = None
            if args:
                target_chat_ids = set()
                parse_failed = False
                for arg in args:
                    for token in arg.split(","):
                        token = token.strip()
                        if not token:
                            continue
                        try:
                            target_chat_ids.add(int(token))
                        except ValueError:
                            print("Ungültige Chat-ID. Nutzung: runonce oder runonce <chat_id[,chat_id2,...]>")
                            parse_failed = True
                            break
                    if parse_failed:
                        break
                if parse_failed:
                    continue

            summary = await service.run_once(target_chat_ids=target_chat_ids)
            print(f"Fertig. Chats: {summary.chat_count}, gesendet: {summary.sent_count}")
            continue

        if cmd == "chats":
            limit = 10
            if args:
                try:
                    limit = max(1, min(20, int(args[0])))
                except ValueError:
                    print("Nutzung: chats [limit]")
                    continue

            folder_chat_ids = await service.core.get_folder_chat_ids()
            contexts = await service.core.collect_unanswered_chats(folder_chat_ids)
            if not contexts:
                print("Keine unbeantworteten Chats gefunden.")
                continue

            print("Unbeantwortete Chats:")
            for item in contexts[:limit]:
                print(f"- {item.title} ({item.chat_id})")
            continue

        if cmd == "startauto":
            await service.start_auto()
            print("Auto-Modus gestartet.")
            continue

        if cmd == "stopauto":
            await service.stop_auto()
            print("Auto-Modus gestoppt.")
            continue

        if cmd == "last":
            if not service.last_results:
                print("Keine Analyse vorhanden.")
                continue
            print("Letzte Vorschläge:")
            for result in service.last_results[:5]:
                print(f"- {result.context.title} ({result.context.chat_id}): {result.suggestion}")
            continue

        if cmd == "history":
            if not service.store:
                print("Keine Datenbank konfiguriert.")
                continue
            entries = service.store.latest(limit=5)
            if not entries:
                print("Keine gespeicherten Analysen vorhanden.")
                continue
            print("Persistierte Analysen:")
            for item in entries:
                line = f"- {item.created_at:%Y-%m-%d %H:%M} | {item.title} ({item.chat_id})"
                print(line)
                if item.metadata:
                    print("  Meta=" + ",".join(f"{k}={v}" for k, v in item.metadata.items()))
                if item.analysis:
                    print(f"  Analyse={item.analysis}")
            continue

        if cmd == "kvset":
            if not service.store:
                print("Keine Datenbank konfiguriert.")
                continue
            if len(args) < 3:
                print("Nutzung: kvset <scammer_chat_id> <key> <value>")
                continue
            try:
                scammer_chat_id = int(args[0])
            except ValueError:
                print("scammer_chat_id muss eine Zahl sein.")
                continue
            key = args[1].strip().lower()
            value = " ".join(args[2:]).strip()
            if not key or not value:
                print("Nutzung: kvset <scammer_chat_id> <key> <value>")
                continue
            service.store.kv_set(scammer_chat_id, key, value)
            print(f"Gespeichert für {scammer_chat_id}: {key}={value}")
            continue

        if cmd == "kvget":
            if not service.store:
                print("Keine Datenbank konfiguriert.")
                continue
            if len(args) != 2:
                print("Nutzung: kvget <scammer_chat_id> <key>")
                continue
            try:
                scammer_chat_id = int(args[0])
            except ValueError:
                print("scammer_chat_id muss eine Zahl sein.")
                continue
            key = args[1].strip().lower()
            item = service.store.kv_get(scammer_chat_id, key)
            if not item:
                print("Key nicht gefunden.")
                continue
            print(f"[{item.scammer_chat_id}] {item.key}={item.value} (updated {item.updated_at:%Y-%m-%d %H:%M:%S})")
            continue

        if cmd == "kvdel":
            if not service.store:
                print("Keine Datenbank konfiguriert.")
                continue
            if len(args) != 2:
                print("Nutzung: kvdel <scammer_chat_id> <key>")
                continue
            try:
                scammer_chat_id = int(args[0])
            except ValueError:
                print("scammer_chat_id muss eine Zahl sein.")
                continue
            key = args[1].strip().lower()
            deleted = service.store.kv_delete(scammer_chat_id, key)
            print("Gelöscht." if deleted else "Key nicht gefunden.")
            continue

        if cmd == "kvlist":
            if not service.store:
                print("Keine Datenbank konfiguriert.")
                continue
            if len(args) != 1:
                print("Nutzung: kvlist <scammer_chat_id>")
                continue
            try:
                scammer_chat_id = int(args[0])
            except ValueError:
                print("scammer_chat_id muss eine Zahl sein.")
                continue
            items = service.store.kv_list(scammer_chat_id, limit=20)
            if not items:
                print("Keine Keys für diesen Scammer gespeichert.")
                continue
            print(f"KV Store für {scammer_chat_id}:")
            for item in items:
                print(f"- {item.key}={item.value}")
            continue

        print(f"Unbekannter Befehl: {cmd}. Nutze 'help'.")


async def run() -> None:
    config = load_config()
    store = AnalysisStore(config.analysis_db_path)
    core = ScambaiterCore(config, store=store)
    service: BackgroundService | None = None
    bot_app = None

    await core.start()
    try:
        if not config.bot_token:
            await run_batch(core, store)
            return

        service = BackgroundService(core, interval_seconds=config.auto_interval_seconds, store=store)
        bot_app = create_bot_app(
            token=config.bot_token,
            service=service,
            allowed_chat_id=config.bot_allowed_chat_id,
        )

        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()

        if config.bot_allowed_chat_id is not None:
            await bot_app.bot.send_message(chat_id=config.bot_allowed_chat_id, text=COMMAND_OVERVIEW)

        await _run_stdio_command_loop(service)
    finally:
        if service is not None:
            await service.stop_auto()
        if bot_app is not None:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
        await core.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
