import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.errors import LVTError
from lvt.core.jobs import ErrorCode, JobStatus
from lvt.core.processes import CancellationToken
from lvt.db.repository import JobRepository
from lvt.workers.progress import (
    STAGE_WEIGHTS,
    ProgressReporter,
    calculate_overall_progress,
)
from lvt.workers.runner import JobWorkerPool


class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.current = now or datetime(2026, 8, 23, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class ErrorPipeline:
    def __init__(
        self,
        error: Exception,
        *,
        calls: list[str],
        internal_attempts: int = 1,
    ) -> None:
        self.error = error
        self.calls = calls
        self.internal_attempts = internal_attempts

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return JobStatus.DOWNLOADING

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Callable[[JobStatus, int], None],
    ) -> None:
        for _ in range(self.internal_attempts):
            self.calls.append(run_id)
        progress_callback(JobStatus.DOWNLOADING, 25)
        raise self.error


class BlockingPipeline:
    def __init__(
        self,
        *,
        active: set[str],
        lock: threading.Lock,
        reached: threading.Event,
        release: threading.Event,
        target: int,
        thread_names: list[str] | None = None,
    ) -> None:
        self.active = active
        self.lock = lock
        self.reached = reached
        self.release = release
        self.target = target
        self.thread_names = thread_names

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return JobStatus.DOWNLOADING

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Callable[[JobStatus, int], None],
    ) -> None:
        if self.thread_names is not None:
            self.thread_names.append(threading.current_thread().name)
        with self.lock:
            self.active.add(run_id)
            if len(self.active) >= self.target:
                self.reached.set()
        try:
            progress_callback(JobStatus.DOWNLOADING, 10)
            while not self.release.is_set():
                if cancellation.wait(0.05):
                    cancellation.raise_if_cancelled()
        finally:
            with self.lock:
                self.active.remove(run_id)
        raise LVTError("MEDIA_INVALID", "deterministic stop")


def _repository(tmp_path: Path) -> JobRepository:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    return repository


def _create_jobs(
    repository: JobRepository,
    count: int,
    *,
    now: datetime | None = None,
) -> list[str]:
    return [
        str(repository.create(f"https://example.test/{index}", now=now)["uuid"])
        for index in range(count)
    ]


def _wait_for_status(
    repository: JobRepository,
    job_id: str,
    expected: JobStatus,
    *,
    timeout: float = 2,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = repository.get(job_id)
        if job is not None and job["status"] == expected.value:
            return
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {expected.value}")


def test_fixed_stage_weights_and_progress_formula() -> None:
    assert STAGE_WEIGHTS == {
        JobStatus.DOWNLOADING: 15,
        JobStatus.EXTRACTING: 5,
        JobStatus.TRANSCRIBING: 35,
        JobStatus.DIARIZING: 15,
        JobStatus.SEGMENTING: 5,
        JobStatus.TRANSLATING: 20,
        JobStatus.EXPORTING: 5,
    }
    assert calculate_overall_progress(JobStatus.DOWNLOADING, 50) == 7
    assert calculate_overall_progress(JobStatus.EXTRACTING, 50) == 17
    assert calculate_overall_progress(JobStatus.TRANSCRIBING, 100) == 55
    assert calculate_overall_progress(JobStatus.EXPORTING, 100) == 100


def test_progress_reporter_rejects_stale_old_stage_and_lower_progress(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    job_id = _create_jobs(repository, 1)[0]
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claimed is not None
    run_id = str(claimed["active_run_id"])
    reporter = ProgressReporter(repository, job_id, run_id, high_water=0)

    assert reporter.persist(JobStatus.DOWNLOADING, 50)
    assert not reporter.persist(JobStatus.DOWNLOADING, 49)
    assert repository.advance_stage(
        job_id,
        run_id,
        JobStatus.DOWNLOADING,
        JobStatus.EXTRACTING,
    )
    assert not reporter.persist(JobStatus.DOWNLOADING, 100)
    assert reporter.persist(JobStatus.EXTRACTING, 10)

    now = datetime.now(UTC)
    assert (
        repository.automatic_requeue(
            job_id=job_id,
            run_id=run_id,
            expected_status=JobStatus.EXTRACTING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="retry",
            next_attempt_at=now,
        ).value
        == "requeued"
    )
    next_claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
        now=now,
    )
    assert next_claim is not None
    assert not reporter.persist(JobStatus.EXTRACTING, 100)


def test_progress_high_water_does_not_drop_after_earlier_resume(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    job_id = _create_jobs(repository, 1)[0]
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.TRANSCRIBING,
    )
    assert claimed is not None
    first_run = str(claimed["active_run_id"])
    reporter = ProgressReporter(repository, job_id, first_run, high_water=0)
    assert reporter.persist(JobStatus.TRANSCRIBING, 100)
    assert repository.get(job_id)["overall_progress"] == 55  # type: ignore[index]
    now = datetime.now(UTC)
    assert (
        repository.automatic_requeue(
            job_id=job_id,
            run_id=first_run,
            expected_status=JobStatus.TRANSCRIBING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="retry",
            next_attempt_at=now,
        ).value
        == "requeued"
    )
    second = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
        now=now,
    )
    assert second is not None
    resumed = ProgressReporter(repository, job_id, str(second["active_run_id"]), high_water=55)

    assert resumed.persist(JobStatus.DOWNLOADING, 100)
    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["stage_progress"] == 100
    assert persisted["overall_progress"] == 55


