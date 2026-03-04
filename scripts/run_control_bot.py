#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os

_log = logging.getLogger(__name__)

from scambaiter.bot_api import create_bot_app
from scambaiter.config import load_config
from scambaiter.core import ScambaiterCore
from scambaiter.service import BackgroundService
from scambaiter.storage import AnalysisStore
from scambaiter.telethon_executor import TelethonExecutor


async def _run(run_timeout: int | None = None, skip_backfill: bool = False) -> None:
    config = load_config()
    token = config.bot_token or os.getenv("SCAMBAITER_BOT_TOKEN")
    if not token:
        raise RuntimeError("SCAMBAITER_BOT_TOKEN is required")
    allowed = os.getenv("SCAMBAITER_CONTROL_CHAT_ID")
    allowed_chat_id = int(allowed) if allowed else None
    group_chat_id = os.getenv("SCAMBAITER_GROUP_CHAT_ID")
    extra_allowed = [int(group_chat_id)] if group_chat_id else []
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
        extra_allowed_chat_ids=extra_allowed,
    )
    await app.initialize()
    await app.start()
    folder_name = os.getenv("SCAMBAITER_FOLDER_NAME", "Scammers")
    if telethon_executor is not None:
        control_ids: set[int] = set()
        if allowed_chat_id is not None:
            control_ids.add(allowed_chat_id)
        for cid in extra_allowed:
            control_ids.add(cid)
        await telethon_executor.start_listener(
            store=store,
            service=service,
            config=config,
            folder_name=folder_name,
            control_chat_ids=control_ids,
        )
        if not skip_backfill:
            # Backfill läuft als echter Background-Task — blockiert den Start nicht
            asyncio.create_task(
                telethon_executor.startup_backfill(store=store, config=config, folder_name=folder_name)
            )
        else:
            _log.info("Backfill übersprungen (--no-backfill)")
    register_command_menu = app.bot_data.get("register_command_menu")
    if callable(register_command_menu):
        await register_command_menu()
    await app.updater.start_polling()
    try:
        if run_timeout:
            await asyncio.sleep(run_timeout)
        else:
            while True:
                await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if telethon_executor is not None:
            await telethon_executor.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Scambaiter control bot.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
    parser.add_argument("--timeout", type=int, default=None,
                        help="Automatisch stoppen nach N Sekunden (für Tests).")
    parser.add_argument("--no-backfill", action="store_true",
                        help="Kein History-Backfill beim Start (für Tests, spart ~30s).")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

    asyncio.run(_run(run_timeout=args.timeout, skip_backfill=args.no_backfill))


if __name__ == "__main__":
    main()
