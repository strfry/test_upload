from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass
class StoredAnalysis:
    created_at: datetime
    chat_id: int
    title: str
    suggestion: str
    analysis: str | None
    metadata: dict[str, str]


@dataclass
class StoredKeyValue:
    scammer_chat_id: int
    key: str
    value: str
    updated_at: datetime


class AnalysisStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    suggestion TEXT NOT NULL,
                    analysis TEXT,
                    metadata_json TEXT
                )
                """
            )
            self._ensure_column(conn, "analyses", "metadata_json", "TEXT")
            self._ensure_column(conn, "analyses", "language", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS key_values_by_scammer (
                    scammer_chat_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scammer_chat_id, key)
                )
                """
            )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def save(
        self,
        *,
        chat_id: int,
        title: str,
        suggestion: str,
        analysis: str | None,
        metadata: dict[str, str],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analyses (created_at, chat_id, title, suggestion, analysis, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    chat_id,
                    title,
                    suggestion,
                    analysis,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            self._upsert_kv_snapshot(
                conn,
                scammer_chat_id=chat_id,
                suggestion=suggestion,
                analysis=analysis,
                metadata=metadata,
                now=now,
            )

    def _upsert_kv_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        scammer_chat_id: int,
        suggestion: str,
        analysis: str | None,
        metadata: dict[str, str],
        now: str,
    ) -> None:
        kv_items = dict(metadata)
        kv_items["antwort"] = suggestion
        if analysis:
            kv_items["analyse"] = analysis

        for key, value in kv_items.items():
            key = str(key).strip().lower()
            value = str(value).strip()
            if not key or not value:
                continue
            conn.execute(
                """
                INSERT INTO key_values_by_scammer (scammer_chat_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scammer_chat_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (scammer_chat_id, key, value, now),
            )

    def latest(self, limit: int = 5) -> list[StoredAnalysis]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, chat_id, title, suggestion, analysis, metadata_json, language
                FROM analyses
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            StoredAnalysis(
                created_at=datetime.fromisoformat(row[0]),
                chat_id=row[1],
                title=row[2],
                suggestion=row[3],
                analysis=row[4],
                metadata=self._decode_metadata(row[5], row[6]),
            )
            for row in rows
        ]

    @staticmethod
    def _decode_metadata(metadata_json: str | None, legacy_language: str | None = None) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if metadata_json:
            try:
                data = json.loads(metadata_json)
                if isinstance(data, dict):
                    metadata = {str(k): str(v) for k, v in data.items()}
            except json.JSONDecodeError:
                pass
        if legacy_language and "sprache" not in metadata:
            metadata["sprache"] = legacy_language
        return metadata

    def kv_set(self, scammer_chat_id: int, key: str, value: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO key_values_by_scammer (scammer_chat_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scammer_chat_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (scammer_chat_id, key, value, now),
            )

    def kv_get(self, scammer_chat_id: int, key: str) -> StoredKeyValue | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scammer_chat_id, key, value, updated_at
                FROM key_values_by_scammer
                WHERE scammer_chat_id = ? AND key = ?
                """,
                (scammer_chat_id, key),
            ).fetchone()
        if not row:
            return None
        return StoredKeyValue(scammer_chat_id=row[0], key=row[1], value=row[2], updated_at=datetime.fromisoformat(row[3]))

    def kv_delete(self, scammer_chat_id: int, key: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM key_values_by_scammer WHERE scammer_chat_id = ? AND key = ?",
                (scammer_chat_id, key),
            )
            return result.rowcount > 0

    def kv_list(self, scammer_chat_id: int, limit: int = 20) -> list[StoredKeyValue]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scammer_chat_id, key, value, updated_at
                FROM key_values_by_scammer
                WHERE scammer_chat_id = ?
                ORDER BY key ASC
                LIMIT ?
                """,
                (scammer_chat_id, limit),
            ).fetchall()
        return [
            StoredKeyValue(scammer_chat_id=row[0], key=row[1], value=row[2], updated_at=datetime.fromisoformat(row[3]))
            for row in rows
        ]
