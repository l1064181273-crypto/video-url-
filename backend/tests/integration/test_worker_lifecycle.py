import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.jobs import JobStatus
from lvt.core.processes import CancellationToken
from lvt.db.repository import JobRepository
from lvt.workers.runner import (
    JobWorkerPool,
    WorkerPipeline,
    WorkerStartupError,
)


class PassivePipeline:
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
        cancellation.raise_if_cancelled()


class BlockingResolverPipeline(PassivePipeline):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.entered = entered
        self.release = release

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        self.entered.set()
        assert self.release.wait(timeout=2)
        return JobStatus.DOWNLOADING


class FailingResolverPipeline(PassivePipeline):
    def __init__(self, entered: threading.Event | None = None) -> None:
        self.entered = entered

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        if self.entered is not None:
            self.entered.set()
        raise RuntimeError("resolver failed")


class CancellationObservedPipeline(PassivePipeline):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Callable[[JobStatus, int], None],
    ) -> None:
        self.started.set()
        assert cancellation.wait(timeout=2)
        self.cancelled.set()
        cancellation.raise_if_cancelled()


class RetirementPipeline(PassivePipeline):
    def __init__(
        self,
        entered: threading.Event,
        release: threading.Event,
        exited: threading.Event,
    ) -> None:
        self.entered = entered
        self.release = release
        self.exited = exited

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Callable[[JobStatus, int], None],
    ) -> None:
        self.entered.set()
        assert self.release.wait(timeout=5)
        self.exited.set()
        cancellation.raise_if_cancelled()


class BlockingClaimRepository(JobRepository):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.claim_entered = threading.Event()
        self.claim_release = threading.Event()

    def claim_next(self, **kwargs: Any) -> dict[str, Any] | None:
        self.claim_entered.set()
        assert self.claim_release.wait(timeout=2)
        return super().claim_next(**kwargs)


def _repository(path: Path) -> JobRepository:
    repository = JobRepository(path)
    repository.initialize()
    return repository


def _create_job(repository: JobRepository, suffix: str = "job") -> str:
    return str(repository.create(f"https://example.test/{suffix}")["uuid"])


def _stop_in_thread(
    pool: JobWorkerPool,
    errors: list[BaseException],
) -> threading.Thread:
    def stop() -> None:
        try:
            pool.stop(graceful_timeout=0, cancel_timeout=2)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=stop)
    thread.start()
    return thread


def test_stop_before_claim_admission_never_claims_repeatedly(tmp_path: Path) -> None:
    for iteration in range(20):
        repository = _repository(tmp_path / f"before-{iteration}.sqlite3")
        job_id = _create_job(repository, str(iteration))
        entered = threading.Event()
        release = threading.Event()
        pool = JobWorkerPool(
            repository=repository,
            pipeline_factory=lambda entered=entered, release=release: BlockingResolverPipeline(
                entered, release
            ),
            concurrency=1,
            poll_interval=60,
        )
        pool.start()
        pool.notify()
        assert entered.wait(timeout=2)
        errors: list[BaseException] = []
        stopper = _stop_in_thread(pool, errors)
        assert pool.wait_until_stopping(timeout=2)
        release.set()
        stopper.join(timeout=2)

        assert not stopper.is_alive()
        assert errors == []
        job = repository.get(job_id)
        assert job is not None
        assert job["status"] == JobStatus.QUEUED.value
        assert job["execution_count_total"] == 0
        assert pool.live_thread_count == 0


def test_stop_waits_for_claim_admission_then_cancels_registered_token_repeatedly(
    tmp_path: Path,
) -> None:
    for iteration in range(20):
        repository = BlockingClaimRepository(tmp_path / f"during-{iteration}.sqlite3")
        repository.initialize()
        job_id = _create_job(repository, str(iteration))
        pipeline = CancellationObservedPipeline()
        pool = JobWorkerPool(
            repository=repository,
            pipeline_factory=lambda pipeline=pipeline: pipeline,
            concurrency=1,
            poll_interval=60,
        )
        pool.start()
        pool.notify()
        assert repository.claim_entered.wait(timeout=2)
        errors: list[BaseException] = []
        stopper = _stop_in_thread(pool, errors)
        assert pool.wait_until_stopping(timeout=2)
        repository.claim_release.set()
        assert pipeline.started.wait(timeout=2)
        stopper.join(timeout=2)

        assert not stopper.is_alive()
        assert errors == []
        assert pipeline.cancelled.is_set()
        job = repository.get(job_id)
        assert job is not None
        assert job["execution_count_total"] == 1
        assert pool.live_thread_count == 0


@pytest.mark.parametrize("failure_call", [1, 2])
def test_factory_failure_aborts_start_without_threads(
    tmp_path: Path,
    failure_call: int,
) -> None:
    repository = _repository(tmp_path / f"factory-{failure_call}.sqlite3")
    calls = 0

    def factory() -> WorkerPipeline:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise RuntimeError(f"factory failure {failure_call}")
        return PassivePipeline()

    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=factory,
        concurrency=2,
    )

    with pytest.raises(WorkerStartupError, match="pipeline factory"):
        pool.start()

    assert calls == failure_call
    assert pool.live_thread_count == 0
    assert pool.fatal_errors
    pool.stop()
    pool.stop()


def test_lifespan_surfaces_factory_failure_without_worker_thread(tmp_path: Path) -> None:
    def builder(_repository: JobRepository) -> WorkerPipeline:
        raise RuntimeError("lifespan factory failure")

    app = create_app(
        db_path=tmp_path / "lifespan.sqlite3",
        api_token="token",
        pipeline_builder=builder,
    )

    with pytest.raises(WorkerStartupError), TestClient(app):
        pass

    assert app.state.worker_pool.live_thread_count == 0


