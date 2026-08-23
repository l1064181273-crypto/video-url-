import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from lvt.core.jobs import ACTIVE_JOB_STATUSES, ErrorCode, JobStatus
from lvt.core.processes import CancellationToken, SubprocessExecutor
from lvt.db.repository import JobRepository
from lvt.workers.runner import CancelRequestResult, JobWorkerPool


class WaitingPipeline:
    def __init__(self, status: JobStatus) -> None:
        self.status = status
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return self.status

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


class UninterruptiblePipeline:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return JobStatus.TRANSCRIBING

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Callable[[JobStatus, int], None],
    ) -> None:
        self.started.set()
        assert self.release.wait(timeout=2)


class ProgressRacePipeline:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

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
        self.started.set()
        assert self.release.wait(timeout=2)
        progress_callback(JobStatus.DOWNLOADING, 50)
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


class ProcessTreePipeline:
    def __init__(self, leader_script: Path, child_script: Path, pid_file: Path) -> None:
        self.leader_script = leader_script
        self.child_script = child_script
        self.pid_file = pid_file
        self.started = threading.Event()

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
        self.started.set()
        SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1).run(
            [
                sys.executable,
                str(self.leader_script),
                str(self.child_script),
                str(self.pid_file),
            ],
            timeout=30,
            cancellation=cancellation,
        )


def _repository(path: Path) -> JobRepository:
    repository = JobRepository(path)
    repository.initialize()
    return repository


def _create_job(repository: JobRepository, suffix: str = "job") -> str:
    return str(repository.create(f"https://example.test/{suffix}")["uuid"])


def _wait_status(
    repository: JobRepository,
    job_id: str,
    expected: JobStatus,
    *,
    timeout: float = 2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = repository.get(job_id)
        if job is not None and job["status"] == expected.value:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {expected.value}")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_queued_cancel_and_claim_race_has_one_winner_twenty_times(
    tmp_path: Path,
) -> None:
    for iteration in range(20):
        repository = _repository(tmp_path / f"queued-race-{iteration}.sqlite3")
        job_id = _create_job(repository, str(iteration))
        pipeline = WaitingPipeline(JobStatus.DOWNLOADING)
        pool = JobWorkerPool(
            repository=repository,
            pipeline_factory=lambda pipeline=pipeline: pipeline,
            concurrency=1,
        )
        barrier = threading.Barrier(2)
        run_results: list[bool] = []
        cancel_results: list[CancelRequestResult] = []

        def run(
            pool: JobWorkerPool = pool,
            pipeline: WaitingPipeline = pipeline,
            barrier: threading.Barrier = barrier,
            run_results: list[bool] = run_results,
        ) -> None:
            run_results.append(pool.run_once(pipeline, before_claim=barrier.wait))

        def cancel(
            pool: JobWorkerPool = pool,
            barrier: threading.Barrier = barrier,
            job_id: str = job_id,
            cancel_results: list[CancelRequestResult] = cancel_results,
        ) -> None:
            barrier.wait()
            cancel_results.append(pool.request_cancel(job_id))

        runner = threading.Thread(target=run)
        canceller = threading.Thread(target=cancel)
        canceller.start()
        runner.start()
        runner.join(timeout=2)
        canceller.join(timeout=2)

        assert not runner.is_alive()
        assert not canceller.is_alive()
        assert len(run_results) == len(cancel_results) == 1
        cancelled = repository.get(job_id)
        assert cancelled is not None
        assert cancelled["status"] == JobStatus.CANCELLED.value
        assert cancelled["active_run_id"] is None
        assert cancelled["error_code"] == ErrorCode.CANCELLED_BY_USER.value
        assert cancelled["execution_count_total"] in {0, 1}


def test_queued_cancel_is_idempotent_and_never_creates_run_id(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "queued-idempotent.sqlite3")
    job_id = _create_job(repository)
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: WaitingPipeline(JobStatus.DOWNLOADING),
        concurrency=1,
    )

    assert pool.request_cancel(job_id) is CancelRequestResult.CANCELLED
    assert pool.request_cancel(job_id) is CancelRequestResult.ALREADY_CANCELLED

    cancelled = repository.get(job_id)
    assert cancelled is not None
    assert cancelled["active_run_id"] is None
    assert cancelled["execution_count_total"] == 0
    assert repository.peek_next_queued() is None


@pytest.mark.parametrize("status", sorted(ACTIVE_JOB_STATUSES, key=lambda item: item.value))
def test_running_cancel_converges_from_every_active_status(
    tmp_path: Path,
    status: JobStatus,
) -> None:
    repository = _repository(tmp_path / f"{status.value}.sqlite3")
    job_id = _create_job(repository, status.value)
    pipeline = WaitingPipeline(status)
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    try:
        pool.notify()
        assert pipeline.started.wait(timeout=2)
        assert pool.request_cancel(job_id) is CancelRequestResult.CANCELLING
        cancelled = _wait_status(repository, job_id, JobStatus.CANCELLED)
        assert pipeline.cancelled.is_set()
        assert cancelled["active_run_id"] is None
        assert cancelled["error_code"] == ErrorCode.CANCELLED_BY_USER.value
        assert pool.request_cancel(job_id) is CancelRequestResult.ALREADY_CANCELLED
    finally:
        pool.stop(graceful_timeout=2, cancel_timeout=2)


