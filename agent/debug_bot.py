"""
agent/debug_bot.py — DebugAgentBot

Läuft parallel zum ScamBaiterControl-Bot in der "ScamBaiter Control Crew"-Gruppe.
Beobachtet die SQLite-DB auf neue Scammer-Events und postet kompakte State-Cards
in die Gruppe (MANUAL-Modus: nur Beobachtung, keine eigenen Aktionen).

Kein Token-Konflikt mit dem Hauptbot, da eigener Token.
Kein Schreib-Zugriff auf die DB.

Umgebungsvariablen:
  SCAMBAITER_DEBUG_BOT_TOKEN  – Token des DebugAgentBot
  SCAMBAITER_GROUP_CHAT_ID    – Chat-ID der gemeinsamen Gruppe (negativ)
  SCAMBAITER_ANALYSIS_DB_PATH – Pfad zur SQLite-DB (default: scambaiter.sqlite3)
  SCAMBAITER_POLL_INTERVAL    – Sekunden zwischen DB-Polls (default: 15)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timezone

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application

from agent.state_reader import StateReader, ChatStateSnapshot

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State-Card Formatter
# ---------------------------------------------------------------------------

def format_state_card(snap: ChatStateSnapshot, event_text: str | None = None) -> str:
    """Formatiert einen State-Snapshot als Telegram-HTML-Card."""
    lines = []

    # Header
    lines.append(f"🔍 <b>ScamBaiter Debug — {_esc(snap.title)}</b>")
    lines.append(f"<code>chat_id: {snap.chat_id}</code>")
    lines.append("")

    # Neues Event (falls vorhanden)
    if event_text:
        lines.append(f"📨 <b>Neues Scammer-Event:</b>")
        lines.append(f"<i>{_esc(event_text[:200])}</i>")
        lines.append("")

    # Zeitstempel
    if snap.minutes_since_inbound is not None:
        lines.append(f"⏱ Letzte Scammer-Nachricht: <b>vor {snap.minutes_since_inbound:.0f} min</b>")
    if snap.minutes_since_outbound is not None:
        lines.append(f"⏱ Letzte Antwort gesendet:  <b>vor {snap.minutes_since_outbound:.0f} min</b>")

    # Loop-Warnung
    if snap.loop_indicator:
        lines.append("")
        lines.append("⚠️ <b>Loop-Indikator aktiv</b> — Wiederholungsmuster erkannt")

    # Vorschlag
    if snap.pending_suggestion:
        ps = snap.pending_suggestion
        risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(ps.loop_risk, "⚪")
        lines.append("")
        lines.append(f"💬 <b>Letzter Vorschlag</b> (analysis #{ps.analysis_id}):")
        lines.append(f"<i>{_esc(ps.message[:300])}</i>")
        lines.append(f"Actions: {', '.join(ps.actions) or '–'}  |  Loop-Risiko: {risk_emoji} {ps.loop_risk}")
    else:
        lines.append("")
        lines.append("💬 Kein Vorschlag vorhanden — run_prompt empfohlen")

    # Direktiven
    if snap.active_directives:
        lines.append("")
        lines.append(f"📋 <b>Aktive Direktiven ({len(snap.active_directives)}):</b>")
        for d in snap.active_directives[:3]:
            lines.append(f"  • {_esc(d[:80])}")
        if len(snap.active_directives) > 3:
            lines.append(f"  … (+{len(snap.active_directives) - 3} weitere)")

    lines.append("")
    lines.append(f"<i>Events gesamt: {snap.event_count}</i>")

    return "\n".join(lines)


def _esc(text: str) -> str:
    """Minimales HTML-Escaping für Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Watcher: SQLite-Polling auf neue Scammer-Events
# ---------------------------------------------------------------------------

