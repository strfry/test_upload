#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os

from scambaiter.bot_api import create_bot_app
from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore
from scambaiter.telethon_executor import TelethonExecutor


async def _run() -> None:
    config = load_config()
    token = config.bot_token or os.getenv("SCAMBAITER_BOT_TOKEN")
    if not token:
        raise RuntimeError("SCAMBAITER_BOT_TOKEN is required")
    allowed = os.getenv("SCAMBAITER_CONTROL_CHAT_ID")
    allowed_chat_id = int(allowed) if allowed else None
    telethon_executor = None
    if config.telethon_api_id and config.telethon_api_hash:
        try:
            telethon_executor = TelethonExecutor(
                api_id=config.telethon_api_id,
                api_hash=config.telethon_api_hash,
                session=config.telethon_session,
            )
            await telethon_executor.start()
        except Exception:
            telethon_executor = None

    store = AnalysisStore(config.analysis_db_path)
    core = ScambaiterCore(config=config, store=store)
    service = BackgroundService(core=core, interval_seconds=config.auto_interval_seconds, store=store)

    app = create_bot_app(
        token=token,
        service=service,
        allowed_chat_id=allowed_chat_id,
        telethon_executor=telethon_executor,
    )
    await app.initialize()
    await app.start()
    folder_name = os.getenv("SCAMBAITER_FOLDER_NAME", "Scammers")
    if telethon_executor is not None:
        await telethon_executor.start_listener(
            store=store,
            service=service,
            config=config,
            folder_name=folder_name,
        )
        try:
            await telethon_executor.startup_backfill(store=store, config=config, folder_name=folder_name)
        except Exception as exc:
            raise RuntimeError(f"startup_backfill failed: {exc}") from exc
    register_command_menu = app.bot_data.get("register_command_menu")
    if callable(register_command_menu):
        await register_command_menu()
    await app.updater.start_polling()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if telethon_executor is not None:
            await telethon_executor.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
