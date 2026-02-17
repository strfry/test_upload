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
    analysis: dict[str, object] | None
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_descriptions (
                    image_hash TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._drop_legacy_kv_table(conn)
            self._purge_legacy_analysis_rows(conn)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, sql_type: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    @staticmethod
    def _drop_legacy_kv_table(conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS key_values_by_scammer")

    @staticmethod
    def _purge_legacy_analysis_rows(conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, analysis FROM analyses WHERE analysis IS NOT NULL").fetchall()
        for row_id, analysis_text in rows:
            if not isinstance(analysis_text, str):
                conn.execute("DELETE FROM analyses WHERE id = ?", (row_id,))
                continue
            cleaned = analysis_text.strip()
            if not cleaned:
                continue
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                conn.execute("DELETE FROM analyses WHERE id = ?", (row_id,))
                continue
            if not isinstance(parsed, dict):
                conn.execute("DELETE FROM analyses WHERE id = ?", (row_id,))

    def save(
        self,
        *,
        chat_id: int,
        title: str,
        suggestion: str,
        analysis: dict[str, object] | None,
        metadata: dict[str, str],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        analysis_json = json.dumps(analysis, ensure_ascii=False) if isinstance(analysis, dict) else None
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
                    analysis_json,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )

    def latest(self, limit: int = 5) -> list[StoredAnalysis]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, chat_id, title, suggestion, analysis, metadata_json
                FROM analyses
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            StoredAnalysis(
                created_at=datetime.fromisoformat(row[0]),
                chat_id=int(row[1]),
                title=str(row[2]),
                suggestion=str(row[3]),
                analysis=self._decode_analysis(row[4]),
                metadata=self._decode_metadata(row[5]),
            )
            for row in rows
        ]

    def latest_for_chat(self, chat_id: int) -> StoredAnalysis | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, chat_id, title, suggestion, analysis, metadata_json
                FROM analyses
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        if not row:
            return None
        return StoredAnalysis(
            created_at=datetime.fromisoformat(row[0]),
            chat_id=int(row[1]),
            title=str(row[2]),
            suggestion=str(row[3]),
            analysis=self._decode_analysis(row[4]),
            metadata=self._decode_metadata(row[5]),
        )

    def update_latest_analysis(self, chat_id: int, analysis: dict[str, object]) -> bool:
        serialized = json.dumps(analysis, ensure_ascii=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM analyses WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            if not row:
                return False
            conn.execute("UPDATE analyses SET analysis = ? WHERE id = ?", (serialized, int(row[0])))
            return True

    def list_known_chats(self, limit: int = 50) -> list[StoredKnownChat]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.chat_id, a.title, MAX(a.created_at) AS updated_at
                FROM analyses a
                GROUP BY a.chat_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            StoredKnownChat(
                chat_id=int(row[0]),
                title=str(row[1]),
                updated_at=datetime.fromisoformat(row[2]),
            )
            for row in rows
        ]

    @staticmethod
    def _decode_analysis(value: str | None) -> dict[str, object] | None:
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    @staticmethod
    def _decode_metadata(metadata_json: str | None) -> dict[str, str]:
        metadata: dict[str, str] = {}
        if metadata_json:
            try:
                data = json.loads(metadata_json)
                if isinstance(data, dict):
                    metadata = {str(k): str(v) for k, v in data.items()}
            except json.JSONDecodeError:
                pass
        return metadata

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
