import sqlite3
from pathlib import Path

from lvt.db.repository import SCHEMA_VERSION, JobRepository


def test_schema_migrates_existing_jobs_and_persists_options(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
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
                duration_ms INTEGER
            )
            """
        )

    repository = JobRepository(db_path)
    repository.initialize()
    created = repository.create(
        "https://example.test/video",
        {"asr_model": "medium", "translate_to": "zh-CN", "diarization": True},
    )

    assert repository.schema_version() == SCHEMA_VERSION
    assert created["options"]["asr_model"] == "medium"
