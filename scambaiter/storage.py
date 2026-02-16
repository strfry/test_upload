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
class StoredImageDescription:
    image_hash: str
    description: str
    updated_at: datetime

@dataclass
class StoredKnownChat:
    chat_id: int
    title: str
    updated_at: datetime


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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_descriptions (
                    image_hash TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS known_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
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

    def list_known_chats(self, limit: int = 50) -> list[StoredKnownChat]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH latest_analyses AS (
                    SELECT a.chat_id, a.title, MAX(a.created_at) AS updated_at
                    FROM analyses a
                    GROUP BY a.chat_id
                ),
                latest_kv AS (
                    SELECT k.scammer_chat_id AS chat_id, MAX(k.updated_at) AS updated_at
                    FROM key_values_by_scammer k
                    GROUP BY k.scammer_chat_id
                ),
                known AS (
                    SELECT kc.chat_id, kc.title, kc.updated_at FROM known_chats kc

                    UNION ALL

                    SELECT la.chat_id,
                           la.title,
                           COALESCE(lk.updated_at, la.updated_at) AS updated_at
                    FROM latest_analyses la
                    LEFT JOIN latest_kv lk ON lk.chat_id = la.chat_id

                    UNION ALL

                    SELECT lk.chat_id,
                           CAST(lk.chat_id AS TEXT) AS title,
                           lk.updated_at
                    FROM latest_kv lk
                    WHERE lk.chat_id NOT IN (SELECT chat_id FROM latest_analyses)
                ),
                ranked AS (
                    SELECT chat_id,
                           title,
                           updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY chat_id
                               ORDER BY updated_at DESC
                           ) AS rn
                    FROM known
                )
                SELECT chat_id, title, updated_at
                FROM ranked
                WHERE rn = 1
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        known_chats: list[StoredKnownChat] = []
        for row in rows:
            known_chats.append(
                StoredKnownChat(
                    chat_id=int(row[0]),
                    title=str(row[1]),
                    updated_at=datetime.fromisoformat(row[2]),
                )
            )
        return known_chats

    def upsert_known_chat(self, chat_id: int, title: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO known_chats (chat_id, title, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at
                """,
                (chat_id, title, now),
            )

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

    def kv_get_many(self, scammer_chat_id: int, keys: list[str]) -> dict[str, str]:
        cleaned = [key.strip().lower() for key in keys if key.strip()]
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        query = (
            "SELECT key, value FROM key_values_by_scammer "
            f"WHERE scammer_chat_id = ? AND key IN ({placeholders})"
        )
        with self._connect() as conn:
            rows = conn.execute(query, (scammer_chat_id, *cleaned)).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def image_description_get(self, image_hash: str) -> StoredImageDescription | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT image_hash, description, updated_at
                FROM image_descriptions
                WHERE image_hash = ?
                """,
                (image_hash,),
            ).fetchone()
        if not row:
            return None
        return StoredImageDescription(image_hash=row[0], description=row[1], updated_at=datetime.fromisoformat(row[2]))

    def image_description_set(self, image_hash: str, description: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO image_descriptions (image_hash, description, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(image_hash) DO UPDATE SET
                    description=excluded.description,
                    updated_at=excluded.updated_at
                """,
                (image_hash, description, now),
            )
