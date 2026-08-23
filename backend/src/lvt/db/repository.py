from __future__ import annotations

import builtins
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

from lvt.core.jobs import (
    ACTIVE_JOB_STATUSES,
    ERROR_POLICIES,
    ErrorCode,
    JobEventType,
    JobStatus,
    can_transition,
)
from lvt.security.urls import sanitize_display_url

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5_000
DEFAULT_WORKER_CONCURRENCY = 1
MAX_AUTOMATIC_REQUEUES_PER_CYCLE = 2

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


class ArtifactRegistrationResult(StrEnum):
    CREATED = "created"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    STALE = "stale"


class JobRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = self._read_schema_version(connection)
            if version is not None and version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )

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

    def peek_next_queued(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        timestamp = self._timestamp(now)
        with self._connect() as connection:
            row = self._select_next_queued(connection, timestamp)
        return self._decode_row(row) if row else None

    def claim_next(
        self,
        *,
        expected_job_id: str,
        first_required_stage: JobStatus,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        self._require_status(first_required_stage)
        if first_required_stage not in ACTIVE_JOB_STATUSES:
            raise ValueError("first_required_stage must be an active JobStatus")
        timestamp = self._timestamp(now)
        run_id = str(uuid.uuid4())

        with self._write_transaction() as connection:
            candidate = self._select_next_queued(connection, timestamp)
            if candidate is None or candidate["uuid"] != expected_job_id:
                return None
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    stage_progress = 0,
                    active_run_id = ?,
                    execution_count_total = execution_count_total + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?,
                    finished_at = NULL,
                    next_attempt_at = NULL,
                    error_code = NULL,
                    error_message = NULL
                WHERE uuid = ?
                  AND status = ?
                  AND active_run_id IS NULL
                """,
                (
                    first_required_stage.value,
                    run_id,
                    timestamp,
                    timestamp,
                    expected_job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_event(
                connection,
                expected_job_id,
                JobEventType.CLAIMED.value,
                timestamp,
                {
                    "run_id": run_id,
                    "resume_stage": first_required_stage.value,
                },
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE uuid = ?", (expected_job_id,)
            ).fetchone()
        if claimed is None:
            raise RuntimeError("claimed job could not be read")
        return self._decode_row(claimed)

    def advance_stage(
        self,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        target_status: JobStatus,
        *,
        now: datetime | None = None,
    ) -> bool:
        self._require_status(expected_status)
        self._require_status(target_status)
        if expected_status not in ACTIVE_JOB_STATUSES:
            raise ValueError("expected_status must be an active JobStatus")
        if target_status not in ACTIVE_JOB_STATUSES or not can_transition(
            expected_status, target_status
        ):
            raise ValueError("target_status is not a legal active-stage transition")
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage_progress = 0, updated_at = ?
                WHERE uuid = ? AND active_run_id = ? AND status = ?
                """,
                (
                    target_status.value,
                    timestamp,
                    job_id,
                    run_id,
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                target_status.value,
                timestamp,
                {"run_id": run_id, "from_status": expected_status.value},
            )
        return True

    def update_progress(
        self,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        *,
        stage_progress: int,
        overall_progress: int,
        now: datetime | None = None,
    ) -> bool:
        self._require_active_status(expected_status)
        self._require_progress(stage_progress, "stage_progress")
        self._require_progress(overall_progress, "overall_progress")
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET stage_progress = ?, overall_progress = ?, updated_at = ?
                WHERE uuid = ?
                  AND active_run_id = ?
                  AND status = ?
                  AND stage_progress <= ?
                  AND overall_progress <= ?
                """,
                (
                    stage_progress,
                    overall_progress,
                    timestamp,
                    job_id,
                    run_id,
                    expected_status.value,
                    stage_progress,
                    overall_progress,
                ),
            )
        return cursor.rowcount == 1

    def update_worker_metadata(
        self,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        *,
        title: str | None = None,
        duration_ms: int | None = None,
        detected_language: str | None = None,
        work_dir: str | None = None,
        checkpoint_pointer: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        self._require_active_status(expected_status)
        updates: list[tuple[str, object]] = []
        if title is not None:
            updates.append(("title", title))
        if duration_ms is not None:
            if type(duration_ms) is not int or duration_ms <= 0:
                raise ValueError("duration_ms must be a positive integer")
            updates.append(("duration_ms", duration_ms))
        if detected_language is not None:
            updates.append(("detected_language", detected_language))
        if work_dir is not None:
            updates.append(("work_dir", work_dir))
        if checkpoint_pointer is not None:
            updates.append(("checkpoint_pointer", checkpoint_pointer))
        if not updates:
            raise ValueError("at least one worker metadata field is required")

        timestamp = self._timestamp(now)
        assignments = ", ".join(f"{column} = ?" for column, _ in updates)
        parameters = [value for _, value in updates]
        parameters.extend([timestamp, job_id, run_id, expected_status.value])
        with self._write_transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET {assignments}, updated_at = ?
                WHERE uuid = ? AND active_run_id = ? AND status = ?
                """,
                parameters,
            )
        return cursor.rowcount == 1

    def fail_job(
        self,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        error_code: ErrorCode,
        error_message: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        self._require_active_status(expected_status)
        self._require_error_code(error_code)
        if not can_transition(expected_status, JobStatus.FAILED):
            raise ValueError("expected_status cannot transition to failed")
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    active_run_id = NULL,
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE uuid = ? AND active_run_id = ? AND status = ?
                """,
                (
                    JobStatus.FAILED.value,
                    error_code.value,
                    error_message,
                    timestamp,
                    timestamp,
                    job_id,
                    run_id,
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                JobEventType.FAILED.value,
                timestamp,
                {"run_id": run_id, "error_code": error_code.value},
            )
        return True

    def complete_job(
        self,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        *,
        now: datetime | None = None,
    ) -> bool:
        self._require_status(expected_status)
        if not can_transition(expected_status, JobStatus.COMPLETED):
            raise ValueError("expected_status cannot transition to completed")
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    stage_progress = 100,
                    overall_progress = 100,
                    active_run_id = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?,
                    finished_at = ?
                WHERE uuid = ? AND active_run_id = ? AND status = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    timestamp,
                    timestamp,
                    job_id,
                    run_id,
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                JobEventType.COMPLETED.value,
                timestamp,
                {"run_id": run_id},
            )
        return True

    def automatic_requeue(
        self,
        *,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        error_code: ErrorCode,
        error_message: str,
        next_attempt_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        self._require_active_status(expected_status)
        self._require_error_code(error_code)
        if not ERROR_POLICIES[error_code].auto_requeue:
            raise ValueError("error_code is not eligible for automatic requeue")
        timestamp = self._timestamp(now)
        next_timestamp = self._timestamp(next_attempt_at)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    stage_progress = 0,
                    active_run_id = NULL,
                    automatic_requeue_count_in_cycle =
                        automatic_requeue_count_in_cycle + 1,
                    next_attempt_at = ?,
                    updated_at = ?,
                    finished_at = NULL,
                    error_code = NULL,
                    error_message = NULL
                WHERE uuid = ?
                  AND active_run_id = ?
                  AND status = ?
                  AND automatic_requeue_count_in_cycle < ?
                """,
                (
                    JobStatus.QUEUED.value,
                    next_timestamp,
                    timestamp,
                    job_id,
                    run_id,
                    expected_status.value,
                    MAX_AUTOMATIC_REQUEUES_PER_CYCLE,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                JobEventType.AUTOMATIC_REQUEUED.value,
                timestamp,
                {
                    "run_id": run_id,
                    "error_code": error_code.value,
                    "error_message": error_message,
                },
            )
        return True

    def manual_retry(
        self,
        job_id: str,
        expected_status: JobStatus,
        *,
        now: datetime | None = None,
    ) -> bool:
        self._require_status(expected_status)
        if expected_status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise ValueError("manual retry requires failed or cancelled status")
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    stage_progress = 0,
                    active_run_id = NULL,
                    retry_cycle = retry_cycle + 1,
                    automatic_requeue_count_in_cycle = 0,
                    next_attempt_at = ?,
                    cancel_requested_at = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    finished_at = NULL,
                    updated_at = ?
                WHERE uuid = ? AND status = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                    job_id,
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                JobEventType.MANUAL_RETRY.value,
                timestamp,
                {"from_status": expected_status.value},
            )
        return True

    def request_cancel(
        self,
        job_id: str,
        expected_status: JobStatus,
        *,
        now: datetime | None = None,
    ) -> bool:
        self._require_status(expected_status)
        timestamp = self._timestamp(now)
        if expected_status is JobStatus.QUEUED:
            target_status = JobStatus.CANCELLED
            event_status = JobEventType.CANCELLED.value
        elif expected_status in ACTIVE_JOB_STATUSES:
            target_status = JobStatus.CANCELLING
            event_status = JobEventType.CANCEL_REQUESTED.value
        else:
            raise ValueError("cancel requires queued or active status")

        with self._write_transaction() as connection:
            if expected_status is JobStatus.QUEUED:
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        active_run_id = NULL,
                        cancel_requested_at = ?,
                        error_code = ?,
                        error_message = ?,
                        updated_at = ?,
                        finished_at = ?
                    WHERE uuid = ? AND status = ? AND active_run_id IS NULL
                    """,
                    (
                        target_status.value,
                        timestamp,
                        ErrorCode.CANCELLED_BY_USER.value,
                        "任务已由用户取消",
                        timestamp,
                        timestamp,
                        job_id,
                        expected_status.value,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, cancel_requested_at = ?, updated_at = ?
                    WHERE uuid = ?
                      AND status = ?
                      AND active_run_id IS NOT NULL
                    """,
                    (
                        target_status.value,
                        timestamp,
                        timestamp,
                        job_id,
                        expected_status.value,
                    ),
                )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                event_status,
                timestamp,
                {"from_status": expected_status.value},
            )
        return True

    def mark_cancelled(
        self,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        *,
        now: datetime | None = None,
    ) -> bool:
        self._require_status(expected_status)
        if expected_status is not JobStatus.CANCELLING:
            raise ValueError("worker cancellation requires cancelling status")
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    active_run_id = NULL,
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE uuid = ? AND active_run_id = ? AND status = ?
                """,
                (
                    JobStatus.CANCELLED.value,
                    ErrorCode.CANCELLED_BY_USER.value,
                    "任务已由用户取消",
                    timestamp,
                    timestamp,
                    job_id,
                    run_id,
                    expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_event(
                connection,
                job_id,
                JobEventType.CANCELLED.value,
                timestamp,
                {"run_id": run_id},
            )
        return True

    def register_artifact(
        self,
        *,
        job_id: str,
        run_id: str,
        expected_status: JobStatus,
        artifact_id: str,
        kind: str,
        path: str,
        now: datetime | None = None,
    ) -> ArtifactRegistrationResult:
        self._require_active_status(expected_status)
        timestamp = self._timestamp(now)

        with self._write_transaction() as connection:
            owner = connection.execute(
                """
                SELECT 1
                FROM jobs
                WHERE uuid = ? AND active_run_id = ? AND status = ?
                """,
                (job_id, run_id, expected_status.value),
            ).fetchone()
            if owner is None:
                return ArtifactRegistrationResult.STALE
            existing = connection.execute(
                """
                SELECT id, job_id, kind, path
                FROM artifacts
                WHERE id = ? OR (job_id = ? AND kind = ?)
                """,
                (artifact_id, job_id, kind),
            ).fetchone()
            if existing is not None:
                if (
                    existing["id"] == artifact_id
                    and existing["job_id"] == job_id
                    and existing["kind"] == kind
                    and existing["path"] == path
                ):
                    return ArtifactRegistrationResult.IDEMPOTENT
                return ArtifactRegistrationResult.CONFLICT
            connection.execute(
                """
                INSERT INTO artifacts (id, job_id, kind, path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (artifact_id, job_id, kind, path, timestamp),
            )
        return ArtifactRegistrationResult.CREATED

    def list_events(self, job_id: str) -> builtins.list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_artifacts(self, job_id: str) -> builtins.list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY kind, id", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _select_next_queued(connection: sqlite3.Connection, timestamp: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE status = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY next_attempt_at, created_at, uuid
                LIMIT 1
                """,
                (JobStatus.QUEUED.value, timestamp),
            ).fetchone(),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job_id: str,
        status: str,
        timestamp: str,
        details: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (job_id, status, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                job_id,
                status,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                timestamp,
            ),
        )

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_status(status: JobStatus) -> None:
        if not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus")

    @classmethod
    def _require_active_status(cls, status: JobStatus) -> None:
        cls._require_status(status)
        if status not in ACTIVE_JOB_STATUSES:
            raise ValueError("status must be an active JobStatus")

    @staticmethod
    def _require_error_code(error_code: ErrorCode) -> None:
        if not isinstance(error_code, ErrorCode):
            raise TypeError("error_code must be an ErrorCode")

    @staticmethod
    def _require_progress(value: int, field: str) -> None:
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError(f"{field} must be an integer from 0 to 100")

    @staticmethod
    def _timestamp(value: datetime | None = None) -> str:
        timestamp = value or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("timestamps must include timezone information")
        return timestamp.isoformat()

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
        self._enable_wal(connection)
        return connection

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        deadline = monotonic() + BUSY_TIMEOUT_MS / 1_000
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if (
                    exc.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or monotonic() >= deadline
                ):
                    raise
                sleep(0.01)
