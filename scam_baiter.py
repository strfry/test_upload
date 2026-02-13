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


async def run() -> None:
    config = load_config()

    if not config.bot_token:
        core = ScambaiterCore(config)
        store = AnalysisStore(config.analysis_db_path)
        await core.start()
        try:
            await run_batch(core, store)
        finally:
            await core.close()
        return

    bot_app = create_bot_app(
        token=config.bot_token,
        config=config,
        allowed_chat_id=config.bot_allowed_chat_id,
    )
    print(
        "BotAPI aktiv. Verfügbare Kommandos: "
        "/help /login /code /password /logout /status /runonce /startauto /stopauto /last /history /kvset /kvget /kvdel /kvlist"
    )
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