class DbWatcher:
    """
    Pollt die SQLite-DB auf neue Events mit höherer ID als zuletzt gesehen.
    Thread-safe, rein lesend.
    """

    def __init__(self, db_path: str, exclude_chat_ids: set[int] | None = None) -> None:
        self._db_path = db_path
        self._exclude: set[int] = exclude_chat_ids or set()
        # Letzter gesehener Event-ID pro chat_id
        self._last_event_id: dict[int, int] = {}
        self._initialized = False

    def _get_reader(self) -> StateReader:
        return StateReader(self._db_path)

    def initialize(self) -> None:
        """Beim Start: aktuelle Max-Event-IDs einlesen, keine Cards posten."""
        with self._get_reader() as r:
            chat_ids = [c for c in r.list_chat_ids() if c not in self._exclude]
            for cid in chat_ids:
                max_id = self._max_event_id(cid)
                self._last_event_id[cid] = max_id
        self._initialized = True
        _log.info("DbWatcher initialisiert: %d Chats bekannt, watching ab jetzt", len(chat_ids))

    def poll(self) -> list[tuple[ChatStateSnapshot, str | None]]:
        """
        Prüft auf neue Scammer-Events seit letztem Poll.
        Gibt Liste von (Snapshot, neuer Event-Text) zurück.
        """
        if not self._initialized:
            self.initialize()
            return []

        results = []
        with self._get_reader() as r:
            chat_ids = [c for c in r.list_chat_ids() if c not in self._exclude]
            for cid in chat_ids:
                new_events = self._fetch_new_scammer_events(cid)
                if not new_events:
                    continue
                # Max-ID aktualisieren
                max_new_id = max(e["id"] for e in new_events)
                self._last_event_id[cid] = max_new_id

                snap = r.get_snapshot(cid)
                if snap is None:
                    continue

                # Letzten neuen Event-Text für die Card
                latest_text = None
                for ev in reversed(new_events):
                    if ev.get("text"):
                        latest_text = ev["text"]
                        break

                _log.info(
                    "Neues Scammer-Event in Chat %s (%s): %d neue Events",
                    cid, snap.title, len(new_events),
                )
                results.append((snap, latest_text))

        return results

    def _max_event_id(self, chat_id: int) -> int:
        import sqlite3
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE chat_id = ? AND role = 'scammer'",
                (chat_id,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def _fetch_new_scammer_events(self, chat_id: int) -> list[dict]:
        import sqlite3
        last_id = self._last_event_id.get(chat_id, 0)
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, text, ts_utc FROM events
                WHERE chat_id = ? AND role = 'scammer' AND id > ?
                ORDER BY id ASC
                """,
                (chat_id, last_id),
            ).fetchall()
            return [{"id": r["id"], "text": r["text"], "ts_utc": r["ts_utc"]} for r in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class DebugAgentBot:
    def __init__(self, token: str, group_chat_id: int, db_path: str, poll_interval: int = 15) -> None:
        self._token = token
        self._group_chat_id = group_chat_id
        self._poll_interval = poll_interval
        self._watcher = DbWatcher(db_path, exclude_chat_ids={group_chat_id})
        self._app = Application.builder().token(token).build()
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        # DebugAgentBot registriert bewusst keine /chats oder /status Handler —
        # diese Kommandos gehören ausschließlich ScamBaiterControl.
        pass

    async def _post_to_group(self, text: str) -> None:
        """Postet eine Nachricht in die Gruppe. Fehler werden geloggt, nicht geworfen."""
        try:
            await self._app.bot.send_message(
                chat_id=self._group_chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as exc:
            _log.warning("Konnte nicht in Gruppe posten: %s", exc)

    async def _watch_loop(self) -> None:
        """Polling-Loop: prüft DB alle poll_interval Sekunden auf neue Events."""
        _log.info("DbWatcher startet (Intervall: %ds)", self._poll_interval)
        self._watcher.initialize()
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                updates = self._watcher.poll()
                for snap, event_text in updates:
                    card = format_state_card(snap, event_text)
                    await self._post_to_group(card)
            except Exception as exc:
                _log.exception("Fehler im Watch-Loop: %s", exc)

    async def run(self, timeout: int | None = None) -> None:
        """Startet Bot + Watch-Loop. Stoppt nach timeout Sekunden (None = unendlich)."""
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(allowed_updates=["message", "callback_query"])

        _log.info("DebugAgentBot gestartet. Gruppe: %s", self._group_chat_id)
        timeout_info = f" (Timeout: {timeout}s)" if timeout else ""
        await self._post_to_group(
            f"🤖 <b>DebugAgentBot online</b>{timeout_info}\n"
            "Beobachte ScamBaiter-Aktivitäten (MANUAL-Modus).\n"
            "/status für Überblick · /chats für Chat-Liste"
        )

        watch_task = asyncio.create_task(self._watch_loop())
        try:
            if timeout:
                await asyncio.wait_for(asyncio.shield(watch_task), timeout=timeout)
            else:
                await watch_task
        except (asyncio.TimeoutError, KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task
            _log.info("Fahre DebugAgentBot herunter...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
