#!/usr/bin/env python3
"""
Back-to-back Auto-Send Probe — zwei Szenarien:

Szenario A: Auto-Send ist schon aktiv wenn eine neue Scammer-Nachricht ankommt.
  → Telethon-Listener triggert Auto-Send via live_message callback.

Szenario B: Unbeantwortete Scammer-Nachrichten existieren schon, dann wird
            Auto-Send eingeschaltet.
  → _start_auto_send_task läuft sofort an.

Voraussetzung: Control-Bot läuft (lokal oder auf Uberspace).

Beispiel:
  source secret.sh
  # Server starten (lokal, für Tests):
  python3 -m scripts.run_control_bot --timeout 180 --no-backfill &
  # Oder Uberspace nutzen (ssh strfry.org supervisorctl start scambaiter)

  PYTHONPATH=. python3 scripts/probe_autosend.py
  PYTHONPATH=. python3 scripts/probe_autosend.py --scenario a
  PYTHONPATH=. python3 scripts/probe_autosend.py --scenario b
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys
import time

from telegram import Bot
from telethon import TelegramClient
from telethon.tl.types import KeyboardButtonCallback

from scambaiter.config import load_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
                    print(f"    → [{btn.text}] ({data})")
                    await msg.click(data=btn.data)
                    return True
    return False


async def _autosend_state(client, bot_entity, sender_id: int, chat_id: int) -> bool | None:
    """Liest den aktuellen Auto-Send-Status vom Chat-Card-Button-Text."""
    async for msg in client.iter_messages(bot_entity, limit=10):
        if msg.sender_id != sender_id or not msg.reply_markup:
            continue
        for row in msg.reply_markup.rows:
            for btn in row.buttons:
                if not isinstance(btn, KeyboardButtonCallback):
                    continue
                data = btn.data.decode() if isinstance(btn.data, bytes) else btn.data
                if f"sc:autosend_toggle:{chat_id}" in data:
                    return "ON" in btn.text
    return None


async def _wait_for_new_bot_msg(client, bot_entity, sender_id: int, after_msg_id: int,
                                 pattern: str = "", timeout: int = 90) -> str | None:
    """Wartet bis eine neue Bot-Nachricht nach after_msg_id erscheint (optional: enthält pattern)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async for msg in client.iter_messages(bot_entity, limit=3):
            if msg.sender_id != sender_id:
                continue
            if msg.id <= after_msg_id:
                break
            text = msg.text or ""
            if not pattern or pattern in text:
                return text
        await asyncio.sleep(2)
    return None


async def _latest_group_msg_id(client, group_id: int, sender_id: int) -> int:
    """Neueste Nachricht von sender_id in der Gruppe, oder 0."""
    async for msg in client.iter_messages(group_id, limit=20):
        if msg.sender_id == sender_id:
            return msg.id
    return 0


async def _wait_for_group_reply(client, group_id: int, sender_id: int,
                                 after_id: int, timeout: int = 90) -> str | None:
    """Wartet auf eine neue Nachricht von sender_id in der Gruppe nach after_id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async for msg in client.iter_messages(group_id, limit=5):
            if msg.sender_id == sender_id and msg.id > after_id:
                return msg.text or "<non-text>"
        await asyncio.sleep(2)
    return None


# ---------------------------------------------------------------------------
# Setup: Auto-Send einschalten/ausschalten
# ---------------------------------------------------------------------------

async def _get_fresh_card(client, bot_entity, sender_id: int, chat_id: int,
                          after_id: int, timeout: int = 10):
    """Wartet auf eine frische Chat-Card nach after_id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async for msg in client.iter_messages(bot_entity, limit=3):
            if msg.sender_id != sender_id or not msg.reply_markup:
                continue
            if msg.id <= after_id:
                break
            for row in msg.reply_markup.rows:
                for btn in row.buttons:
                    if not isinstance(btn, KeyboardButtonCallback):
                        continue
                    data = btn.data.decode() if isinstance(btn.data, bytes) else btn.data
                    if f"sc:autosend_toggle:{chat_id}" in data:
                        return msg, "ON" in btn.text
        await asyncio.sleep(1)
    return None, None


