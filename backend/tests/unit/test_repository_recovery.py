import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lvt.core.jobs import ACTIVE_JOB_STATUSES, ErrorCode, JobEventType, JobStatus
from lvt.db.repository import JobRepository


def _repository(path: Path) -> JobRepository:
    repository = JobRepository(path)
    repository.initialize()
    return repository


def _claim(
    repository: JobRepository,
    job_id: str,
    status: JobStatus,
) -> tuple[str, str]:
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=status,
    )
    assert claimed is not None
    run_id = str(claimed["active_run_id"])
    pointer = f"{job_id}/runs/{run_id}/checkpoint/manifest.json"
    assert repository.update_worker_metadata(
        job_id,
        run_id,
        status,
        checkpoint_pointer=pointer,
    )
    assert repository.update_progress(
        job_id,
        run_id,
        status,
        stage_progress=50,
        overall_progress=67,
    )
    return run_id, pointer


@pytest.mark.parametrize("status", sorted(ACTIVE_JOB_STATUSES, key=lambda item: item.value))
def test_recovery_requeues_each_active_status_and_invalidates_old_run(
    tmp_path: Path,
    status: JobStatus,
) -> None:
    repository = _repository(tmp_path / f"{status.value}.sqlite3")
    job_id = str(repository.create(f"https://example.test/{status.value}")["uuid"])
    old_run_id, pointer = _claim(repository, job_id, status)

    summary = repository.recover_startup(now=datetime(2026, 8, 23, tzinfo=UTC))

    assert summary.interrupted_requeued == 1
    assert summary.cancelling_cancelled == 0
    recovered = repository.get(job_id)
    assert recovered is not None
    assert recovered["status"] == JobStatus.QUEUED.value
    assert recovered["active_run_id"] is None
    assert recovered["checkpoint_pointer"] == pointer
    assert recovered["stage_progress"] == 0
    assert recovered["overall_progress"] == 67
    assert recovered["error_code"] is None
    assert recovered["finished_at"] is None
    events = repository.list_events(job_id)
    assert [event["status"] for event in events].count(JobEventType.INTERRUPTED.value) == 1
    details = json.loads(events[-1]["message"])
    assert details == {
        "from_status": status.value,
        "old_run_id": old_run_id,
        "reason": "startup_recovery",
    }

    repeated = repository.recover_startup(now=datetime(2026, 8, 23, tzinfo=UTC))

    assert repeated.interrupted_requeued == 0
    assert repeated.cancelling_cancelled == 0
    assert [event["status"] for event in repository.list_events(job_id)].count(
        JobEventType.INTERRUPTED.value
    ) == 1


def test_recovery_finishes_cancelling_and_preserves_queued_and_terminal_jobs(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "mixed.sqlite3")
    cancelling_id = str(repository.create("https://example.test/cancelling")["uuid"])
    cancelling_run, pointer = _claim(
        repository,
        cancelling_id,
        JobStatus.TRANSLATING,
    )
    assert repository.request_cancel(cancelling_id, JobStatus.TRANSLATING)
    cancelled_id = str(repository.create("https://example.test/cancelled")["uuid"])
    assert repository.request_cancel(cancelled_id, JobStatus.QUEUED)
    failed_id = str(repository.create("https://example.test/failed")["uuid"])
    failed_claim = repository.claim_next(
        expected_job_id=failed_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert failed_claim is not None
    assert repository.fail_job(
        failed_id,
        str(failed_claim["active_run_id"]),
        JobStatus.DOWNLOADING,
        ErrorCode.MEDIA_INVALID,
        "invalid",
    )
    queued_id = str(repository.create("https://example.test/queued")["uuid"])
    before = {job_id: repository.get(job_id) for job_id in (queued_id, cancelled_id, failed_id)}

    summary = repository.recover_startup(now=datetime(2026, 8, 23, tzinfo=UTC))

    assert summary.interrupted_requeued == 0
    assert summary.cancelling_cancelled == 1
    cancelling = repository.get(cancelling_id)
    assert cancelling is not None
    assert cancelling["status"] == JobStatus.CANCELLED.value
    assert cancelling["active_run_id"] is None
    assert cancelling["checkpoint_pointer"] == pointer
    assert cancelling["error_code"] == ErrorCode.CANCELLED_BY_USER.value
    assert cancelling["finished_at"] is not None
    details = json.loads(repository.list_events(cancelling_id)[-1]["message"])
    assert details == {
        "old_run_id": cancelling_run,
        "reason": "startup_recovery",
    }
    for job_id in (queued_id, cancelled_id, failed_id):
        assert repository.get(job_id) == before[job_id]


def test_recovery_is_one_transaction_and_rolls_back_event_failure(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "rollback.sqlite3")
    first_id = str(repository.create("https://example.test/first")["uuid"])
    second_id = str(repository.create("https://example.test/second")["uuid"])
    first_run, _ = _claim(repository, first_id, JobStatus.DOWNLOADING)
    second_run, _ = _claim(repository, second_id, JobStatus.TRANSCRIBING)
    with repository._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_interrupted_event
            BEFORE INSERT ON job_events
            WHEN NEW.status = 'interrupted'
            BEGIN
                SELECT RAISE(ABORT, 'reject interrupted');
            END
            """
        )

    with pytest.raises(Exception, match="reject interrupted"):
        repository.recover_startup()

    first = repository.get(first_id)
    second = repository.get(second_id)
    assert first is not None and second is not None
    assert first["status"] == JobStatus.DOWNLOADING.value
    assert first["active_run_id"] == first_run
    assert second["status"] == JobStatus.TRANSCRIBING.value
    assert second["active_run_id"] == second_run
    assert all(
        event["status"] != JobEventType.INTERRUPTED.value
        for job_id in (first_id, second_id)
        for event in repository.list_events(job_id)
    )


def test_two_connections_recover_once_and_write_one_interrupted_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent.sqlite3"
    repository = _repository(database)
    job_id = str(repository.create("https://example.test/concurrent")["uuid"])
    _claim(repository, job_id, JobStatus.DIARIZING)
    barrier = threading.Barrier(2)

    def recover() -> tuple[int, int]:
        barrier.wait()
        summary = JobRepository(database).recover_startup()
        return summary.interrupted_requeued, summary.cancelling_cancelled

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(recover)
        second = executor.submit(recover)
        results = [first.result(), second.result()]

    assert sorted(results) == [(0, 0), (1, 0)]
    assert [event["status"] for event in repository.list_events(job_id)].count(
        JobEventType.INTERRUPTED.value
    ) == 1
