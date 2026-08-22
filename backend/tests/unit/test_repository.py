import json
import sqlite3
from pathlib import Path

import pytest

from lvt.db.repository import SCHEMA_VERSION, JobRepository, UnsupportedSchemaVersionError


def _create_v2_database(db_path: Path, *, duplicate_artifacts: bool = False) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (version) VALUES (2);

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
                options_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(uuid) ON DELETE CASCADE,
                status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(uuid) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (
                uuid, original_url, sanitized_display_url, title, status,
                stage_progress, overall_progress, detected_language, attempts,
                created_at, updated_at, options_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-v2",
                "https://example.test/video?secret=value",
                "https://example.test/video",
                "Existing title",
                "failed",
                40,
                25,
                "en",
                2,
                "2026-08-23T00:00:00+00:00",
                "2026-08-23T00:01:00+00:00",
                json.dumps(
                    {
                        "asr_model": "medium",
                        "translate_to": "zh-CN",
                        "diarization": True,
                    }
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO job_events (job_id, status, message, created_at)
            VALUES ('job-v2', 'failed', 'existing event', '2026-08-23T00:01:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO artifacts (id, job_id, kind, path, created_at)
            VALUES ('artifact-v2', 'job-v2', 'source.txt', 'exports/source.txt',
                    '2026-08-23T00:01:00+00:00')
            """
        )
        if duplicate_artifacts:
            connection.execute(
                """
                INSERT INTO artifacts (id, job_id, kind, path, created_at)
                VALUES ('artifact-v2-duplicate', 'job-v2', 'source.txt',
                        'exports/duplicate-source.txt', '2026-08-23T00:01:01+00:00')
                """
            )


def test_v2_migration_preserves_jobs_options_events_and_artifacts(tmp_path: Path) -> None:
    db_path = tmp_path / "v2.sqlite3"
    _create_v2_database(db_path)

    repository = JobRepository(db_path)
    repository.initialize()

    assert repository.schema_version() == SCHEMA_VERSION == 3
    restored = repository.get("job-v2")
    assert restored is not None
    assert restored["title"] == "Existing title"
    assert restored["attempts"] == 2
    assert restored["execution_count_total"] == 2
    assert restored["retry_cycle"] == 0
    assert restored["automatic_requeue_count_in_cycle"] == 0
    assert restored["active_run_id"] is None
    assert restored["checkpoint_pointer"] is None
    assert restored["options"] == {
        "asr_model": "medium",
        "translate_to": "zh-CN",
        "diarization": True,
    }

    with sqlite3.connect(db_path) as connection:
        event = connection.execute(
            "SELECT status, message FROM job_events WHERE job_id = 'job-v2'"
        ).fetchone()
        artifact = connection.execute(
            "SELECT id, kind, path FROM artifacts WHERE job_id = 'job-v2'"
        ).fetchone()
    assert event == ("failed", "existing event")
    assert artifact == ("artifact-v2", "source.txt", "exports/source.txt")


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")

    repository.initialize()
    created = repository.create("https://example.test/video", {"diarization": False})
    repository.initialize()

    assert repository.schema_version() == 3
    assert repository.get(created["uuid"]) == created
    assert repository.get_worker_concurrency() == 1


def test_failed_migration_rolls_back_all_schema_changes(tmp_path: Path) -> None:
    db_path = tmp_path / "duplicate-artifacts.sqlite3"
    _create_v2_database(db_path, duplicate_artifacts=True)

    with pytest.raises(sqlite3.IntegrityError):
        JobRepository(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        settings_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
    assert version == (2,)
    assert "execution_count_total" not in columns
    assert settings_table is None


def test_future_schema_version_is_rejected_without_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION + 1,))

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than supported"):
        JobRepository(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute("SELECT version FROM schema_version").fetchone()
    assert tables == {"schema_version"}
    assert version == (SCHEMA_VERSION + 1,)


def test_sqlite_pragmas_indexes_and_constraints_are_enabled(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    job = repository.create("https://example.test/video")

    with repository._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        job_indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(jobs)").fetchall()
        }
        artifact_indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(artifacts)").fetchall()
        }
        assert "idx_jobs_claim" in job_indexes
        assert "uq_artifacts_job_kind" in artifact_indexes

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE settings SET value = '3' WHERE key = 'worker_concurrency'")

        connection.execute(
            """
            INSERT INTO artifacts (id, job_id, kind, path, created_at)
            VALUES ('first', ?, 'source.txt', 'exports/source.txt', 'now')
            """,
            (job["uuid"],),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifacts (id, job_id, kind, path, created_at)
                VALUES ('duplicate', ?, 'source.txt', 'exports/other.txt', 'now')
                """,
                (job["uuid"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO artifacts (id, job_id, kind, path, created_at)
                VALUES ('orphan', 'missing-job', 'source.srt', 'exports/source.srt', 'now')
                """
            )


@pytest.mark.parametrize("concurrency", [1, 2])
def test_worker_concurrency_accepts_only_supported_values(tmp_path: Path, concurrency: int) -> None:
    repository = JobRepository(tmp_path / f"lvt-{concurrency}.sqlite3")
    repository.initialize()

    repository.set_worker_concurrency(concurrency)

    assert repository.get_worker_concurrency() == concurrency


@pytest.mark.parametrize("concurrency", [-1, 0, 3, 10])
def test_worker_concurrency_rejects_unsupported_values(tmp_path: Path, concurrency: int) -> None:
    repository = JobRepository(tmp_path / f"lvt-{concurrency}.sqlite3")
    repository.initialize()

    with pytest.raises(ValueError, match="1 or 2"):
        repository.set_worker_concurrency(concurrency)