def test_runtime_concurrency_decrease_retires_worker_after_active_run(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "runtime-concurrency.sqlite3")
    job_ids = [_create_job(repository, str(index)) for index in range(3)]
    first_entered = threading.Event()
    first_release = threading.Event()
    first_exited = threading.Event()
    second_entered = threading.Event()
    second_release = threading.Event()
    second_exited = threading.Event()
    pipelines: list[WorkerPipeline] = [
        RetirementPipeline(first_entered, first_release, first_exited),
        RetirementPipeline(second_entered, second_release, second_exited),
    ]
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipelines.pop(0),
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    pool.notify()
    assert first_entered.wait(timeout=2)

    pool.update_concurrency(2)
    pool.notify()
    assert second_entered.wait(timeout=2)
    assert repository.get_worker_concurrency() == 2
    assert pool.live_thread_count == 2

    pool.update_concurrency(1)
    assert repository.get_worker_concurrency() == 1
    assert not first_exited.is_set()
    assert not second_exited.is_set()
    retiring_thread = pool._threads[1]

    pool.update_concurrency(2)
    assert pool._threads[1] is retiring_thread
    assert pool.live_thread_count == 2
    pool.update_concurrency(1)

    second_release.set()
    assert second_exited.wait(timeout=2)
    pool._threads[1].join(timeout=2)
    assert not pool._threads[1].is_alive()
    assert pool.live_thread_count == 1
    third = repository.get(job_ids[2])
    assert third is not None
    assert third["status"] == JobStatus.QUEUED.value
    assert third["execution_count_total"] == 0

    first_release.set()
    assert first_exited.wait(timeout=2)
    pool.stop()
    assert pool.live_thread_count == 0


def test_runtime_concurrency_increase_failure_rolls_back_setting_and_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "runtime-increase-failure.sqlite3")
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=PassivePipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    original_start = threading.Thread.start

    def fail_second_worker(thread: threading.Thread) -> None:
        if thread.name == "lvt-worker-2":
            raise RuntimeError("injected runtime thread failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second_worker)
    with pytest.raises(WorkerStartupError, match="thread failed to start"):
        pool.update_concurrency(2)

    assert pool.concurrency == 1
    assert repository.get_worker_concurrency() == 1
    assert len(pool._threads) == 1
    assert pool.live_thread_count == 1
    pool.stop()


@pytest.mark.parametrize("failure_point", ["resolver", "peek", "claim"])
def test_worker_loop_records_fatal_errors_without_hot_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    repository = _repository(tmp_path / f"{failure_point}.sqlite3")
    _create_job(repository)
    calls = 0

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"{failure_point} failed")

    if failure_point == "peek":
        monkeypatch.setattr(repository, "peek_next_queued", fail)
        pipeline: WorkerPipeline = PassivePipeline()
    elif failure_point == "claim":
        monkeypatch.setattr(repository, "claim_next", fail)
        pipeline = PassivePipeline()
    else:
        pipeline = FailingResolverPipeline()
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )

    pool.start()
    assert pool.wait_for_fatal(timeout=2)

    health = pool.health_snapshot()
    assert health["status"] == "unhealthy"
    assert health["fatal_count"] == 1
    assert calls <= 1
    pool.stop()
    pool.stop()
    assert pool.live_thread_count == 0


def test_health_is_unhealthy_when_one_of_two_workers_dies(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "degraded.sqlite3")
    _create_job(repository)
    failed = threading.Event()
    blocked = threading.Event()
    release = threading.Event()
    pipelines: list[WorkerPipeline] = [
        FailingResolverPipeline(failed),
        BlockingResolverPipeline(blocked, release),
    ]

    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipelines.pop(0),
        concurrency=2,
        poll_interval=60,
    )
    pool.start()
    assert failed.wait(timeout=2)
    assert blocked.wait(timeout=2)
    assert pool.wait_for_fatal(timeout=2)

    health = pool.health_snapshot()
    assert health["status"] == "unhealthy"
    assert health["configured_workers"] == 2
    assert health["fatal_count"] == 1
    assert health["live_workers"] == 1

    errors: list[BaseException] = []
    stopper = _stop_in_thread(pool, errors)
    assert pool.wait_until_stopping(timeout=2)
    release.set()
    stopper.join(timeout=2)
    assert errors == []
    assert pool.live_thread_count == 0


def test_health_endpoint_reports_fatal_worker(tmp_path: Path) -> None:
    entered = threading.Event()

    def builder(_repository: JobRepository) -> WorkerPipeline:
        return FailingResolverPipeline(entered)

    app = create_app(
        db_path=tmp_path / "health.sqlite3",
        api_token="token",
        pipeline_builder=builder,
        worker_poll_interval=60,
    )
    with TestClient(app) as client:
        healthy = client.get("/health")
        assert healthy.status_code == 200
        response = client.post(
            "/api/v1/jobs",
            headers={"X-LVT-Token": "token"},
            json={"urls": ["https://example.test/fatal"]},
        )
        assert response.status_code == 200
        assert entered.wait(timeout=2)
        assert app.state.worker_pool.wait_for_fatal(timeout=2)

        unhealthy = client.get("/health")
        assert unhealthy.status_code == 503
        assert unhealthy.json()["status"] == "unhealthy"
        assert unhealthy.json()["worker"]["fatal_count"] == 1