def test_retryable_error_runs_initial_plus_two_automatic_attempts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    clock = FakeClock()
    job_id = _create_jobs(repository, 1, now=clock.now())[0]
    calls: list[str] = []
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: ErrorPipeline(
            LVTError("DOWNLOAD_FAILED", "temporary"),
            calls=calls,
        ),
        concurrency=1,
        clock=clock,
    )

    assert pool.run_once()
    first = repository.get(job_id)
    assert first is not None
    assert first["status"] == JobStatus.QUEUED.value
    assert first["execution_count_total"] == 1
    assert first["next_attempt_at"] == (clock.now() + timedelta(seconds=2)).isoformat()
    assert not pool.run_once()

    clock.advance(2)
    assert pool.run_once()
    second = repository.get(job_id)
    assert second is not None
    assert second["execution_count_total"] == 2
    assert second["next_attempt_at"] == (clock.now() + timedelta(seconds=10)).isoformat()
    assert not pool.run_once()

    clock.advance(10)
    assert pool.run_once()
    final = repository.get(job_id)
    assert final is not None
    assert final["status"] == JobStatus.FAILED.value
    assert final["execution_count_total"] == 3
    assert final["automatic_requeue_count_in_cycle"] == 2
    assert len(set(calls)) == 3


def test_non_retryable_error_fails_without_requeue(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    clock = FakeClock()
    job_id = _create_jobs(repository, 1, now=clock.now())[0]
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: ErrorPipeline(
            LVTError("MEDIA_INVALID", "invalid"),
            calls=[],
        ),
        concurrency=1,
        clock=clock,
    )

    assert pool.run_once()

    job = repository.get(job_id)
    assert job is not None
    assert job["status"] == JobStatus.FAILED.value
    assert job["execution_count_total"] == 1
    assert job["automatic_requeue_count_in_cycle"] == 0


def test_unstructured_error_message_cannot_enable_automatic_retry(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    clock = FakeClock()
    job_id = _create_jobs(repository, 1, now=clock.now())[0]
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: ErrorPipeline(
            RuntimeError("DOWNLOAD_FAILED appears only in text"),
            calls=[],
        ),
        concurrency=1,
        clock=clock,
    )

    assert pool.run_once()

    job = repository.get(job_id)
    assert job is not None
    assert job["status"] == JobStatus.FAILED.value
    assert job["error_code"] == ErrorCode.INTERNAL_ERROR.value
    assert job["automatic_requeue_count_in_cycle"] == 0


def test_internal_tool_retries_do_not_increment_job_execution_count(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    clock = FakeClock()
    job_id = _create_jobs(repository, 1, now=clock.now())[0]
    calls: list[str] = []
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: ErrorPipeline(
            LVTError("MEDIA_INVALID", "invalid"),
            calls=calls,
            internal_attempts=3,
        ),
        concurrency=1,
        clock=clock,
    )

    assert pool.run_once()

    job = repository.get(job_id)
    assert job is not None
    assert job["execution_count_total"] == 1
    assert len(calls) == 3
    assert len(set(calls)) == 1


