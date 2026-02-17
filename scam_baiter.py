#!/usr/bin/env python3
"""Telegram Scambaiter suggestion tool.

Batch mode (default): one run with optional interactive send.
Bot mode: Telegram Bot API control for run/start/stop/insights.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram import Bot

from scambaiter.bot_api import create_bot_app
from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore


def merge_analysis(
    previous: dict[str, object] | None,
    current: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(previous, dict) and not isinstance(current, dict):
        return None
    if not isinstance(previous, dict):
        return dict(current or {})
    if not isinstance(current, dict):
        return dict(previous)
    merged: dict[str, object] = dict(previous)
    for key, value in current.items():
        old_value = merged.get(key)
        if isinstance(old_value, dict) and isinstance(value, dict):
            nested = merge_analysis(old_value, value)
            merged[key] = nested if isinstance(nested, dict) else {}
        else:
            merged[key] = value
    return merged


async def run_batch(core: ScambaiterCore, store: AnalysisStore) -> None:
    folder_chat_ids = await core.get_folder_chat_ids()
    contexts = await core.collect_folder_chats(folder_chat_ids)

    if not contexts:
        print("Keine Chats im Ordner gefunden.")
        return

    print(f"Gefundene Chats im Ordner: {len(contexts)}\n")
    for index, context in enumerate(contexts, start=1):
        language_hint = None
        prompt_context: dict[str, object] = {
            "messenger": "telegram",
            "now_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        previous = store.latest_for_chat(context.chat_id)
        previous_analysis = previous.analysis if previous else None
        if previous_analysis:
            prompt_context["previous_analysis"] = previous_analysis
            for key in ("language", "sprache"):
                value = previous_analysis.get(key)
                if isinstance(value, str) and value.strip():
                    language_hint = value.strip()
                    break
        recent_entries = store.recent_for_chat(context.chat_id, limit=6)
        recent_assistant_messages = [
            item.suggestion.strip()
            for item in reversed(recent_entries)
            if isinstance(item.suggestion, str) and item.suggestion.strip()
        ]
        if recent_assistant_messages:
            prompt_context["recent_assistant_messages"] = recent_assistant_messages[-5:]
        directives = store.list_directives(chat_id=context.chat_id, active_only=True, limit=25)
        if directives:
            prompt_context["operator"] = {
                "directives": [
                    {"id": str(item.id), "text": item.text, "scope": item.scope}
                    for item in directives
                ]
            }
        output = core.generate_output(
            context,
            language_hint=language_hint,
            prompt_context=prompt_context,
        )
        merged_analysis = merge_analysis(previous_analysis, output.analysis)
        store.save(
            chat_id=context.chat_id,
            title=context.title,
            suggestion=output.suggestion,
            analysis=merged_analysis,
            actions=output.actions,
            metadata=output.metadata,
        )
        print(f"=== Vorschlag {index}: {context.title} (ID: {context.chat_id}) ===")
        print(output.suggestion)
        if output.metadata:
            meta = ", ".join(f"{k}={v}" for k, v in output.metadata.items())
            print(f"[Meta] {meta}")
        if merged_analysis:
            print(f"[Analysis] {merged_analysis}")
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

        bot_me = await Bot(config.bot_token).get_me()
        control_chat_id = await core.resolve_control_chat_id(bot_me.username)
        service = BackgroundService(core, interval_seconds=config.auto_interval_seconds, store=store)
        bot_app = create_bot_app(
            token=config.bot_token,
            service=service,
            allowed_chat_id=control_chat_id,
        )
        print(
            "BotAPI aktiv. verfügbare Kommandos: "
            "/runonce /chats /last /history /analysisget /analysisset"
        )
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()
        service.start_startup_bootstrap()
        service.start_periodic_run()
        send_start_menu = bot_app.bot_data.get("send_start_menu")
        if callable(send_start_menu):
            await send_start_menu()
        else:
            await bot_app.bot.send_message(chat_id=control_chat_id, text="Scambaiter Bot gestartet. /chats")
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