async def _ensure_autosend(client, bot_entity, main_me, chat_id: int, target_state: bool) -> bool:
    """Setzt Auto-Send auf target_state. Gibt True zurück wenn erfolgreich."""
    # Letzten bekannten msg_id merken
    last_id = 0
    async for msg in client.iter_messages(bot_entity, limit=1):
        last_id = msg.id

    # Frische Chat-Card anfordern
    await client.send_message(bot_entity, f"/chat {chat_id}")

    card_msg, current = await _get_fresh_card(client, bot_entity, main_me.id, chat_id, after_id=last_id)
    if card_msg is None:
        print("  ⚠️  Keine frische Chat-Card erhalten")
        return False

    print(f"  Auto-Send aktuell: {'ON' if current else 'OFF'} (msg_id={card_msg.id})")
    if current == target_state:
        return True

    # Toggle auf der frischen Card klicken
    for row in card_msg.reply_markup.rows:
        for btn in row.buttons:
            if not isinstance(btn, KeyboardButtonCallback):
                continue
            data = btn.data.decode() if isinstance(btn.data, bytes) else btn.data
            if f"sc:autosend_toggle:{chat_id}" in data:
                print(f"    → [{btn.text}] ({data})")
                await card_msg.click(data=btn.data)
                # Hinweis: query.answer("Auto-Send aktiviert/deaktiviert") bestätigt den Toggle.
                # Die Chat-Card zeigt erst "ON" wenn der Loop seine erste Phase setzt —
                # das passiert nur wenn es eine offene Scammer-Nachricht gibt.
                # Wir vertrauen dem erfolgreichen click() ohne State-Verifikation.
                await asyncio.sleep(1)
                print(f"  Auto-Send Toggle gesendet (Ziel: {'ON' if target_state else 'OFF'})")
                return True

    print("  ⚠️  Auto-Send-Toggle-Button nicht gefunden")
    return False


# ---------------------------------------------------------------------------
# Szenario A: Auto-Send schon aktiv, neue Scammer-Nachricht kommt rein
# ---------------------------------------------------------------------------

async def scenario_a(client, debug_bot: Bot, bot_entity, main_me, config, chat_id: int) -> bool:
    print("\n══ Szenario A: Live-Event triggert Auto-Send ══")

    # 1. Auto-Send einschalten
    print("1. Auto-Send einschalten...")
    if not await _ensure_autosend(client, bot_entity, main_me, chat_id, True):
        return False
    await asyncio.sleep(1)

    # 2. Baseline: letzte Bot-Antwort in der Gruppe merken
    print("2. Baseline in Testgruppe merken...")
    telethon_user = await client.get_me()
    last_sent_id = await _latest_group_msg_id(client, chat_id, telethon_user.id)
    print(f"   Letzte eigene Nachricht in Gruppe: msg_id={last_sent_id}")

    # 3. Scammer-Nachricht via DebugAgentBot senden
    msg_text = "Szenario A: Hallo! Ich habe eine sehr interessante Investitionsmöglichkeit für dich."
    print(f"3. Scammer-Nachricht senden: {msg_text!r}")
    await debug_bot.send_message(chat_id, msg_text)
    t0 = time.monotonic()

    # 4. Warten auf Antwort in der Testgruppe
    print("4. Warte auf Auto-Send-Antwort in Testgruppe (max 90s)...")
    reply = await _wait_for_group_reply(client, chat_id, telethon_user.id, last_sent_id, timeout=180)
    elapsed = time.monotonic() - t0

    if reply:
        print(f"✅ Antwort nach {elapsed:.1f}s: {reply[:120]!r}")
        return True
    else:
        print(f"❌ Keine Antwort nach 180s")
        return False


# ---------------------------------------------------------------------------
# Szenario B: Unbeantwortete Scammer-Nachrichten, dann Auto-Send einschalten
# ---------------------------------------------------------------------------