def test_cancel_waits_for_claim_token_registration(tmp_path: Path) -> None:
    repository = BlockingClaimRepository(tmp_path / "claim-token.sqlite3")
    repository.initialize()
    job_id = _create_job(repository)
    pipeline = WaitingPipeline(JobStatus.DOWNLOADING)
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    pool.notify()
    assert repository.claim_entered.wait(timeout=2)
    results: list[CancelRequestResult] = []
    canceller = threading.Thread(target=lambda: results.append(pool.request_cancel(job_id)))
    canceller.start()
    repository.claim_release.set()
    assert pipeline.started.wait(timeout=2)
    canceller.join(timeout=2)

    assert results == [CancelRequestResult.CANCELLING]
    assert pipeline.cancelled.wait(timeout=2)
    _wait_status(repository, job_id, JobStatus.CANCELLED)
    pool.stop(graceful_timeout=2, cancel_timeout=2)


def test_cancel_and_progress_callback_converge_to_cancelled(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "progress.sqlite3")
    job_id = _create_job(repository)
    pipeline = ProgressRacePipeline()
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    try:
        pool.notify()
        assert pipeline.started.wait(timeout=2)
        assert pool.request_cancel(job_id) is CancelRequestResult.CANCELLING
        pipeline.release.set()
        _wait_status(repository, job_id, JobStatus.CANCELLED)
    finally:
        pool.stop(graceful_timeout=2, cancel_timeout=2)


def test_uninterruptible_call_converges_after_return_boundary(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "in-process.sqlite3")
    job_id = _create_job(repository)
    pipeline = UninterruptiblePipeline()
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    try:
        pool.notify()
        assert pipeline.started.wait(timeout=2)
        assert JobRepository(repository.db_path).request_cancel(
            job_id,
            JobStatus.TRANSCRIBING,
        )
        assert repository.get(job_id)["status"] == JobStatus.CANCELLING.value  # type: ignore[index]
        pipeline.release.set()
        _wait_status(repository, job_id, JobStatus.CANCELLED)
    finally:
        pool.stop(graceful_timeout=2, cancel_timeout=2)


@pytest.mark.parametrize("iteration", range(20))
def test_stop_and_user_cancel_converge_to_cancelled(
    tmp_path: Path,
    iteration: int,
) -> None:
    repository = _repository(tmp_path / f"stop-cancel-{iteration}.sqlite3")
    job_id = _create_job(repository)
    pipeline = WaitingPipeline(JobStatus.DOWNLOADING)
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    pool.notify()
    assert pipeline.started.wait(timeout=2)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def stop() -> None:
        barrier.wait()
        try:
            pool.stop(graceful_timeout=0, cancel_timeout=2)
        except BaseException as exc:
            errors.append(exc)

    def cancel() -> None:
        barrier.wait()
        pool.request_cancel(job_id)

    stopper = threading.Thread(target=stop)
    canceller = threading.Thread(target=cancel)
    stopper.start()
    canceller.start()
    stopper.join(timeout=2)
    canceller.join(timeout=2)

    assert errors == []
    cancelled = repository.get(job_id)
    assert cancelled is not None
    assert cancelled["status"] == JobStatus.CANCELLED.value
    assert cancelled["error_code"] == ErrorCode.CANCELLED_BY_USER.value
    assert pool.live_thread_count == 0


def test_external_process_tree_is_gone_before_cancelled(tmp_path: Path) -> None:
    child_script = tmp_path / "child.py"
    child_script.write_text(
        """
import os, subprocess, sys, time
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
open(sys.argv[1], "w").write(f"{os.getpid()} {grandchild.pid}")
while True:
    time.sleep(0.01)
""",
        encoding="utf-8",
    )
    leader_script = tmp_path / "leader.py"
    leader_script.write_text(
        """
import subprocess, sys, time
subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
while True:
    time.sleep(0.01)
""",
        encoding="utf-8",
    )
    pid_file = tmp_path / "pids.txt"
    repository = _repository(tmp_path / "process.sqlite3")
    job_id = _create_job(repository)
    pipeline = ProcessTreePipeline(leader_script, child_script, pid_file)
    pool = JobWorkerPool(
        repository=repository,
        pipeline_factory=lambda: pipeline,
        concurrency=1,
        poll_interval=60,
    )
    pool.start()
    try:
        pool.notify()
        assert pipeline.started.wait(timeout=2)
        deadline = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid, grandchild_pid = [
            int(value) for value in pid_file.read_text(encoding="utf-8").split()
        ]
        assert pool.request_cancel(job_id) is CancelRequestResult.CANCELLING
        _wait_status(repository, job_id, JobStatus.CANCELLED)
        assert not _process_exists(child_pid)
        assert not _process_exists(grandchild_pid)
    finally:
        if pid_file.exists():
            for pid in [int(value) for value in pid_file.read_text(encoding="utf-8").split()]:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
        pool.stop(graceful_timeout=2, cancel_timeout=2)
