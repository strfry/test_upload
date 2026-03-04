#!/usr/bin/env python3
"""
Probe: Simuliert eine Scammer-Nachricht via DebugAgentBot in die Test-Scammer-Gruppe.
Telethon (User-Account) ingested sie als scammer-Event → Control Bot reagiert.
DebugAgentBot lauscht auf die Antwort in der Control-Gruppe.

Usage:
  source secret.sh
  PYTHONPATH=. python3 scripts/probe_scammer.py "Hallo, ich bin Lisa."
  PYTHONPATH=. python3 scripts/probe_scammer.py --wait 20 "Ich suche nach Investment-Möglichkeiten"
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sys

from telegram import Bot


async def main(messages: list[str], wait: int) -> None:
    test_chat_id_str = os.environ.get("SCAMBAITER_TEST_CHAT_ID")
    group_chat_id_str = os.environ.get("SCAMBAITER_GROUP_CHAT_ID")
    debug_token = os.environ.get("SCAMBAITER_DEBUG_BOT_TOKEN")
    main_token = os.environ.get("SCAMBAITER_BOT_TOKEN")

    missing = [k for k, v in {
        "SCAMBAITER_TEST_CHAT_ID": test_chat_id_str,
        "SCAMBAITER_GROUP_CHAT_ID": group_chat_id_str,
        "SCAMBAITER_DEBUG_BOT_TOKEN": debug_token,
        "SCAMBAITER_BOT_TOKEN": main_token,
    }.items() if not v]
    if missing:
        print(f"❌ Fehlende Umgebungsvariablen: {', '.join(missing)}")
        sys.exit(1)

    test_chat_id = int(test_chat_id_str)
    group_chat_id = int(group_chat_id_str)

    async with Bot(token=main_token) as b:
        main_me = await b.get_me()

    debug_bot = Bot(token=debug_token)
    updates = await debug_bot.get_updates(timeout=1, limit=1, offset=-1)
    offset = (updates[-1].update_id + 1) if updates else 0

    print(f"📨 Sende {len(messages)} Nachricht(en) in Test-Chat {test_chat_id}...")
    for msg in messages:
        sent = await debug_bot.send_message(chat_id=test_chat_id, text=msg)
        print(f"  → {msg!r} (msg_id={sent.message_id})")
        await asyncio.sleep(1)

    print(f"⏳ Warte {wait}s auf Antwort von @{main_me.username} in Control-Gruppe...")
    await asyncio.sleep(wait)

    all_updates, batch_offset = [], offset
    while True:
        batch = await debug_bot.get_updates(timeout=2, limit=100, offset=batch_offset)
        if not batch:
            break
        all_updates.extend(batch)
        batch_offset = batch[-1].update_id + 1

    responses = [
        upd.effective_message for upd in all_updates
        if upd.effective_message
        and upd.effective_message.from_user
        and upd.effective_message.from_user.id == main_me.id
    ]

    print(f"\n── Ergebnis ({len(all_updates)} Updates, {len(responses)} Control-Bot-Antworten) ──")
    if responses:
        for r in responses:
            preview = (r.text or "<non-text>")[:400].replace("\n", "\n   ")
            print(f"  ✅ msg_id={r.message_id}:\n   {preview}\n")
    else:
        print("  ⚠️  Keine Antwort empfangen.")
        print("     → Ist Test-Chat im 'Scammers'-Folder? Läuft der Server?")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("messages", nargs="+")
    parser.add_argument("--wait", type=int, default=15)
    args = parser.parse_args()
    asyncio.run(main(args.messages, args.wait))
