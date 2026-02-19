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
    actions: list[dict[str, object]] | None
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
class StoredDirective:
    id: int
    chat_id: int
    text: str
    scope: str
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass
class StoredGenerationAttempt:
    id: int
    created_at: datetime
    chat_id: int
    title: str
    trigger: str
    attempt_no: int
    phase: str
    parsed_ok: bool
    accepted: bool
    reject_reason: str | None
    heuristic_score: float | None
    heuristic_flags: list[str]
    raw_excerpt: str | None
    suggestion: str | None
    schema: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None


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
                    actions_json TEXT,
                    metadata_json TEXT
                )
                """
            )
            self._ensure_column(conn, "analyses", "actions_json", "TEXT")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS directives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'session',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_directives_chat_active ON directives (chat_id, active, id DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    parsed_ok INTEGER NOT NULL,
                    accepted INTEGER NOT NULL,
                    reject_reason TEXT,
                    heuristic_score REAL,
                    heuristic_flags_json TEXT,
                    raw_excerpt TEXT,
                    suggestion TEXT,
                    schema TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    reasoning_tokens INTEGER
                )
                """
            )
            self._ensure_column(conn, "generation_attempts", "prompt_tokens", "INTEGER")
            self._ensure_column(conn, "generation_attempts", "completion_tokens", "INTEGER")
            self._ensure_column(conn, "generation_attempts", "total_tokens", "INTEGER")
            self._ensure_column(conn, "generation_attempts", "reasoning_tokens", "INTEGER")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_generation_attempts_chat_id_id ON generation_attempts (chat_id, id DESC)"
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
        analysis: dict[str, object] | None,
        actions: list[dict[str, object]] | None,
        metadata: dict[str, str],
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        analysis_json = json.dumps(analysis, ensure_ascii=False) if isinstance(analysis, dict) else None
        actions_json = json.dumps(actions, ensure_ascii=False) if isinstance(actions, list) else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analyses (created_at, chat_id, title, suggestion, analysis, actions_json, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    chat_id,
                    title,
                    suggestion,
                    analysis_json,
                    actions_json,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )

    def latest(self, limit: int = 5) -> list[StoredAnalysis]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, chat_id, title, suggestion, analysis, actions_json, metadata_json
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
                actions=self._decode_actions(row[5]),
                metadata=self._decode_metadata(row[6]),
            )
            for row in rows
        ]

    def latest_for_chat(self, chat_id: int) -> StoredAnalysis | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at, chat_id, title, suggestion, analysis, actions_json, metadata_json
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
            actions=self._decode_actions(row[5]),
            metadata=self._decode_metadata(row[6]),
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

    def add_directive(self, chat_id: int, text: str, scope: str = "session") -> StoredDirective | None:
        clean_text = text.strip()
        clean_scope = scope.strip().lower() or "session"
        if not clean_text:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO directives (chat_id, text, scope, active, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (int(chat_id), clean_text, clean_scope, now, now),
            )
            row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            row = conn.execute(
                """
                SELECT id, chat_id, text, scope, active, created_at, updated_at
                FROM directives
                WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
        if not row:
            return None
        return StoredDirective(
            id=int(row[0]),
            chat_id=int(row[1]),
            text=str(row[2]),
            scope=str(row[3]),
            active=bool(int(row[4])),
            created_at=datetime.fromisoformat(row[5]),
            updated_at=datetime.fromisoformat(row[6]),
        )

    def list_directives(self, chat_id: int, active_only: bool = True, limit: int = 50) -> list[StoredDirective]:
        query = (
            """
            SELECT id, chat_id, text, scope, active, created_at, updated_at
            FROM directives
            WHERE chat_id = ?
            """
        )
        params: list[object] = [int(chat_id)]
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            StoredDirective(
                id=int(row[0]),
                chat_id=int(row[1]),
                text=str(row[2]),
                scope=str(row[3]),
                active=bool(int(row[4])),
                created_at=datetime.fromisoformat(row[5]),
                updated_at=datetime.fromisoformat(row[6]),
            )
            for row in rows
        ]

    def delete_directive(self, chat_id: int, directive_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM directives WHERE id = ? AND chat_id = ? LIMIT 1",
                (int(directive_id), int(chat_id)),
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM directives WHERE id = ?", (int(directive_id),))
            return True

    def save_generation_attempt(
        self,
        *,
        chat_id: int,
        title: str,
        trigger: str,
        attempt_no: int,
        phase: str,
        parsed_ok: bool,
        accepted: bool,
        reject_reason: str | None,
        heuristic_score: float | None,
        heuristic_flags: list[str] | None,
        raw_excerpt: str | None,
        suggestion: str | None,
        schema: str | None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        flags_json = json.dumps(list(heuristic_flags or []), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO generation_attempts (
                    created_at, chat_id, title, trigger, attempt_no, phase,
                    parsed_ok, accepted, reject_reason, heuristic_score,
                    heuristic_flags_json, raw_excerpt, suggestion, schema,
                    prompt_tokens, completion_tokens, total_tokens, reasoning_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    int(chat_id),
                    str(title),
                    str(trigger),
                    int(attempt_no),
                    str(phase),
                    1 if parsed_ok else 0,
                    1 if accepted else 0,
                    reject_reason,
                    float(heuristic_score) if heuristic_score is not None else None,
                    flags_json,
                    raw_excerpt,
                    suggestion,
                    schema,
                    (int(prompt_tokens) if isinstance(prompt_tokens, int) else None),
                    (int(completion_tokens) if isinstance(completion_tokens, int) else None),
                    (int(total_tokens) if isinstance(total_tokens, int) else None),
                    (int(reasoning_tokens) if isinstance(reasoning_tokens, int) else None),
                ),
            )

    def list_generation_attempts_for_chat(self, chat_id: int, limit: int = 20) -> list[StoredGenerationAttempt]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, chat_id, title, trigger, attempt_no, phase,
                       parsed_ok, accepted, reject_reason, heuristic_score,
                       heuristic_flags_json, raw_excerpt, suggestion, schema,
                       prompt_tokens, completion_tokens, total_tokens, reasoning_tokens
                FROM generation_attempts
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(chat_id), int(limit)),
            ).fetchall()
        return [self._decode_generation_attempt_row(row) for row in rows]

    def list_generation_attempts_recent(self, limit: int = 50) -> list[StoredGenerationAttempt]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, chat_id, title, trigger, attempt_no, phase,
                       parsed_ok, accepted, reject_reason, heuristic_score,
                       heuristic_flags_json, raw_excerpt, suggestion, schema,
                       prompt_tokens, completion_tokens, total_tokens, reasoning_tokens
                FROM generation_attempts
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._decode_generation_attempt_row(row) for row in rows]

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

    @staticmethod
    def _decode_actions(actions_json: str | None) -> list[dict[str, object]] | None:
        if not actions_json:
            return None
        try:
            parsed = json.loads(actions_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        actions: list[dict[str, object]] = []
        for item in parsed:
            if isinstance(item, dict):
                actions.append({str(k): v for k, v in item.items()})
        return actions or None

    @staticmethod
    def _decode_generation_attempt_row(row: tuple) -> StoredGenerationAttempt:
        flags: list[str] = []
        raw_flags = row[11]
        if isinstance(raw_flags, str) and raw_flags:
            try:
                parsed = json.loads(raw_flags)
                if isinstance(parsed, list):
                    flags = [str(item) for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                flags = []
        return StoredGenerationAttempt(
            id=int(row[0]),
            created_at=datetime.fromisoformat(str(row[1])),
            chat_id=int(row[2]),
            title=str(row[3]),
            trigger=str(row[4]),
            attempt_no=int(row[5]),
            phase=str(row[6]),
            parsed_ok=bool(int(row[7])),
            accepted=bool(int(row[8])),
            reject_reason=(str(row[9]) if row[9] is not None else None),
            heuristic_score=(float(row[10]) if row[10] is not None else None),
            heuristic_flags=flags,
            raw_excerpt=(str(row[12]) if row[12] is not None else None),
            suggestion=(str(row[13]) if row[13] is not None else None),
            schema=(str(row[14]) if row[14] is not None else None),
            prompt_tokens=(int(row[15]) if row[15] is not None else None),
            completion_tokens=(int(row[16]) if row[16] is not None else None),
            total_tokens=(int(row[17]) if row[17] is not None else None),
            reasoning_tokens=(int(row[18]) if row[18] is not None else None),
        )

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
