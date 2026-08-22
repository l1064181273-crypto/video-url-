from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lvt.security.urls import sanitize_display_url

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    uuid TEXT PRIMARY KEY,
    original_url TEXT NOT NULL,
    sanitized_display_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    stage_progress INTEGER NOT NULL DEFAULT 0,
    overall_progress INTEGER NOT NULL DEFAULT 0,
    detected_language TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    work_dir TEXT,
    duration_ms INTEGER,
    options_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(uuid) ON DELETE CASCADE,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(uuid) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class JobRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "options_json" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def create(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        job_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    uuid, original_url, sanitized_display_url, status,
                    created_at, updated_at, options_json
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    url,
                    sanitize_display_url(url),
                    now,
                    now,
                    json.dumps(options or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.execute(
                "INSERT INTO job_events (job_id, status, created_at) VALUES (?, 'queued', ?)",
                (job_id, now),
            )
        result = self.get(job_id)
        if result is None:
            raise RuntimeError("created job could not be read")
        return result

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE uuid = ?", (job_id,)).fetchone()
        return self._decode_row(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._decode_row(row) for row in rows]

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            raise RuntimeError("schema version is missing")
        return int(row["version"])

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["options"] = json.loads(result.pop("options_json"))
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
