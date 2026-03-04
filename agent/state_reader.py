"""
agent/state_reader.py — Read-only State-Snapshot aus SQLite

Liest den aktuellen Zustand eines Scambaiter-Chats aus der SQLite-Datenbank
und erzeugt einen kompakten, serialisierbaren Snapshot, der als LLM-Input
für den Orchestrierungs-Agenten dient.

Kein Write-Zugriff auf die DB. Kein Import aus service.py oder bot_api.py.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Typen
# ---------------------------------------------------------------------------

@dataclass
class PendingSuggestion:
    """Neueste gespeicherte Analyse / Vorschlag."""
    analysis_id: int
    message: str                    # Vorgeschlagene Antwort an den Scammer
    actions: list[str]              # z.B. ["wait_medium", "send"]
    loop_risk: str                  # "low" | "medium" | "high" (heuristisch)
    created_at: str


@dataclass
class RecentEvent:
    """Komprimiertes Event für den Snapshot."""
    role: str                       # scammer | scambaiter | system
    event_type: str
    text: str | None
    ts_utc: str


@dataclass
class ChatStateSnapshot:
    """Vollständiger State-Snapshot eines Chats zur Orchestrierungs-Entscheidung."""
    chat_id: int
    title: str

    # Zeitstempel letzter relevanter Nachrichten
    last_inbound_ts: str | None     # letzte Scammer-Nachricht
    last_outbound_ts: str | None    # letzte Scambaiter-Antwort
    minutes_since_inbound: float | None
    minutes_since_outbound: float | None

    # Kerninhalt
    pending_suggestion: PendingSuggestion | None
    active_directives: list[str]    # Texte der aktiven Direktiven
    recent_events: list[RecentEvent]

    # Signale
    loop_indicator: bool            # True = Wiederholungsmuster erkannt
    event_count: int                # Gesamtzahl Events im Chat

    # Optional: Memory-Summary
    memory_summary: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """Serialisiert als JSON-kompatibles Dict (für LLM-Prompt)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

