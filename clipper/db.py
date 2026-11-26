"""Database access.

SQL is written with "?" placeholders and portable column types (TEXT ids and
timestamps, REAL, INTEGER) so the same statements run on SQLite (solo and small
VPS) and on PostgreSQL (multi-user). The PostgreSQL backend is selected by a
postgresql:// DATABASE_URL.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- "llm" or "transcription"
    preset TEXT NOT NULL,          -- groq, gemini, openai, ..., whisper_local
    name TEXT NOT NULL,
    base_url TEXT,
    model TEXT,
    api_key_enc TEXT,
    api_key_hint TEXT,             -- last four characters only, for display
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_test_ok INTEGER,
    last_test_error TEXT,
    last_tested_at TEXT,
    last_rate_limit TEXT,
    limits_note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_usage (
    provider_id TEXT NOT NULL,
    day TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider_id, day)
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_json TEXT,            -- clip settings applied to every project in the batch
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    batch_id TEXT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,     -- upload | link
    source_location TEXT NOT NULL, -- original filename or URL
    source_key TEXT,               -- storage key of the source file
    duration REAL,
    file_size INTEGER,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    status_message TEXT,
    theme TEXT,
    clip_length REAL,
    clip_count INTEGER,
    mirror INTEGER NOT NULL DEFAULT 0,
    captions_style TEXT NOT NULL DEFAULT 'classic',
    orientation TEXT NOT NULL DEFAULT 'landscape',
    resolution TEXT NOT NULL DEFAULT '1080p',
    transcription_mode TEXT NOT NULL DEFAULT 'local',  -- local | cloud
    transcription_engine TEXT,                       -- the engine that actually ran
    error TEXT,
    provider_used TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    project_id TEXT PRIMARY KEY,
    segments_json TEXT NOT NULL,
    words_json TEXT,
    engine TEXT NOT NULL,
    language TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    id TEXT PRIMARY KEY,
    theme TEXT NOT NULL,
    kind TEXT NOT NULL,            -- sound | broll
    file_name TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    duration REAL,
    content_hash TEXT NOT NULL,
    transcript_json TEXT,
    tags_json TEXT,
    license TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    score REAL,
    reason TEXT,
    status TEXT NOT NULL,          -- pending | rendering | done | failed
    output_key TEXT,
    provider_used TEXT,
    settings_json TEXT,
    effects_json TEXT,
    error TEXT,
    views INTEGER,
    likes INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT,
    type TEXT NOT NULL,            -- download | transcribe | plan | render
    status TEXT NOT NULL,          -- queued | running | done | failed
    progress REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT,
    error TEXT,
    run_after TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (status, run_after);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_clips_project ON clips (project_id, idx);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    """Thin wrapper. Every call opens its own connection, so it is safe across threads."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.Lock()
        if url.startswith("sqlite:///"):
            self.kind = "sqlite"
            self.path = Path(url[len("sqlite:///"):])
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif url.startswith("postgresql://") or url.startswith("postgres://"):
            self.kind = "postgres"
            self.path = None
        else:
            raise ValueError(f"Unsupported DATABASE_URL: {url}")
        self.init_schema()

    # -- connections -------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[Any]:
        if self.kind == "sqlite":
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:  # pragma: no cover - exercised only with a PostgreSQL server
            import psycopg
            from psycopg.rows import dict_row

            conn = psycopg.connect(self.url, row_factory=dict_row)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _sql(self, sql: str) -> str:
        return sql if self.kind == "sqlite" else sql.replace("?", "%s")

    def init_schema(self) -> None:
        with self._conn() as conn:
            if self.kind == "sqlite":
                conn.executescript(SCHEMA)
            else:  # pragma: no cover
                for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
                    conn.execute(stmt)

    # -- helpers -----------------------------------------------------------
    def one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(self._sql(sql), params).fetchone()
            return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(self._sql(sql), params).fetchall()]

    def run(self, sql: str, params: tuple = ()) -> int:
        with self._conn() as conn:
            cur = conn.execute(self._sql(sql), params)
            return cur.rowcount

    def run_many(self, statements: list[tuple[str, tuple]]) -> None:
        """Run several statements in one transaction."""
        with self._conn() as conn:
            for sql, params in statements:
                conn.execute(self._sql(sql), params)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)
