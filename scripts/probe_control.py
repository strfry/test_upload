#!/usr/bin/env python3
"""
Probe: Sendet Kommandos via Telethon (User-Account) in den Control-Chat
und klickt optional Inline-Buttons. Testet den Bot-Command-Flow direkt.

Beispiele:
  # Chat-Card öffnen und Dry-Run starten
  PYTHONPATH=. python3 scripts/probe_control.py --chat $SCAMBAITER_TEST_CHAT_ID --dryrun

  # Nur Chat-Card anzeigen
  PYTHONPATH=. python3 scripts/probe_control.py --chat $SCAMBAITER_TEST_CHAT_ID

  # Beliebiges Kommando senden
  PYTHONPATH=. python3 scripts/probe_control.py --cmd "/whoami"
  PYTHONPATH=. python3 scripts/probe_control.py --cmd "/chats"
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys

from telegram import Bot
from telethon import TelegramClient
from telethon.tl.types import KeyboardButtonCallback
from scambaiter.config import load_config


async def _click_button(client, entity, sender_id: int, data_pattern: str) -> bool:
    async for msg in client.iter_messages(entity, limit=5):
        if msg.sender_id != sender_id or not msg.reply_markup:
            continue
        for row in msg.reply_markup.rows:
            for btn in row.buttons:
                if not isinstance(btn, KeyboardButtonCallback):
                    continue
                data = btn.data.decode() if isinstance(btn.data, bytes) else btn.data
                if data_pattern in data:
                    print(f"  → Klicke: {btn.text!r} ({data})")
                    await msg.click(data=btn.data)
                    return True
    return False


async def main(chat_id: int | None, cmd: str | None, dryrun: bool, send: bool, wait: int) -> None:
    config = load_config()
    control_chat_id_str = os.environ.get("SCAMBAITER_CONTROL_CHAT_ID")
    group_chat_id_str = os.environ.get("SCAMBAITER_GROUP_CHAT_ID")
    debug_token = os.environ.get("SCAMBAITER_DEBUG_BOT_TOKEN")
    main_token = os.environ.get("SCAMBAITER_BOT_TOKEN")

    missing = [k for k, v in {
        "SCAMBAITER_CONTROL_CHAT_ID": control_chat_id_str,
        "SCAMBAITER_GROUP_CHAT_ID": group_chat_id_str,
        "SCAMBAITER_DEBUG_BOT_TOKEN": debug_token,
        "SCAMBAITER_BOT_TOKEN": main_token,
    }.items() if not v]
    if missing:
        print(f"❌ Fehlende Umgebungsvariablen: {', '.join(missing)}")
        sys.exit(1)

    control_chat_id = int(control_chat_id_str)

    async with Bot(token=main_token) as b:
        main_me = await b.get_me()

    # DebugAgentBot: Offset für neue Updates
    debug_bot = Bot(token=debug_token)
    updates = await debug_bot.get_updates(timeout=1, limit=1, offset=-1)
    offset = (updates[-1].update_id + 1) if updates else 0

    # Telethon starten
    client = TelegramClient("probe_session", config.telethon_api_id, config.telethon_api_hash)
    await client.start()

    # Kommando senden
    # Bot-Entity via Username (nicht control_chat_id — das ist die eigene User-ID)
    bot_entity = await client.get_entity(main_me.username)

    if cmd:
        print(f"💬 Sende Kommando: {cmd!r} → @{main_me.username}")
        await client.send_message(bot_entity, cmd)
        await asyncio.sleep(2)

    if chat_id:
        print(f"💬 Öffne Chat-Card für {chat_id}...")
        await client.send_message(bot_entity, f"/chat {chat_id}")
        await asyncio.sleep(3)

        if dryrun:
            # Schritt 1: Prompt-Button klicken
            print(f"🖱  Schritt 1: Klicke Prompt-Button...")
            clicked = await _click_button(client, bot_entity, main_me.id, f"sc:prompt:{chat_id}")
            if clicked:
                await asyncio.sleep(3)
                # Schritt 2: Dry-Run-Button im Prompt-Card klicken
                print(f"🖱  Schritt 2: Klicke Dry-Run-Button...")
                clicked2 = await _click_button(client, bot_entity, main_me.id, f"sc:dryrun:{chat_id}")
                if clicked2:
                    if send:
                        await asyncio.sleep(5)
                        print(f"🖱  Schritt 3: Klicke Send-Button...")
                        clicked3 = await _click_button(client, bot_entity, main_me.id, f"sc:reply_send:{chat_id}")
                        if not clicked3:
                            print("  ⚠️  Kein Send-Button auf der Result-Card gefunden.")
                else:
                    print("  ⚠️  Kein Dry-Run-Button im Prompt-Card gefunden.")
            else:
                print("  ⚠️  Kein Prompt-Button auf der Chat-Card gefunden.")

    print(f"⏳ Warte {wait}s auf Antwort von @{main_me.username}...")
    await asyncio.sleep(wait)

    # Antworten via Telethon lesen (persönlicher Chat + Gruppe)
    from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonCallback, KeyboardButtonRow
    responses = []
    for chat in [bot_entity]:
        async for msg in client.iter_messages(chat, limit=10):
            if msg.sender_id == main_me.id:
                text = msg.text or msg.caption or "<non-text>"
                buttons = []
                if msg.reply_markup and isinstance(msg.reply_markup, ReplyInlineMarkup):
                    for row in msg.reply_markup.rows:
                        row_labels = []
                        for btn in row.buttons:
                            data = ""
                            if isinstance(btn, KeyboardButtonCallback):
                                data = btn.data.decode() if isinstance(btn.data, bytes) else btn.data
                            row_labels.append(f"[{btn.text}]" + (f"→{data}" if data else ""))
                        buttons.append("  ".join(row_labels))
                responses.append((chat, msg.id, text, buttons))
            if len(responses) >= 5:
                break

    await client.disconnect()

    print(f"\n── Ergebnis ({len(responses)} Antworten von @{main_me.username}) ──")
    if responses:
        for _chat, msg_id, text, buttons in responses:
            preview = text[:400].replace("\n", "\n   ")
            print(f"  ✅ msg_id={msg_id}:\n   {preview}")
            if buttons:
                print("   Buttons:")
                for row in buttons:
                    print(f"     {row}")
            print()
    else:
        print("  ⚠️  Keine Antwort empfangen.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", type=int, default=None, help="Chat-ID für /chat <id>")
    parser.add_argument("--cmd", type=str, default=None, help="Kommando direkt senden")
    parser.add_argument("--dryrun", action="store_true", help="Dry-Run-Button klicken")
    parser.add_argument("--send", action="store_true", help="Nach Dry-Run auch Send klicken")
    parser.add_argument("--wait", type=int, default=15)
    args = parser.parse_args()

    if not args.chat and not args.cmd:
        parser.error("Entweder --chat oder --cmd angeben")

    asyncio.run(main(args.chat, args.cmd, args.dryrun, args.send, args.wait))