class StateReader:
    """
    Read-only Zugriff auf die SQLite-Datenbank des Scambaiters.

    Öffnet eine eigene Verbindung (check_same_thread=False, read-only uri).
    Thread-safe für concurrent reads.
    """

    def __init__(self, db_path: str) -> None:
        if not Path(db_path).exists():
            raise FileNotFoundError(f"DB nicht gefunden: {db_path}")
        # file:...?mode=ro öffnet read-only und verhindert versehentliche Writes
        uri = f"file:{db_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Öffentliche API
    # ------------------------------------------------------------------

    def list_chat_ids(self) -> list[int]:
        """Alle Chat-IDs mit mindestens einem Event oder einer Analyse."""
        rows = self._conn.execute(
            """
            SELECT DISTINCT chat_id FROM (
                SELECT chat_id FROM events
                UNION
                SELECT chat_id FROM analyses
            ) ORDER BY chat_id
            """
        ).fetchall()
        return [int(r["chat_id"]) for r in rows]

    def get_snapshot(
        self,
        chat_id: int,
        recent_event_limit: int = 10,
    ) -> ChatStateSnapshot | None:
        """
        Erzeugt einen State-Snapshot für einen Chat.
        Gibt None zurück wenn kein Chat bekannt.
        """
        event_count = self._count_events(chat_id)
        if event_count == 0 and not self._has_analysis(chat_id):
            return None

        title = self._get_title(chat_id)
        recent_events = self._get_recent_events(chat_id, recent_event_limit)

        last_inbound_ts = self._last_ts_for_role(chat_id, "scammer")
        last_outbound_ts = self._last_ts_for_role(chat_id, "scambaiter")

        now = datetime.now(timezone.utc)
        minutes_since_inbound = _minutes_ago(last_inbound_ts, now)
        minutes_since_outbound = _minutes_ago(last_outbound_ts, now)

        pending = self._get_pending_suggestion(chat_id, recent_events)
        directives = self._get_active_directives(chat_id)
        loop_indicator = self._detect_loop(chat_id)
        memory_summary = self._get_memory_summary(chat_id)

        return ChatStateSnapshot(
            chat_id=chat_id,
            title=title,
            last_inbound_ts=last_inbound_ts,
            last_outbound_ts=last_outbound_ts,
            minutes_since_inbound=minutes_since_inbound,
            minutes_since_outbound=minutes_since_outbound,
            pending_suggestion=pending,
            active_directives=directives,
            recent_events=recent_events,
            loop_indicator=loop_indicator,
            event_count=event_count,
            memory_summary=memory_summary,
        )

    # ------------------------------------------------------------------
    # Interne Hilfsmethoden
    # ------------------------------------------------------------------

    def _count_events(self, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _has_analysis(self, chat_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM analyses WHERE chat_id = ? LIMIT 1", (chat_id,)
        ).fetchone()
        return row is not None

    def _get_title(self, chat_id: int) -> str:
        """Liest den Chat-Titel aus dem letzten gespeicherten Profil oder der letzten Analyse."""
        row = self._conn.execute(
            "SELECT snapshot_json FROM chat_profiles WHERE chat_id = ? LIMIT 1", (chat_id,)
        ).fetchone()
        if row:
            import json
            try:
                snap = json.loads(row["snapshot_json"])
                name = snap.get("name") or snap.get("first_name") or snap.get("username")
                if name:
                    return str(name)
            except Exception:
                pass

        row2 = self._conn.execute(
            "SELECT title FROM analyses WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (chat_id,)
        ).fetchone()
        return str(row2["title"]) if row2 else str(chat_id)

    def _last_ts_for_role(self, chat_id: int, role: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT ts_utc FROM events
            WHERE chat_id = ? AND role = ? AND ts_utc != ''
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (chat_id, role),
        ).fetchone()
        return str(row["ts_utc"]) if row else None

    def _get_recent_events(self, chat_id: int, limit: int) -> list[RecentEvent]:
        rows = self._conn.execute(
            """
            SELECT role, event_type, text, ts_utc
            FROM events
            WHERE chat_id = ?
              AND role IN ('scammer', 'scambaiter', 'system')
              AND event_type IN ('message', 'photo', 'sticker')
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        # Älteste zuerst
        return [
            RecentEvent(
                role=str(r["role"]),
                event_type=str(r["event_type"]),
                text=r["text"],
                ts_utc=str(r["ts_utc"]),
            )
            for r in reversed(rows)
        ]

    def _get_pending_suggestion(
        self,
        chat_id: int,
        recent_events: list[RecentEvent],
    ) -> PendingSuggestion | None:
        """
        Neueste gespeicherte Analyse = aktueller Vorschlag.
        loop_risk wird heuristisch aus den letzten Scambaiter-Nachrichten geschätzt.
        """
        import json
        row = self._conn.execute(
            """
            SELECT id, suggestion, actions_json, created_at
            FROM analyses
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None

        try:
            raw_actions = json.loads(row["actions_json"])
            if isinstance(raw_actions, list):
                # actions können Dicts {"type": "..."} oder Strings sein
                action_types = [
                    a.get("type", str(a)) if isinstance(a, dict) else str(a)
                    for a in raw_actions
                ]
            else:
                action_types = []
        except Exception:
            action_types = []

        loop_risk = self._estimate_loop_risk(chat_id, recent_events)

        return PendingSuggestion(
            analysis_id=int(row["id"]),
            message=str(row["suggestion"]),
            actions=action_types,
            loop_risk=loop_risk,
            created_at=str(row["created_at"]),
        )

    def _get_active_directives(self, chat_id: int) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT text FROM directives
            WHERE chat_id = ? AND active = 1
            ORDER BY id DESC
            LIMIT 20
            """,
            (chat_id,),
        ).fetchall()
        return [str(r["text"]) for r in rows]

    def _get_memory_summary(self, chat_id: int) -> dict[str, Any] | None:
        import json
        row = self._conn.execute(
            "SELECT summary_json FROM summaries WHERE chat_id = ? LIMIT 1", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            val = json.loads(row["summary_json"])
            return val if isinstance(val, dict) else None
        except Exception:
            return None

    def _detect_loop(self, chat_id: int, window: int = 6, threshold: float = 0.6) -> bool:
        """
        Einfache Loop-Heuristik: Prüft ob die letzten N Scambaiter-Nachrichten
        zu ähnlich sind (Wortüberschneidung > threshold).

        Gibt True zurück wenn ein Wiederholungsmuster erkannt wird.
        """
        rows = self._conn.execute(
            """
            SELECT text FROM events
            WHERE chat_id = ? AND role = 'scambaiter'
              AND event_type = 'message' AND text IS NOT NULL AND text != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, window),
        ).fetchall()

        texts = [str(r["text"]).lower().split() for r in rows if r["text"]]
        if len(texts) < 3:
            return False

        # Paarweise Jaccard-Ähnlichkeit der letzten Nachrichten
        matches = 0
        pairs = 0
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, b = set(texts[i]), set(texts[j])
                if not a or not b:
                    continue
                jaccard = len(a & b) / len(a | b)
                if jaccard >= threshold:
                    matches += 1
                pairs += 1

        if pairs == 0:
            return False
        return (matches / pairs) >= 0.5

    def _estimate_loop_risk(self, chat_id: int, recent_events: list[RecentEvent]) -> str:
        """
        Schätzt Loop-Risiko für den Vorschlag basierend auf den letzten Events.
        Gibt "low", "medium" oder "high" zurück.
        """
        if self._detect_loop(chat_id):
            return "high"

        # Mittleres Risiko wenn scammer schon länger nicht geantwortet hat
        # (Vorschlag könnte veraltet sein)
        scammer_events = [e for e in recent_events if e.role == "scammer"]
        baiter_events = [e for e in recent_events if e.role == "scambaiter"]

        if not scammer_events:
            return "medium"

        # Wenn Scambaiter mehr als doppelt so viele Nachrichten wie Scammer → medium
        if len(baiter_events) >= len(scammer_events) * 2:
            return "medium"

        return "low"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _minutes_ago(ts_str: str | None, now: datetime) -> float | None:
    """Berechnet Minuten seit einem ISO-UTC-Zeitstempel. None wenn kein TS."""
    if not ts_str:
        return None
    try:
        # Normalisiere: 2026-03-04T17:00:00Z → aware datetime
        ts = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        return round(delta.total_seconds() / 60, 1)
    except Exception:
        return None
