from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import threading


ALLOWED_EVENT_TYPES = {"message", "photo", "forward", "sticker", "typing_interval"}
ALLOWED_ROLES = {"manual", "scammer", "scambaiter", "system"}


@dataclass(slots=True)
class StoredAnalysis:
    id: int
    chat_id: int
    title: str
    suggestion: str
    analysis: dict[str, Any]
    actions: list[dict[str, Any]]
    metadata: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class Directive:
    id: int
    chat_id: int
    text: str
    scope: str
    active: bool
    created_at: str


@dataclass(slots=True)
class EventRecord:
    id: int
    chat_id: int
    event_type: str
    role: str
    text: str | None
    ts_utc: str | None
    source_message_id: str | None
    meta: dict[str, Any]


@dataclass(slots=True)
class ChatProfile:
    chat_id: int
    snapshot: dict[str, Any]
    last_source: str
    last_updated_at: str


@dataclass(slots=True)
class ProfileChange:
    id: int
    chat_id: int
    field_path: str
    old_value: Any
    new_value: Any
    source: str
    changed_at: str


@dataclass(slots=True)
class GenerationAttempt:
    id: int
    chat_id: int
    provider: str
    model: str
    prompt_json: dict[str, Any]
    response_json: dict[str, Any]
    result_text: str
    status: str
    error_message: str | None
    attempt_no: int
    phase: str
    accepted: bool
    reject_reason: str | None
    created_at: str


@dataclass(slots=True)
class MemorySummary:
    chat_id: int
    summary: dict[str, Any]
    cursor_event_id: int
    model: str
    last_updated_at: str


