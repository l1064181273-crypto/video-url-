import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lvt.core.jobs import ErrorCode, JobStatus
from lvt.db.repository import ArtifactRegistrationResult, JobRepository


def _repository(tmp_path: Path) -> JobRepository:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    return repository


def _create_job(repository: JobRepository, suffix: str = "video") -> dict[str, object]:
    return repository.create(f"https://example.test/{suffix}")


def _claim(
    repository: JobRepository,
    job_id: str,
    *,
    stage: JobStatus = JobStatus.DOWNLOADING,
    now: datetime | None = None,
) -> dict[str, object]:
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=stage,
        now=now,
    )
    assert claimed is not None
    return claimed


def test_two_connections_compete_for_one_job_and_only_one_claims(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job = _create_job(repository)
    job_id = str(job["uuid"])
    ready = threading.Barrier(2)

    def compete() -> dict[str, object] | None:
        contender = JobRepository(repository.db_path)
        candidate = contender.peek_next_queued()
        assert candidate is not None
        assert candidate["uuid"] == job_id
        ready.wait()
        return contender.claim_next(
            expected_job_id=job_id,
            first_required_stage=JobStatus.DOWNLOADING,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: compete(), range(2)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == JobStatus.DOWNLOADING.value
    assert winners[0]["execution_count_total"] == 1
    assert winners[0]["active_run_id"]
    events = repository.list_events(job_id)
    assert events[-1]["status"] == "claimed"
    claim_details = json.loads(events[-1]["message"])
    assert claim_details == {
        "resume_stage": JobStatus.DOWNLOADING.value,
        "run_id": winners[0]["active_run_id"],
    }


def test_claim_orders_jobs_by_next_attempt_created_at_and_uuid(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    jobs = [_create_job(repository, str(index)) for index in range(4)]
    base = datetime(2026, 8, 23, tzinfo=UTC)
    identifiers = [str(job["uuid"]) for job in jobs]

    with repository._connect() as connection:
        values = [
            (base + timedelta(minutes=3), base, identifiers[0]),
            (base + timedelta(minutes=1), base + timedelta(minutes=2), identifiers[1]),
            (base + timedelta(minutes=1), base + timedelta(minutes=1), identifiers[2]),
            (base + timedelta(minutes=1), base + timedelta(minutes=1), identifiers[3]),
        ]
        for next_attempt, created_at, job_id in values:
            connection.execute(
                """
                UPDATE jobs
                SET next_attempt_at = ?, created_at = ?, updated_at = ?
                WHERE uuid = ?
                """,
                (next_attempt.isoformat(), created_at.isoformat(), created_at.isoformat(), job_id),
            )

    expected = [
        *sorted(identifiers[2:4]),
        identifiers[1],
        identifiers[0],
    ]
    claimed_order: list[str] = []
    claim_time = base + timedelta(minutes=10)
    for expected_job_id in expected:
        candidate = repository.peek_next_queued(now=claim_time)
        assert candidate is not None
        assert candidate["uuid"] == expected_job_id
        claimed = repository.claim_next(
            expected_job_id=expected_job_id,
            first_required_stage=JobStatus.DOWNLOADING,
            now=claim_time,
        )
        assert claimed is not None
        claimed_order.append(str(claimed["uuid"]))

    assert claimed_order == expected


def test_claim_validates_resume_stage_and_rejects_strings(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])

    with pytest.raises(ValueError, match="active"):
        repository.claim_next(
            expected_job_id=job_id,
            first_required_stage=JobStatus.COMPLETED,
        )
    with pytest.raises(TypeError, match="JobStatus"):
        repository.claim_next(
            expected_job_id=job_id,
            first_required_stage="downloading",  # type: ignore[arg-type]
        )


def test_reclaimed_job_gets_new_run_id_and_preserves_first_started_at(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    first_time = datetime.now(UTC) + timedelta(seconds=1)
    first = _claim(repository, job_id, now=first_time)
    first_run_id = str(first["active_run_id"])

    assert repository.automatic_requeue(
        job_id=job_id,
        run_id=first_run_id,
        expected_status=JobStatus.DOWNLOADING,
        error_code=ErrorCode.DOWNLOAD_FAILED,
        error_message="temporary",
        next_attempt_at=first_time,
        now=first_time,
    )
    second = _claim(repository, job_id, now=first_time + timedelta(seconds=1))

    assert second["active_run_id"] != first_run_id
    assert second["execution_count_total"] == 2
    assert second["started_at"] == first_time.isoformat()


def test_stale_run_cannot_update_any_worker_owned_data(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    first = _claim(repository, job_id)
    stale_run_id = str(first["active_run_id"])
    now = datetime.now(UTC)
    assert repository.automatic_requeue(
        job_id=job_id,
        run_id=stale_run_id,
        expected_status=JobStatus.DOWNLOADING,
        error_code=ErrorCode.DOWNLOAD_FAILED,
        error_message="temporary",
        next_attempt_at=now,
    )
    current = _claim(repository, job_id, now=now + timedelta(seconds=1))
    current_run_id = str(current["active_run_id"])

    assert not repository.advance_stage(
        job_id,
        stale_run_id,
        JobStatus.DOWNLOADING,
        JobStatus.EXTRACTING,
    )
    assert not repository.update_progress(
        job_id,
        stale_run_id,
        JobStatus.DOWNLOADING,
        stage_progress=10,
        overall_progress=2,
    )
    assert not repository.update_worker_metadata(
        job_id,
        stale_run_id,
        JobStatus.DOWNLOADING,
        title="stale",
        duration_ms=99,
        detected_language="ru",
        work_dir="stale",
        checkpoint_pointer="stale",
    )
    assert not repository.fail_job(
        job_id,
        stale_run_id,
        JobStatus.DOWNLOADING,
        ErrorCode.DOWNLOAD_FAILED,
        "stale",
    )
    assert (
        repository.register_artifact(
            job_id=job_id,
            run_id=stale_run_id,
            expected_status=JobStatus.DOWNLOADING,
            artifact_id="stale-artifact",
            kind="source.txt",
            path="stale/source.txt",
        )
        is ArtifactRegistrationResult.STALE
    )
    assert not repository.complete_job(
        job_id,
        stale_run_id,
        JobStatus.EXPORTING,
    )

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["active_run_id"] == current_run_id
    assert persisted["status"] == JobStatus.DOWNLOADING.value
    assert persisted["title"] == ""
    assert persisted["duration_ms"] is None
    assert persisted["detected_language"] is None
    assert persisted["work_dir"] is None
    assert persisted["checkpoint_pointer"] is None
    assert persisted["error_code"] is None
    assert repository.list_artifacts(job_id) == []


def test_progress_is_monotonic_and_rejects_old_stage(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    claimed = _claim(repository, job_id)
    run_id = str(claimed["active_run_id"])

    assert repository.update_progress(
        job_id,
        run_id,
        JobStatus.DOWNLOADING,
        stage_progress=40,
        overall_progress=6,
    )
    assert not repository.update_progress(
        job_id,
        run_id,
        JobStatus.DOWNLOADING,
        stage_progress=39,
        overall_progress=6,
    )
    assert not repository.update_progress(
        job_id,
        run_id,
        JobStatus.DOWNLOADING,
        stage_progress=41,
        overall_progress=5,
    )
    assert not repository.update_progress(
        job_id,
        run_id,
        JobStatus.EXTRACTING,
        stage_progress=50,
        overall_progress=7,
    )

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["stage_progress"] == 40
    assert persisted["overall_progress"] == 6


def test_current_run_can_update_all_worker_metadata_with_one_cas(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    claimed = _claim(repository, job_id)
    run_id = str(claimed["active_run_id"])

    assert repository.update_worker_metadata(
        job_id,
        run_id,
        JobStatus.DOWNLOADING,
        title="Resolved title",
        duration_ms=12_345,
        detected_language="en",
        work_dir="work/job/runs/current",
        checkpoint_pointer="checkpoints/downloaded.json",
    )

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["title"] == "Resolved title"
    assert persisted["duration_ms"] == 12_345
    assert persisted["detected_language"] == "en"
    assert persisted["work_dir"] == "work/job/runs/current"
    assert persisted["checkpoint_pointer"] == "checkpoints/downloaded.json"


def test_state_and_event_roll_back_together_when_event_insert_fails(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    claimed = _claim(repository, job_id)
    run_id = str(claimed["active_run_id"])
    before_events = repository.list_events(job_id)

    with repository._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_extracting_event
            BEFORE INSERT ON job_events
            WHEN NEW.status = 'extracting'
            BEGIN
                SELECT RAISE(FAIL, 'injected event failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        repository.advance_stage(
            job_id,
            run_id,
            JobStatus.DOWNLOADING,
            JobStatus.EXTRACTING,
        )

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.DOWNLOADING.value
    assert repository.list_events(job_id) == before_events


def test_automatic_requeue_is_limited_to_two_per_cycle(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    now = datetime.now(UTC)

    for expected_count in (1, 2):
        claimed = _claim(repository, job_id, now=now)
        assert repository.automatic_requeue(
            job_id=job_id,
            run_id=str(claimed["active_run_id"]),
            expected_status=JobStatus.DOWNLOADING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="temporary",
            next_attempt_at=now,
            now=now,
        )
        persisted = repository.get(job_id)
        assert persisted is not None
        assert persisted["automatic_requeue_count_in_cycle"] == expected_count

    third = _claim(repository, job_id, now=now)
    assert not repository.automatic_requeue(
        job_id=job_id,
        run_id=str(third["active_run_id"]),
        expected_status=JobStatus.DOWNLOADING,
        error_code=ErrorCode.DOWNLOAD_FAILED,
        error_message="temporary",
        next_attempt_at=now,
        now=now,
    )
    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.DOWNLOADING.value
    assert persisted["automatic_requeue_count_in_cycle"] == 2


def test_manual_retry_starts_new_cycle_without_resetting_total_execution(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    now = datetime.now(UTC)

    for _ in range(2):
        claimed = _claim(repository, job_id, now=now)
        assert repository.automatic_requeue(
            job_id=job_id,
            run_id=str(claimed["active_run_id"]),
            expected_status=JobStatus.DOWNLOADING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="temporary",
            next_attempt_at=now,
            now=now,
        )
    third = _claim(repository, job_id, now=now)
    assert repository.fail_job(
        job_id,
        str(third["active_run_id"]),
        JobStatus.DOWNLOADING,
        ErrorCode.DOWNLOAD_FAILED,
        "final failure",
        now=now,
    )

    assert repository.manual_retry(job_id, JobStatus.FAILED, now=now)
    retried = repository.get(job_id)
    assert retried is not None
    assert retried["status"] == JobStatus.QUEUED.value
    assert retried["execution_count_total"] == 3
    assert retried["retry_cycle"] == 1
    assert retried["automatic_requeue_count_in_cycle"] == 0
    assert retried["active_run_id"] is None
    assert retried["error_code"] is None


def test_running_and_queued_cancel_follow_active_run_lifecycle(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    queued_id = str(_create_job(repository, "queued")["uuid"])
    running_id = str(_create_job(repository, "running")["uuid"])

    assert repository.request_cancel(queued_id, JobStatus.QUEUED)
    queued = repository.get(queued_id)
    assert queued is not None
    assert queued["status"] == JobStatus.CANCELLED.value
    assert queued["active_run_id"] is None
    assert queued["error_code"] == ErrorCode.CANCELLED_BY_USER.value

    claimed = _claim(repository, running_id)
    run_id = str(claimed["active_run_id"])
    assert repository.request_cancel(running_id, JobStatus.DOWNLOADING)
    cancelling = repository.get(running_id)
    assert cancelling is not None
    assert cancelling["status"] == JobStatus.CANCELLING.value
    assert cancelling["active_run_id"] == run_id

    assert repository.mark_cancelled(
        running_id,
        run_id,
        JobStatus.CANCELLING,
    )
    cancelled = repository.get(running_id)
    assert cancelled is not None
    assert cancelled["status"] == JobStatus.CANCELLED.value
    assert cancelled["active_run_id"] is None


def test_artifact_registration_is_idempotent_or_explicitly_conflicts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    claimed = _claim(repository, job_id, stage=JobStatus.EXPORTING)
    run_id = str(claimed["active_run_id"])

    first = repository.register_artifact(
        job_id=job_id,
        run_id=run_id,
        expected_status=JobStatus.EXPORTING,
        artifact_id="artifact-1",
        kind="source.txt",
        path="run/source.txt",
    )
    repeated = repository.register_artifact(
        job_id=job_id,
        run_id=run_id,
        expected_status=JobStatus.EXPORTING,
        artifact_id="artifact-1",
        kind="source.txt",
        path="run/source.txt",
    )
    conflict = repository.register_artifact(
        job_id=job_id,
        run_id=run_id,
        expected_status=JobStatus.EXPORTING,
        artifact_id="artifact-2",
        kind="source.txt",
        path="run/other-source.txt",
    )

    assert first is ArtifactRegistrationResult.CREATED
    assert repeated is ArtifactRegistrationResult.IDEMPOTENT
    assert conflict is ArtifactRegistrationResult.CONFLICT
    assert len(repository.list_artifacts(job_id)) == 1


def test_repository_rejects_string_statuses_and_error_codes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    claimed = _claim(repository, job_id)
    run_id = str(claimed["active_run_id"])

    with pytest.raises(TypeError, match="JobStatus"):
        repository.advance_stage(
            job_id,
            run_id,
            "downloading",  # type: ignore[arg-type]
            JobStatus.EXTRACTING,
        )
    with pytest.raises(TypeError, match="JobStatus"):
        repository.advance_stage(
            job_id,
            run_id,
            "interrupted",  # type: ignore[arg-type]
            JobStatus.EXTRACTING,
        )
    with pytest.raises(TypeError, match="ErrorCode"):
        repository.automatic_requeue(
            job_id=job_id,
            run_id=run_id,
            expected_status=JobStatus.DOWNLOADING,
            error_code="DOWNLOAD_FAILED",  # type: ignore[arg-type]
            error_message="temporary",
            next_attempt_at=datetime.now(UTC),
        )


def test_sqlite_lock_waits_then_cas_update_succeeds(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(_create_job(repository)["uuid"])
    claimed = _claim(repository, job_id)
    run_id = str(claimed["active_run_id"])
    attempted = threading.Event()

    def update() -> bool:
        attempted.set()
        return repository.update_progress(
            job_id,
            run_id,
            JobStatus.DOWNLOADING,
            stage_progress=10,
            overall_progress=1,
        )

    lock_connection = repository._connect()
    lock_connection.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(update)
            assert attempted.wait(timeout=1)
            assert not result.done()
            lock_connection.commit()
            assert result.result(timeout=2)
    finally:
        if lock_connection.in_transaction:
            lock_connection.rollback()
        lock_connection.close()
