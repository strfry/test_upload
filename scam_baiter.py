#!/usr/bin/env python3
"""Telegram Scambaiter suggestion tool.

Batch mode (default): one run with optional interactive send.
Bot mode: Telegram Bot API control for run/start/stop/status/insights.
"""

from __future__ import annotations

import asyncio

from telegram import Bot

from scambaiter.bot_api import create_bot_app
from scambaiter.config import load_config
from scambaiter.core import PROMPT_KV_KEYS, ScambaiterCore
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore


async def run_batch(core: ScambaiterCore, store: AnalysisStore) -> None:
    folder_chat_ids = await core.get_folder_chat_ids()
    contexts = await core.collect_unanswered_chats(folder_chat_ids)

    if not contexts:
        print("Keine unbeantworteten Chats im Ordner gefunden.")
        return

    print(f"Gefundene unbeantwortete Chats: {len(contexts)}\n")
    for index, context in enumerate(contexts, start=1):
        language_hint = None
        prompt_kv_state: dict[str, str] = {}
        lang_item = store.kv_get(context.chat_id, "sprache")
        if lang_item:
            language_hint = lang_item.value
        prompt_kv_state = store.kv_get_many(context.chat_id, list(PROMPT_KV_KEYS))
        output = core.generate_output(
            context,
            language_hint=language_hint,
            prompt_kv_state=prompt_kv_state,
        )
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


async def run() -> None:
    config = load_config()
    store = AnalysisStore(config.analysis_db_path)
    core = ScambaiterCore(config, store=store)
    await core.start()
    try:
        if not config.bot_token:
            await run_batch(core, store)
            return

        service = BackgroundService(core, interval_seconds=config.auto_interval_seconds, store=store)
        try:
            initial_scanned = await service.scan_folder(force=False)
            if initial_scanned:
                print(f"Initialer Ordner-Scan: {initial_scanned} Vorschlaege erzeugt.")
        except Exception as exc:
            print(f"[WARN] Initialer Ordner-Scan fehlgeschlagen: {exc}")
        bot_me = await Bot(config.bot_token).get_me()
        control_chat_id = await core.resolve_control_chat_id(bot_me.username)
        bot_app = create_bot_app(
            token=config.bot_token,
            service=service,
            allowed_chat_id=control_chat_id,
        )
        print(
            "BotAPI aktiv. verfügbare Kommandos: "
            "/status /runonce /scan /chats /last /history /kvset /kvget /kvdel /kvlist"
        )
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        await bot_app.bot.send_message(
            chat_id=control_chat_id,
            text=(
                "Scambaiter Bot gestartet.\n"
                "Kommandos: /status /runonce /scan /chats /last /history /kvset /kvget /kvdel /kvlist"
            ),
        )
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await service.shutdown()
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
    finally:
        await core.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()