class _LockingConnection:
    def __init__(self, raw_conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = raw_conn
        self._lock = lock

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class AnalysisStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        raw_conn = sqlite3.connect(db_path, check_same_thread=False)
        raw_conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn = _LockingConnection(raw_conn, self._lock)
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                actions_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS directives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                scope TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT,
                ts_utc TEXT,
                source_message_id TEXT,
                meta_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_chat_source
            ON events(chat_id, source_message_id)
            WHERE source_message_id IS NOT NULL
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_profiles (
                chat_id INTEGER PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                last_source TEXT NOT NULL,
                last_updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                field_path TEXT NOT NULL,
                old_value_json TEXT NOT NULL,
                new_value_json TEXT NOT NULL,
                source TEXT NOT NULL,
                changed_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS generation_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                result_text TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                phase TEXT NOT NULL DEFAULT 'initial',
                accepted INTEGER NOT NULL DEFAULT 0,
                reject_reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Migration: rename memory_contexts → summaries if the old table still exists.
        existing_tables = {
            row[0] for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "memory_contexts" in existing_tables and "summaries" not in existing_tables:
            self._conn.execute("ALTER TABLE memory_contexts RENAME TO summaries")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summaries (
                chat_id INTEGER PRIMARY KEY,
                summary_json TEXT NOT NULL,
                cursor_event_id INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL,
                last_updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                chat_id  INTEGER NOT NULL,
                key      TEXT    NOT NULL,
                value    TEXT    NOT NULL,
                updated_at TEXT  NOT NULL,
                PRIMARY KEY (chat_id, key)
            )
            """
        )
        self._ensure_analyses_columns()
        self._ensure_generation_attempt_columns()
        # Legacy cleanup: older builds embedded source labels in profile_update text.
        # Normalize persisted rows so views stay clean without data loss.
        self._conn.execute(
            """
            UPDATE events
            SET text = TRIM(REPLACE(text, ' (botapi_forward)', ''))
            WHERE role = 'system'
              AND event_type = 'message'
              AND text LIKE 'profile_update:%(botapi_forward)%'
            """
        )
        self._conn.commit()

    def save(
        self,
        chat_id: int,
        title: str,
        suggestion: str,
        analysis: dict[str, Any] | None,
        actions: list[dict[str, Any]] | None,
        metadata: dict[str, Any] | None,
    ) -> int:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        cur = self._conn.execute(
            """
            INSERT INTO analyses(chat_id, title, suggestion, analysis_json, actions_json, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                title,
                suggestion,
                json.dumps(analysis or {}, ensure_ascii=True),
                json.dumps(actions or [], ensure_ascii=True),
                json.dumps(metadata or {}, ensure_ascii=True),
                ts_utc,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def latest_for_chat(self, chat_id: int) -> StoredAnalysis | None:
        row = self._conn.execute(
            """
            SELECT id, chat_id, title, suggestion, analysis_json, actions_json, metadata_json, created_at
            FROM analyses
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredAnalysis(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            title=str(row["title"]),
            suggestion=str(row["suggestion"]),
            analysis=self._loads_dict(row["analysis_json"]),
            actions=self._loads_list(row["actions_json"]),
            metadata=self._loads_dict(row["metadata_json"]),
            created_at=str(row["created_at"]),
        )

    def add_directive(self, chat_id: int, text: str, scope: str = "chat") -> Directive:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        cur = self._conn.execute(
            """
            INSERT INTO directives(chat_id, text, scope, active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (chat_id, text, scope, ts_utc),
        )
        self._conn.commit()
        return Directive(
            id=int(cur.lastrowid),
            chat_id=chat_id,
            text=text,
            scope=scope,
            active=True,
            created_at=ts_utc,
        )

    def list_directives(self, chat_id: int, active_only: bool = True, limit: int = 50) -> list[Directive]:
        if active_only:
            rows = self._conn.execute(
                """
                SELECT id, chat_id, text, scope, active, created_at
                FROM directives
                WHERE chat_id = ? AND active = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, chat_id, text, scope, active, created_at
                FROM directives
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [
            Directive(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                text=str(row["text"]),
                scope=str(row["scope"]),
                active=bool(row["active"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def deactivate_directive(self, directive_id: int) -> None:
        self._conn.execute("UPDATE directives SET active = 0 WHERE id = ?", (directive_id,))
        self._conn.commit()

    def ingest_event(
        self,
        chat_id: int,
        event_type: str,
        role: str,
        text: str | None = None,
        ts_utc: str | None = None,
        source_message_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> EventRecord:
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {event_type}")
        if role not in ALLOWED_ROLES:
            raise ValueError(f"unsupported role: {role}")
        timestamp = ts_utc if ts_utc is not None else ""
        payload = json.dumps(meta or {}, ensure_ascii=True)
        cur = self._conn.execute(
            """
            INSERT INTO events(chat_id, event_type, role, text, ts_utc, source_message_id, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, event_type, role, text, timestamp, source_message_id, payload),
        )
        self._conn.commit()
        return EventRecord(
            id=int(cur.lastrowid),
            chat_id=chat_id,
            event_type=event_type,
            role=role,
            text=text,
            ts_utc=timestamp,
            source_message_id=source_message_id,
            meta=meta or {},
        )

    def ingest_user_forward(
        self,
        chat_id: int,
        event_type: str,
        text: str | None,
        source_message_id: str,
        role: str = "manual",
        ts_utc: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> EventRecord:
        row = self._conn.execute(
            "SELECT id, chat_id, event_type, role, text, ts_utc, source_message_id, meta_json FROM events WHERE chat_id = ? AND source_message_id = ?",
            (chat_id, source_message_id),
        ).fetchone()
        if row:
            return EventRecord(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                event_type=str(row["event_type"]),
                role=str(row["role"]),
                text=row["text"],
                ts_utc=row["ts_utc"],
                source_message_id=row["source_message_id"],
                meta=self._loads_dict(row["meta_json"]),
            )
        # Forwarded events keep their original event_type.
        return self.ingest_event(
            chat_id=chat_id,
            event_type=event_type,
            role=role,
            text=text,
            ts_utc=ts_utc,
            source_message_id=source_message_id,
            meta=meta,
        )

    def list_events(self, chat_id: int, limit: int = 500) -> list[EventRecord]:
        rows = self._conn.execute(
            """
            SELECT id, chat_id, event_type, role, text, ts_utc, source_message_id, meta_json
            FROM events
            WHERE chat_id = ?
            ORDER BY ts_utc ASC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [
            EventRecord(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                event_type=str(row["event_type"]),
                role=str(row["role"]),
                text=row["text"],
                ts_utc=row["ts_utc"],
                source_message_id=row["source_message_id"],
                meta=self._loads_dict(row["meta_json"]),
            )
            for row in rows
        ]

    def count_events(self, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def repair_timestamps_from_meta(self, chat_id: int | None = None) -> int:
        """Fill ts_utc = '' from meta_json forward_profile.origin_date_utc where available."""
        if chat_id is not None:
            rows = self._conn.execute(
                "SELECT id, meta_json FROM events WHERE chat_id = ? AND ts_utc = ''",
                (chat_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, meta_json FROM events WHERE ts_utc = ''"
            ).fetchall()
        updated = 0
        for row in rows:
            try:
                meta = json.loads(row["meta_json"])
                fp = meta.get("forward_profile") if isinstance(meta, dict) else None
                ts = fp.get("origin_date_utc") if isinstance(fp, dict) else None
                if not isinstance(ts, str) or not ts:
                    continue
                self._conn.execute(
                    "UPDATE events SET ts_utc = ? WHERE id = ?", (ts, row["id"])
                )
                updated += 1
            except Exception:
                continue
        if updated:
            self._conn.commit()
        return updated

    def clear_chat_history(self, chat_id: int) -> int:
        cur = self._conn.execute("DELETE FROM events WHERE chat_id = ?", (chat_id,))
        self._conn.commit()
        deleted = cur.rowcount
        if deleted is None or deleted < 0:
            return 0
        return int(deleted)

    def delete_events_by_ids(self, event_ids: list[int]) -> int:
        """Delete specific events by row id. Returns count of deleted rows."""
        if not event_ids:
            return 0
        placeholders = ",".join("?" * len(event_ids))
        cur = self._conn.execute(
            f"DELETE FROM events WHERE id IN ({placeholders})",
            event_ids,
        )
        self._conn.commit()
        return int(cur.rowcount) if cur.rowcount and cur.rowcount > 0 else 0

    def move_events_to_chat(self, event_ids: list[int], new_chat_id: int) -> int:
        """Reassign events to a different chat_id. Returns count of updated rows."""
        if not event_ids:
            return 0
        placeholders = ",".join("?" * len(event_ids))
        cur = self._conn.execute(
            f"UPDATE events SET chat_id = ? WHERE id IN ({placeholders})",
            [new_chat_id, *event_ids],
        )
        self._conn.commit()
        return int(cur.rowcount) if cur.rowcount and cur.rowcount > 0 else 0

    def clear_chat_context(self, chat_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        table_map = {
            "events": "events",
            "analyses": "analyses",
            "directives": "directives",
            "generation_attempts": "generation_attempts",
            "profile_changes": "profile_change_log",
            "chat_profile": "chat_profiles",
            "summary": "summaries",
            "memory": "memory",
        }
        for label, table in table_map.items():
            cur = self._conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
            rowcount = cur.rowcount
            counts[label] = int(rowcount) if isinstance(rowcount, int) and rowcount > 0 else 0
        self._conn.commit()
        counts["total"] = sum(counts.values())
        return counts

    def list_chat_ids(self, limit: int = 100) -> list[int]:
        rows = self._conn.execute(
            """
            SELECT chat_id
            FROM (
                SELECT DISTINCT chat_id AS chat_id FROM events
                UNION
                SELECT DISTINCT chat_id AS chat_id FROM analyses
                UNION
                SELECT DISTINCT chat_id AS chat_id FROM chat_profiles
                UNION
                SELECT DISTINCT chat_id AS chat_id FROM summaries
            )
            ORDER BY chat_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def get_summary(self, chat_id: int) -> MemorySummary | None:
        row = self._conn.execute(
            """
            SELECT chat_id, summary_json, cursor_event_id, model, last_updated_at
            FROM summaries
            WHERE chat_id = ?
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return MemorySummary(
            chat_id=int(row["chat_id"]),
            summary=self._loads_dict(row["summary_json"]),
            cursor_event_id=int(row["cursor_event_id"]),
            model=str(row["model"]),
            last_updated_at=str(row["last_updated_at"]),
        )

    def upsert_summary(
        self,
        chat_id: int,
        summary: dict[str, Any],
        cursor_event_id: int,
        model: str,
        last_updated_at: str | None = None,
    ) -> MemorySummary:
        ts = last_updated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._conn.execute(
            """
            INSERT INTO summaries(chat_id, summary_json, cursor_event_id, model, last_updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                summary_json = excluded.summary_json,
                cursor_event_id = excluded.cursor_event_id,
                model = excluded.model,
                last_updated_at = excluded.last_updated_at
            """,
            (chat_id, json.dumps(summary, ensure_ascii=True), int(cursor_event_id), model, ts),
        )
        self._conn.commit()
        return MemorySummary(
            chat_id=chat_id,
            summary=summary,
            cursor_event_id=int(cursor_event_id),
            model=model,
            last_updated_at=ts,
        )

    def clear_summary(self, chat_id: int) -> int:
        cur = self._conn.execute("DELETE FROM summaries WHERE chat_id = ?", (chat_id,))
        self._conn.commit()
        rowcount = cur.rowcount
        return int(rowcount) if isinstance(rowcount, int) and rowcount > 0 else 0

    def get_memory_kv(self, chat_id: int) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM memory WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_memory_kv(self, chat_id: int, key: str, value: str) -> None:
        ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._conn.execute(
            """
            INSERT OR REPLACE INTO memory(chat_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, key, value, ts),
        )
        self._conn.commit()

    def get_chat_profile(self, chat_id: int) -> ChatProfile | None:
        row = self._conn.execute(
            """
            SELECT chat_id, snapshot_json, last_source, last_updated_at
            FROM chat_profiles
            WHERE chat_id = ?
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return ChatProfile(
            chat_id=int(row["chat_id"]),
            snapshot=self._loads_dict(row["snapshot_json"]),
            last_source=str(row["last_source"]),
            last_updated_at=str(row["last_updated_at"]),
        )

    def upsert_chat_profile(
        self,
        chat_id: int,
        patch: dict[str, Any],
        source: str,
        changed_at: str | None = None,
    ) -> list[ProfileChange]:
        existing = self.get_chat_profile(chat_id)
        base = existing.snapshot if existing else {}
        merged = self._deep_merge_dicts(base, patch)
        old_flat = self._flatten_dict(base)
        new_flat = self._flatten_dict(merged)
        keys = set(old_flat.keys()) | set(new_flat.keys())
        ts = changed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        changes: list[ProfileChange] = []
        for key in sorted(keys):
            old_value = old_flat.get(key)
            new_value = new_flat.get(key)
            if old_value == new_value:
                continue
            cur = self._conn.execute(
                """
                INSERT INTO profile_change_log(chat_id, field_path, old_value_json, new_value_json, source, changed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    key,
                    json.dumps(old_value, ensure_ascii=True),
                    json.dumps(new_value, ensure_ascii=True),
                    source,
                    ts,
                ),
            )
            changes.append(
                ProfileChange(
                    id=int(cur.lastrowid),
                    chat_id=chat_id,
                    field_path=key,
                    old_value=old_value,
                    new_value=new_value,
                    source=source,
                    changed_at=ts,
                )
            )
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO chat_profiles(chat_id, snapshot_json, last_source, last_updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, json.dumps(merged, ensure_ascii=True), source, ts),
            )
        else:
            self._conn.execute(
                """
                UPDATE chat_profiles
                SET snapshot_json = ?, last_source = ?, last_updated_at = ?
                WHERE chat_id = ?
                """,
                (json.dumps(merged, ensure_ascii=True), source, ts, chat_id),
            )
        self._conn.commit()
        return changes

    def list_profile_changes(self, chat_id: int, limit: int = 50) -> list[ProfileChange]:
        rows = self._conn.execute(
            """
            SELECT id, chat_id, field_path, old_value_json, new_value_json, source, changed_at
            FROM profile_change_log
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [
            ProfileChange(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                field_path=str(row["field_path"]),
                old_value=json.loads(row["old_value_json"]),
                new_value=json.loads(row["new_value_json"]),
                source=str(row["source"]),
                changed_at=str(row["changed_at"]),
            )
            for row in rows
        ]

    def list_profile_system_messages(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        # Keep only the latest change per field_path to avoid repeating old profile churn
        # in every prompt view.
        rows = self._conn.execute(
            """
            SELECT id, field_path, new_value_json, source, changed_at
            FROM profile_change_log
            WHERE chat_id = ?
              AND id IN (
                SELECT MAX(id)
                FROM profile_change_log
                WHERE chat_id = ?
                GROUP BY field_path
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, chat_id, limit),
        ).fetchall()
        rows = list(reversed(rows))
        messages: list[dict[str, Any]] = []
        for row in rows:
            new_value = json.loads(row["new_value_json"])
            rendered = self._stringify_profile_value(new_value)
            messages.append(
                {
                    "event_type": "message",
                    "role": "system",
                    "text": f"profile_update: {row['field_path']} = {rendered}",
                    "ts_utc": str(row["changed_at"]),
                    "meta": {"kind": "profile_change", "change_id": int(row["id"])},
                }
            )
        return messages

    def _ensure_analyses_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(analyses)").fetchall()
        columns = {str(row[1]) for row in rows}
        if "analysis_json" not in columns:
            self._conn.execute("ALTER TABLE analyses ADD COLUMN analysis_json TEXT NOT NULL DEFAULT '{}'")
        if "actions_json" not in columns:
            self._conn.execute("ALTER TABLE analyses ADD COLUMN actions_json TEXT NOT NULL DEFAULT '[]'")
        if "metadata_json" not in columns:
            self._conn.execute("ALTER TABLE analyses ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")

    def _ensure_generation_attempt_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(generation_attempts)").fetchall()
        columns = {str(row[1]) for row in rows}
        if "attempt_no" not in columns:
            self._conn.execute("ALTER TABLE generation_attempts ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1")
        if "phase" not in columns:
            self._conn.execute("ALTER TABLE generation_attempts ADD COLUMN phase TEXT NOT NULL DEFAULT 'initial'")
        if "accepted" not in columns:
            self._conn.execute("ALTER TABLE generation_attempts ADD COLUMN accepted INTEGER NOT NULL DEFAULT 0")
        if "reject_reason" not in columns:
            self._conn.execute("ALTER TABLE generation_attempts ADD COLUMN reject_reason TEXT")

    def next_attempt_no(self, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) FROM generation_attempts WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        current = int(row[0]) if row is not None and row[0] is not None else 0
        return current + 1

    def save_generation_attempt(
        self,
        chat_id: int,
        provider: str,
        model: str,
        prompt_json: dict[str, Any],
        response_json: dict[str, Any],
        result_text: str,
        status: str,
        error_message: str | None = None,
        created_at: str | None = None,
        attempt_no: int | None = None,
        phase: str = "initial",
        accepted: bool = False,
        reject_reason: str | None = None,
    ) -> GenerationAttempt:
        ts = created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        attempt_no_value = int(attempt_no if attempt_no is not None else self.next_attempt_no(chat_id))
        cur = self._conn.execute(
            """
            INSERT INTO generation_attempts(
                chat_id, provider, model, prompt_json, response_json, result_text, status, error_message,
                attempt_no, phase, accepted, reject_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                provider,
                model,
                json.dumps(prompt_json, ensure_ascii=True),
                json.dumps(response_json, ensure_ascii=True),
                result_text,
                status,
                error_message,
                attempt_no_value,
                phase,
                1 if accepted else 0,
                reject_reason,
                ts,
            ),
        )
        self._conn.commit()
        return GenerationAttempt(
            id=int(cur.lastrowid),
            chat_id=chat_id,
            provider=provider,
            model=model,
            prompt_json=prompt_json,
            response_json=response_json,
            result_text=result_text,
            status=status,
            error_message=error_message,
            attempt_no=attempt_no_value,
            phase=phase,
            accepted=bool(accepted),
            reject_reason=reject_reason,
            created_at=ts,
        )

    def list_generation_attempts(self, chat_id: int, limit: int = 20) -> list[GenerationAttempt]:
        rows = self._conn.execute(
            """
            SELECT id, chat_id, provider, model, prompt_json, response_json, result_text, status,
                   error_message, attempt_no, phase, accepted, reject_reason, created_at
            FROM generation_attempts
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [
            GenerationAttempt(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                provider=str(row["provider"]),
                model=str(row["model"]),
                prompt_json=self._loads_dict(row["prompt_json"]),
                response_json=self._loads_dict(row["response_json"]),
                result_text=str(row["result_text"]),
                status=str(row["status"]),
                error_message=row["error_message"],
                attempt_no=int(row["attempt_no"]),
                phase=str(row["phase"]),
                accepted=bool(int(row["accepted"])),
                reject_reason=row["reject_reason"],
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def get_generation_attempt(self, attempt_id: int) -> GenerationAttempt | None:
        row = self._conn.execute(
            """
            SELECT id, chat_id, provider, model, prompt_json, response_json, result_text, status,
                   error_message, attempt_no, phase, accepted, reject_reason, created_at
            FROM generation_attempts
            WHERE id = ?
            LIMIT 1
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return GenerationAttempt(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            prompt_json=self._loads_dict(row["prompt_json"]),
            response_json=self._loads_dict(row["response_json"]),
            result_text=str(row["result_text"]),
            status=str(row["status"]),
            error_message=row["error_message"],
            attempt_no=int(row["attempt_no"]),
            phase=str(row["phase"]),
            accepted=bool(int(row["accepted"])),
            reject_reason=row["reject_reason"],
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _loads_dict(raw: str) -> dict[str, Any]:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _loads_list(raw: str) -> list[dict[str, Any]]:
        value = json.loads(raw)
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @classmethod
    def _deep_merge_dicts(cls, base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = dict(base)
        for key, value in patch.items():
            if value is None:
                continue
            old_value = merged.get(key)
            if isinstance(old_value, dict) and isinstance(value, dict):
                merged[key] = cls._deep_merge_dicts(old_value, value)
            else:
                merged[key] = value
        return merged

    @classmethod
    def _flatten_dict(cls, value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                out.update(cls._flatten_dict(item, prefix=path))
            else:
                out[path] = item
        return out

    @staticmethod
    def _stringify_profile_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=True)
        return str(value)
