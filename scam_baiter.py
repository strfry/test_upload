#!/usr/bin/env python3
"""Telegram Scambaiter suggestion tool.

Batch mode (default): one run with optional interactive send.
Bot mode: Telegram Bot API control for run/start/stop/status/insights.
"""

from __future__ import annotations

import asyncio

from scambaiter.bot_api import create_bot_app
from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
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
        output = core.generate_output(context)
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
    core = ScambaiterCore(config)
    store = AnalysisStore(config.analysis_db_path)
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
        print(
            "BotAPI aktiv. Verfügbare Kommandos: "
            "/status /runonce /startauto /stopauto /last /history /kvset /kvget /kvdel /kvlist"
        )
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await service.stop_auto()
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
    finally:
        await core.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
