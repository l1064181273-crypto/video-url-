from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lvt.security.urls import sanitize_display_url

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5_000
DEFAULT_WORKER_CONCURRENCY = 1

_CREATE_V3_TABLES = (
    """
    CREATE TABLE schema_version (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE jobs (
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
        options_json TEXT NOT NULL DEFAULT '{}',
        active_run_id TEXT,
        execution_count_total INTEGER NOT NULL DEFAULT 0,
        retry_cycle INTEGER NOT NULL DEFAULT 0,
        automatic_requeue_count_in_cycle INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        cancel_requested_at TEXT,
        checkpoint_pointer TEXT
    )
    """,
    """
    CREATE TABLE job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES jobs(uuid) ON DELETE CASCADE,
        status TEXT NOT NULL,
        message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(uuid) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        CHECK (key != 'worker_concurrency' OR value IN ('1', '2'))
    )
    """,
)

_CREATE_V3_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON jobs(status, next_attempt_at, created_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_job_kind
    ON artifacts(job_id, kind)
    """,
)

_V3_JOB_COLUMNS = (
    ("active_run_id", "TEXT"),
    ("execution_count_total", "INTEGER NOT NULL DEFAULT 0"),
    ("retry_cycle", "INTEGER NOT NULL DEFAULT 0"),
    ("automatic_requeue_count_in_cycle", "INTEGER NOT NULL DEFAULT 0"),
    ("next_attempt_at", "TEXT"),
    ("cancel_requested_at", "TEXT"),
    ("checkpoint_pointer", "TEXT"),
)


class UnsupportedSchemaVersionError(RuntimeError):
    pass


class JobRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            version = self._read_schema_version(connection)
            if version is not None and version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )

            connection.execute("BEGIN IMMEDIATE")
            if version is None:
                self._create_v3_schema(connection)
            elif version == 2:
                self._migrate_v2_to_v3(connection)
            elif version == SCHEMA_VERSION:
                self._ensure_v3_objects(connection)
            else:
                raise UnsupportedSchemaVersionError(
                    f"database schema {version} cannot be migrated to {SCHEMA_VERSION}"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create(self, url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        job_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    uuid, original_url, sanitized_display_url, status,
                    created_at, updated_at, next_attempt_at, options_json
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    url,
                    sanitize_display_url(url),
                    now,
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
            version = self._read_schema_version(connection)
        if version is None:
            raise RuntimeError("schema version is missing")
        return version

    def get_worker_concurrency(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'worker_concurrency'"
            ).fetchone()
        if row is None:
            raise RuntimeError("worker concurrency setting is missing")
        return int(row["value"])

    def set_worker_concurrency(self, concurrency: int) -> None:
        if type(concurrency) is not int or concurrency not in {1, 2}:
            raise ValueError("worker concurrency must be 1 or 2")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE settings SET value = ? WHERE key = 'worker_concurrency'",
                (str(concurrency),),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("worker concurrency setting is missing")

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["options"] = json.loads(result.pop("options_json"))
        return result

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int | None:
        table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_version'
            """
        ).fetchone()
        if table is None:
            existing_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if existing_tables:
                raise UnsupportedSchemaVersionError(
                    "unversioned database cannot be migrated safely"
                )
            return None

        rows = connection.execute("SELECT version FROM schema_version").fetchall()
        if len(rows) != 1:
            raise RuntimeError("schema_version must contain exactly one row")
        return int(rows[0]["version"])

    @classmethod
    def _create_v3_schema(cls, connection: sqlite3.Connection) -> None:
        for statement in _CREATE_V3_TABLES:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        cls._ensure_v3_objects(connection)

    @classmethod
    def _migrate_v2_to_v3(cls, connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        if "options_json" not in columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'"
            )
        for name, declaration in _V3_JOB_COLUMNS:
            if name not in columns:
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")

        connection.execute("UPDATE jobs SET execution_count_total = attempts")
        connection.execute(
            """
            UPDATE jobs
            SET next_attempt_at = updated_at
            WHERE status = 'queued' AND next_attempt_at IS NULL
            """
        )
        connection.execute(
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                CHECK (key != 'worker_concurrency' OR value IN ('1', '2'))
            )
            """
        )
        cls._ensure_v3_objects(connection)
        connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    @staticmethod
    def _ensure_v3_objects(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('worker_concurrency', ?)
            """,
            (str(DEFAULT_WORKER_CONCURRENCY),),
        )
        for statement in _CREATE_V3_INDEXES:
            connection.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection
