#!/usr/bin/env python3
"""
Startet den DebugAgentBot.

Läuft parallel zum ScamBaiterControl-Server — kein Token-Konflikt.
Der Server muss NICHT gestoppt werden.

Umgebungsvariablen (aus secret.sh):
  SCAMBAITER_DEBUG_BOT_TOKEN  – Pflicht
  SCAMBAITER_GROUP_CHAT_ID    – Pflicht (negative Gruppen-ID)
  SCAMBAITER_ANALYSIS_DB_PATH – Optional (default: scambaiter.sqlite3)
  SCAMBAITER_POLL_INTERVAL    – Optional (default: 15s)

Usage:
  source secret.sh
  python3 scripts/run_debug_agent.py
  python3 scripts/run_debug_agent.py --poll-interval 30
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="DebugAgentBot starten.")
    parser.add_argument("--poll-interval", type=int, default=None,
                        help="Sekunden zwischen DB-Polls (default: SCAMBAITER_POLL_INTERVAL oder 15)")
    parser.add_argument("--timeout", type=int, default=None,
                        help="Automatisch stoppen nach N Sekunden (default: läuft bis STRG+C)")
    parser.add_argument("--debug", action="store_true", help="Debug-Logging aktivieren.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    token = os.getenv("SCAMBAITER_DEBUG_BOT_TOKEN")
    group_chat_id_str = os.getenv("SCAMBAITER_GROUP_CHAT_ID")
    db_path = os.getenv("SCAMBAITER_ANALYSIS_DB_PATH", "scambaiter.sqlite3")

    missing = []
    if not token:
        missing.append("SCAMBAITER_DEBUG_BOT_TOKEN")
    if not group_chat_id_str:
        missing.append("SCAMBAITER_GROUP_CHAT_ID")
    if missing:
        print(f"❌ Fehlende Umgebungsvariablen: {', '.join(missing)}")
        sys.exit(1)

    group_chat_id = int(group_chat_id_str)
    poll_interval = args.poll_interval or int(os.getenv("SCAMBAITER_POLL_INTERVAL", "15"))

    print(f"🤖 DebugAgentBot startet")
    print(f"   Gruppe:        {group_chat_id}")
    print(f"   DB:            {db_path}")
    print(f"   Poll-Intervall: {poll_interval}s")
    print(f"   (Server muss nicht gestoppt werden)")
    print()

    # Import erst hier, damit Fehler sauber angezeigt werden
    from agent.debug_bot import DebugAgentBot

    bot = DebugAgentBot(
        token=token,
        group_chat_id=group_chat_id,
        db_path=db_path,
        poll_interval=poll_interval,
    )

    try:
        asyncio.run(bot.run(timeout=args.timeout))
    except KeyboardInterrupt:
        print("\n⏹ Gestoppt.")


if __name__ == "__main__":
    main()
