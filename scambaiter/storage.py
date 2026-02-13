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
                CREATE TABLE IF NOT EXISTS key_values (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analyses (created_at, chat_id, title, suggestion, analysis, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    chat_id,
                    title,
                    suggestion,
                    analysis,
                    json.dumps(metadata, ensure_ascii=False),
                ),
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

    def kv_set(self, key: str, value: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO key_values (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def kv_get(self, key: str) -> StoredKeyValue | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, updated_at FROM key_values WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return StoredKeyValue(key=row[0], value=row[1], updated_at=datetime.fromisoformat(row[2]))

    def kv_delete(self, key: str) -> bool:
        with self._connect() as conn:
            result = conn.execute("DELETE FROM key_values WHERE key = ?", (key,))
            return result.rowcount > 0

    def kv_list(self, limit: int = 20) -> list[StoredKeyValue]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT key, value, updated_at
                FROM key_values
                ORDER BY key ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [StoredKeyValue(key=row[0], value=row[1], updated_at=datetime.fromisoformat(row[2])) for row in rows]