async def scenario_b(client, debug_bot: Bot, bot_entity, main_me, config, chat_id: int) -> bool:
    print("\n══ Szenario B: Auto-Send nach vorhandenen Scammer-Nachrichten ══")

    # 1. Auto-Send ausschalten
    print("1. Auto-Send ausschalten...")
    if not await _ensure_autosend(client, bot_entity, main_me, chat_id, False):
        return False
    await asyncio.sleep(1)

    # 2. Scammer-Nachricht senden (unbeantwortet)
    # Hinweis: der Listener ruft bei eingehender Nachricht sofort trigger_for_chat auf,
    # was eine Generierung startet — unabhängig von Auto-Send. Damit Szenario B
    # (Auto-Send aktivieren nachdem Nachricht schon da) sauber testet, warten wir
    # kurz bis der Service-Trigger durch ist, bevor wir die Baseline nehmen.
    msg_text = "Szenario B: Warte mal kurz, ich erklär dir gleich alles."
    print(f"2. Unbeantwortete Scammer-Nachricht senden: {msg_text!r}")
    await debug_bot.send_message(chat_id, msg_text)
    # Live-Trigger generiert nur einen WAITING-Vorschlag, sendet nie selbst.
    # 5s reichen damit die Nachricht im DB-Ingest ist, bevor wir Auto-Send einschalten.
    await asyncio.sleep(5)

    # 3. Baseline
    telethon_user = await client.get_me()
    last_sent_id = await _latest_group_msg_id(client, chat_id, telethon_user.id)
    print(f"3. Baseline: letzte eigene Nachricht in Gruppe: msg_id={last_sent_id}")

    # 4. Auto-Send einschalten → Loop startet sofort
    print("4. Auto-Send einschalten (Loop soll sofort laufen)...")
    if not await _ensure_autosend(client, bot_entity, main_me, chat_id, True):
        return False
    t0 = time.monotonic()

    # 5. Warten auf Antwort
    print("5. Warte auf Auto-Send-Antwort in Testgruppe (max 90s)...")
    reply = await _wait_for_group_reply(client, chat_id, telethon_user.id, last_sent_id, timeout=180)
    elapsed = time.monotonic() - t0

    if reply:
        print(f"✅ Antwort nach {elapsed:.1f}s: {reply[:120]!r}")
        return True
    else:
        print(f"❌ Keine Antwort nach 180s")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(scenario: str, wait_for_server: bool) -> None:
    config = load_config()

    test_chat_id_str = os.environ.get("SCAMBAITER_TEST_CHAT_ID")
    debug_token = os.environ.get("SCAMBAITER_DEBUG_BOT_TOKEN")
    main_token = os.environ.get("SCAMBAITER_BOT_TOKEN")

    missing = [k for k, v in {
        "SCAMBAITER_TEST_CHAT_ID": test_chat_id_str,
        "SCAMBAITER_DEBUG_BOT_TOKEN": debug_token,
        "SCAMBAITER_BOT_TOKEN": main_token,
    }.items() if not v]
    if missing:
        print(f"❌ Fehlende Umgebungsvariablen: {', '.join(missing)}")
        sys.exit(1)

    chat_id = int(test_chat_id_str)

    async with Bot(token=main_token) as b:
        main_me = await b.get_me()

    debug_bot = Bot(token=debug_token)

    client = TelegramClient("probe_session", config.telethon_api_id, config.telethon_api_hash)
    await client.start()
    bot_entity = await client.get_entity(main_me.username)

    results: dict[str, bool] = {}

    if scenario in ("a", "both"):
        results["A"] = await scenario_a(client, debug_bot, bot_entity, main_me, config, chat_id)
        if scenario == "both":
            print("\n── Kurze Pause zwischen Szenarien ──")
            await asyncio.sleep(5)

    if scenario in ("b", "both"):
        results["B"] = await scenario_b(client, debug_bot, bot_entity, main_me, config, chat_id)

    await client.disconnect()
    await debug_bot.shutdown()

    print("\n══ Ergebnis ══")
    for k, v in results.items():
        icon = "✅" if v else "❌"
        print(f"  {icon} Szenario {k}: {'PASS' if v else 'FAIL'}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["a", "b", "both"], default="both",
                        help="Welches Szenario testen (default: both)")
    args = parser.parse_args()
    asyncio.run(main(args.scenario, wait_for_server=False))
