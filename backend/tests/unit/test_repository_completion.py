import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from lvt.core.jobs import ErrorCode, JobStatus
from lvt.db.repository import (
    REQUIRED_ARTIFACT_KINDS,
    ArtifactCompletionResult,
    ArtifactSpec,
    AutomaticRequeueResult,
    JobRepository,
)


def _repository(tmp_path: Path) -> JobRepository:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    return repository


def _claim(
    repository: JobRepository,
    job_id: str,
    stage: JobStatus,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=stage,
        now=now,
    )
    assert claimed is not None
    return claimed


def _artifacts(job_id: str) -> list[ArtifactSpec]:
    return [
        ArtifactSpec(
            artifact_id=f"{job_id}-{kind}",
            kind=kind,
            path=f"runs/current/exports/{kind}",
        )
        for kind in sorted(REQUIRED_ARTIFACT_KINDS)
    ]


def test_datetime_writes_are_canonical_utc_and_due_checks_accept_offsets(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    plus_nine = timezone(timedelta(hours=9))
    created = datetime(2026, 8, 23, 10, 0, tzinfo=plus_nine)
    job = repository.create("https://example.test/timezone", now=created)
    job_id = str(job["uuid"])

    assert job["created_at"] == "2026-08-23T01:00:00+00:00"
    assert job["updated_at"] == "2026-08-23T01:00:00+00:00"
    assert job["next_attempt_at"] == "2026-08-23T01:00:00+00:00"
    assert (
        repository.peek_next_queued(
            now=datetime(2026, 8, 22, 20, 59, 59, tzinfo=timezone(timedelta(hours=-4)))
        )
        is None
    )
    due = repository.peek_next_queued(
        now=datetime(2026, 8, 22, 21, 0, tzinfo=timezone(timedelta(hours=-4)))
    )
    assert due is not None
    assert due["uuid"] == job_id


def test_cross_offset_sorting_uses_next_attempt_created_at_and_uuid(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plus_nine = timezone(timedelta(hours=9))
    minus_five = timezone(timedelta(hours=-5))
    jobs = [
        repository.create(
            "https://example.test/a",
            now=datetime(2026, 8, 23, 9, 0, tzinfo=plus_nine),
        ),
        repository.create(
            "https://example.test/b",
            now=datetime(2026, 8, 22, 19, 1, tzinfo=minus_five),
        ),
        repository.create(
            "https://example.test/c",
            now=datetime(2026, 8, 23, 0, 1, tzinfo=UTC),
        ),
    ]
    identifiers = [str(job["uuid"]) for job in jobs]
    initial_order = [identifiers[0], *sorted(identifiers[1:])]
    claim_time = datetime(2026, 8, 23, 2, 0, tzinfo=UTC)

    for job_id in initial_order:
        claimed = _claim(repository, job_id, JobStatus.DOWNLOADING, now=claim_time)
        assert repository.fail_job(
            job_id,
            str(claimed["active_run_id"]),
            JobStatus.DOWNLOADING,
            ErrorCode.DOWNLOAD_FAILED,
            "prepare retry",
            now=claim_time,
        )

    same_due_offsets = [
        datetime(2026, 8, 23, 12, 0, tzinfo=plus_nine),
        datetime(2026, 8, 22, 22, 0, tzinfo=minus_five),
        datetime(2026, 8, 23, 3, 0, tzinfo=UTC),
    ]
    for job_id, due in zip(identifiers, same_due_offsets, strict=True):
        assert repository.manual_retry(job_id, JobStatus.FAILED, now=due)
        persisted = repository.get(job_id)
        assert persisted is not None
        assert persisted["next_attempt_at"] == "2026-08-23T03:00:00+00:00"

    final_order: list[str] = []
    for expected_job_id in initial_order:
        candidate = repository.peek_next_queued(now=datetime(2026, 8, 23, 3, 0, tzinfo=UTC))
        assert candidate is not None
        assert candidate["uuid"] == expected_job_id
        claimed = _claim(
            repository,
            expected_job_id,
            JobStatus.DOWNLOADING,
            now=datetime(2026, 8, 23, 3, 0, tzinfo=UTC),
        )
        final_order.append(str(claimed["uuid"]))
    assert final_order == initial_order


def test_plain_completion_is_not_available(tmp_path: Path) -> None:
    assert not hasattr(_repository(tmp_path), "complete_job")


@pytest.mark.parametrize("artifact_count", [0, 1, 7, 9])
def test_atomic_completion_rejects_inexact_artifact_count(
    tmp_path: Path, artifact_count: int
) -> None:
    repository = _repository(tmp_path)
    job = repository.create(f"https://example.test/count-{artifact_count}")
    job_id = str(job["uuid"])
    claimed = _claim(repository, job_id, JobStatus.EXPORTING)
    artifacts = _artifacts(job_id)
    if artifact_count == 9:
        artifacts.append(ArtifactSpec("extra", "extra.txt", "runs/current/extra.txt"))
    else:
        artifacts = artifacts[:artifact_count]

    with pytest.raises(ValueError, match="exactly eight"):
        repository.complete_job_with_artifacts(
            job_id=job_id,
            run_id=str(claimed["active_run_id"]),
            artifacts=artifacts,
        )

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.EXPORTING.value
    assert repository.list_artifacts(job_id) == []


def test_atomic_completion_rejects_duplicate_kind(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(repository.create("https://example.test/duplicate-kind")["uuid"])
    claimed = _claim(repository, job_id, JobStatus.EXPORTING)
    artifacts = _artifacts(job_id)
    artifacts[-1] = ArtifactSpec("duplicate-kind", artifacts[0].kind, "runs/current/other")

    with pytest.raises(ValueError, match="exact artifact kinds"):
        repository.complete_job_with_artifacts(
            job_id=job_id,
            run_id=str(claimed["active_run_id"]),
            artifacts=artifacts,
        )

    assert repository.list_artifacts(job_id) == []


def test_atomic_completion_commits_exactly_eight_artifacts_and_event(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(repository.create("https://example.test/complete")["uuid"])
    claimed = _claim(repository, job_id, JobStatus.EXPORTING)
    artifacts = _artifacts(job_id)
    first = artifacts[0]
    assert (
        repository.register_artifact(
            job_id=job_id,
            run_id=str(claimed["active_run_id"]),
            expected_status=JobStatus.EXPORTING,
            artifact_id=first.artifact_id,
            kind=first.kind,
            path=first.path,
        ).value
        == "created"
    )

    result = repository.complete_job_with_artifacts(
        job_id=job_id,
        run_id=str(claimed["active_run_id"]),
        artifacts=artifacts,
    )

    assert result is ArtifactCompletionResult.COMPLETED
    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.COMPLETED.value
    assert persisted["active_run_id"] is None
    assert persisted["overall_progress"] == 100
    assert len(repository.list_artifacts(job_id)) == 8
    assert repository.list_events(job_id)[-1]["status"] == JobStatus.COMPLETED.value


def test_atomic_completion_rejects_cross_job_artifact_conflict(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    owner_id = str(repository.create("https://example.test/owner")["uuid"])
    target_id = str(repository.create("https://example.test/target")["uuid"])
    owner = _claim(repository, owner_id, JobStatus.EXPORTING)
    target = _claim(repository, target_id, JobStatus.EXPORTING)
    artifacts = _artifacts(target_id)
    conflicting = artifacts[0]
    assert (
        repository.register_artifact(
            job_id=owner_id,
            run_id=str(owner["active_run_id"]),
            expected_status=JobStatus.EXPORTING,
            artifact_id=conflicting.artifact_id,
            kind="owner-only.txt",
            path="runs/owner/file",
        ).value
        == "created"
    )

    result = repository.complete_job_with_artifacts(
        job_id=target_id,
        run_id=str(target["active_run_id"]),
        artifacts=artifacts,
    )

    assert result is ArtifactCompletionResult.CONFLICT
    persisted = repository.get(target_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.EXPORTING.value
    assert repository.list_artifacts(target_id) == []


def test_atomic_completion_rejects_stale_run_without_artifacts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(repository.create("https://example.test/stale")["uuid"])
    now = datetime.now(UTC)
    first = _claim(repository, job_id, JobStatus.EXPORTING, now=now)
    assert (
        repository.automatic_requeue(
            job_id=job_id,
            run_id=str(first["active_run_id"]),
            expected_status=JobStatus.EXPORTING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="retry",
            next_attempt_at=now,
        )
        is AutomaticRequeueResult.REQUEUED
    )
    current = _claim(repository, job_id, JobStatus.EXPORTING, now=now)

    result = repository.complete_job_with_artifacts(
        job_id=job_id,
        run_id=str(first["active_run_id"]),
        artifacts=_artifacts(job_id),
    )

    assert result is ArtifactCompletionResult.STALE
    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["active_run_id"] == current["active_run_id"]
    assert persisted["status"] == JobStatus.EXPORTING.value
    assert repository.list_artifacts(job_id) == []


def test_atomic_completion_rolls_back_artifacts_and_state_when_event_fails(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    job_id = str(repository.create("https://example.test/event-failure")["uuid"])
    claimed = _claim(repository, job_id, JobStatus.EXPORTING)
    with repository._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_completed_event
            BEFORE INSERT ON job_events
            WHEN NEW.status = 'completed'
            BEGIN
                SELECT RAISE(FAIL, 'injected completed event failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected completed event failure"):
        repository.complete_job_with_artifacts(
            job_id=job_id,
            run_id=str(claimed["active_run_id"]),
            artifacts=_artifacts(job_id),
        )

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.EXPORTING.value
    assert repository.list_artifacts(job_id) == []


def test_retry_and_cancel_race_has_exactly_one_winner(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(repository.create("https://example.test/retry-cancel")["uuid"])
    claimed = _claim(repository, job_id, JobStatus.DOWNLOADING)
    run_id = str(claimed["active_run_id"])
    barrier = threading.Barrier(2)

    def retry() -> AutomaticRequeueResult:
        barrier.wait()
        return JobRepository(repository.db_path).automatic_requeue(
            job_id=job_id,
            run_id=run_id,
            expected_status=JobStatus.DOWNLOADING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="retry",
            next_attempt_at=datetime.now(UTC),
        )

    def cancel() -> bool:
        barrier.wait()
        return JobRepository(repository.db_path).request_cancel(job_id, JobStatus.DOWNLOADING)

    with ThreadPoolExecutor(max_workers=2) as executor:
        retry_future = executor.submit(retry)
        cancel_future = executor.submit(cancel)
        retry_result = retry_future.result()
        cancel_result = cancel_future.result()

    assert (retry_result is AutomaticRequeueResult.REQUEUED) != cancel_result
    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] in {JobStatus.QUEUED.value, JobStatus.CANCELLING.value}


def test_complete_and_cancel_race_has_exactly_one_winner(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = str(repository.create("https://example.test/complete-cancel")["uuid"])
    claimed = _claim(repository, job_id, JobStatus.EXPORTING)
    run_id = str(claimed["active_run_id"])
    artifacts = _artifacts(job_id)
    barrier = threading.Barrier(2)

    def complete() -> ArtifactCompletionResult:
        barrier.wait()
        return JobRepository(repository.db_path).complete_job_with_artifacts(
            job_id=job_id,
            run_id=run_id,
            artifacts=artifacts,
        )

    def cancel() -> bool:
        barrier.wait()
        return JobRepository(repository.db_path).request_cancel(job_id, JobStatus.EXPORTING)

    with ThreadPoolExecutor(max_workers=2) as executor:
        complete_future = executor.submit(complete)
        cancel_future = executor.submit(cancel)
        complete_result = complete_future.result()
        cancel_result = cancel_future.result()

    assert (complete_result is ArtifactCompletionResult.COMPLETED) != cancel_result
    persisted = repository.get(job_id)
    assert persisted is not None
    if complete_result is ArtifactCompletionResult.COMPLETED:
        assert persisted["status"] == JobStatus.COMPLETED.value
        assert len(repository.list_artifacts(job_id)) == 8
    else:
        assert complete_result is ArtifactCompletionResult.STALE
        assert persisted["status"] == JobStatus.CANCELLING.value
        assert repository.list_artifacts(job_id) == []