@pytest.mark.parametrize("concurrency", [1, 2])
def test_worker_pool_never_exceeds_configured_concurrency(tmp_path: Path, concurrency: int) -> None:
    repository = _repository(tmp_path)
    job_ids = _create_jobs(repository, 3)
    active: set[str] = set()
    lock = threading.Lock()
    reached = threading.Event()
    release = threading.Event()
    pipeline = BlockingPipeline(
        active=active,
        lock=lock,
        reached=reached,
        release=release,
        target=concurrency,
    )
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=concurrency,
        poll_interval=60,
    )
    pool.start()
    try:
        pool.notify()
        assert reached.wait(timeout=2)
        with lock:
            assert len(active) == concurrency
        queued = [
            job_id
            for job_id in job_ids
            if repository.get(job_id)["status"] == JobStatus.QUEUED.value  # type: ignore[index]
        ]
        assert len(queued) == 3 - concurrency
    finally:
        release.set()
        pool.stop(graceful_timeout=2, cancel_timeout=2)

    assert pool.live_thread_count == 0


def test_two_workers_compete_but_only_one_executes_job(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    clock = FakeClock()
    _create_jobs(repository, 1, now=clock.now())
    calls: list[str] = []
    pipeline = ErrorPipeline(LVTError("MEDIA_INVALID", "invalid"), calls=calls)
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=2,
        clock=clock,
    )
    barrier = threading.Barrier(2)

    def compete() -> bool:
        return pool.run_once(before_claim=barrier.wait)

    first = threading.Thread(target=compete)
    second = threading.Thread(target=compete)
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(calls) == 1
    assert repository.list()[0]["execution_count_total"] == 1


def test_manual_retry_cycle_still_resets_only_cycle_budget(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    clock = FakeClock()
    job_id = _create_jobs(repository, 1, now=clock.now())[0]
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: ErrorPipeline(
            LVTError("MEDIA_INVALID", "invalid"),
            calls=[],
        ),
        concurrency=1,
        clock=clock,
    )
    assert pool.run_once()
    assert repository.manual_retry(job_id, JobStatus.FAILED, now=clock.now())
    retried = repository.get(job_id)
    assert retried is not None
    assert retried["retry_cycle"] == 1
    assert retried["automatic_requeue_count_in_cycle"] == 0
    assert retried["execution_count_total"] == 1


def test_shutdown_stops_claiming_and_cancels_active_worker_with_finite_wait(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    job_ids = _create_jobs(repository, 2)
    active: set[str] = set()
    lock = threading.Lock()
    reached = threading.Event()
    release = threading.Event()
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: BlockingPipeline(
            active=active,
            lock=lock,
            reached=reached,
            release=release,
            target=1,
        ),
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    pool.notify()
    assert reached.wait(timeout=2)

    pool.stop(graceful_timeout=0.01, cancel_timeout=2)

    assert pool.live_thread_count == 0
    queued_count = sum(
        repository.get(job_id)["status"] == JobStatus.QUEUED.value  # type: ignore[index]
        for job_id in job_ids
    )
    assert queued_count == 1


def test_http_submission_returns_before_background_processing_finishes(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    request_returned = threading.Event()
    thread_names: list[str] = []
    active: set[str] = set()
    lock = threading.Lock()

    def builder(_repository: JobRepository) -> BlockingPipeline:
        return BlockingPipeline(
            active=active,
            lock=lock,
            reached=started,
            release=release,
            target=1,
            thread_names=thread_names,
        )

    app = create_app(
        db_path=tmp_path / "lvt.sqlite3",
        api_token="test-token",
        pipeline_builder=builder,
        worker_concurrency=1,
        worker_poll_interval=60,
    )
    response_holder: list[Any] = []
    with TestClient(app) as client:

        def submit() -> None:
            response_holder.append(
                client.post(
                    "/api/v1/jobs",
                    headers={"X-LVT-Token": "test-token"},
                    json={"urls": ["https://example.test/async"]},
                )
            )
            request_returned.set()

        request = threading.Thread(target=submit)
        request.start()
        try:
            assert request_returned.wait(timeout=1)
            assert response_holder[0].status_code == 200
            assert started.wait(timeout=2)
            assert all(name.startswith("lvt-worker-") for name in thread_names)
        finally:
            release.set()
            request.join(timeout=2)
