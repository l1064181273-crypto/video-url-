from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from lvt.core.errors import LVTError
from lvt.core.jobs import (
    ACTIVE_JOB_STATUSES,
    ErrorCode,
    JobStatus,
    classify_error_code,
)
from lvt.core.processes import CancellationToken, ProcessCancelledError
from lvt.db.repository import AutomaticRequeueResult, JobRepository
from lvt.workers.progress import ProgressReporter, StaleWorkerProgressError

JOB_BACKOFF_SECONDS = (2, 10)


class Clock(Protocol):
    def now(self) -> datetime: ...


class WorkerPipeline(Protocol):
    def resolve_first_required_stage(self, job_id: str) -> JobStatus: ...

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Callable[[JobStatus, int], None],
    ) -> object: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class WorkerShutdownError(RuntimeError):
    pass


class WorkerStartupError(RuntimeError):
    pass


class CancelRequestResult(StrEnum):
    CANCELLED = "cancelled"
    CANCELLING = "cancelling"
    ALREADY_CANCELLING = "already_cancelling"
    ALREADY_CANCELLED = "already_cancelled"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    STALE = "stale"


@dataclass(frozen=True)
class WorkerFatalError:
    worker_index: int | None
    phase: str
    error_type: str
    message: str


class JobCancellationToken(CancellationToken):
    def __init__(self, repository: JobRepository, job_id: str, run_id: str) -> None:
        super().__init__()
        self.repository = repository
        self.job_id = job_id
        self.run_id = run_id

    @property
    def cancelled(self) -> bool:
        if super().cancelled:
            return True
        current = self.repository.get(self.job_id)
        if current is None:
            return False
        status = JobStatus(str(current["status"]))
        active_run_id = current["active_run_id"]
        requested_cancel = status is JobStatus.CANCELLING and active_run_id == self.run_id
        lost_ownership = (
            status in {*ACTIVE_JOB_STATUSES, JobStatus.QUEUED, JobStatus.CANCELLING}
            and active_run_id != self.run_id
        )
        if requested_cancel or lost_ownership:
            self.cancel()
        return super().cancelled


class JobWorkerPool:
    def __init__(
        self,
        *,
        repository: JobRepository,
        pipeline_factory: Callable[[], WorkerPipeline],
        concurrency: int = 1,
        clock: Clock | None = None,
        poll_interval: float = 0.25,
        worker_park_hook: Callable[[int], None] | None = None,
    ) -> None:
        if type(concurrency) is not int or concurrency not in {1, 2}:
            raise ValueError("worker concurrency must be 1 or 2")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.repository = repository
        self.pipeline_factory = pipeline_factory
        self.concurrency = concurrency
        self.clock = clock or SystemClock()
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pipelines: list[WorkerPipeline] = []
        self._active_tokens: dict[tuple[str, str], JobCancellationToken] = {}
        self._shutdown_cleaned_runs: set[tuple[str, str]] = set()
        self._admission_lock = threading.Lock()
        self._capacity_condition = threading.Condition(self._admission_lock)
        self._parked_workers: set[int] = set()
        self._worker_park_hook = worker_park_hook
        self._state_lock = threading.Lock()
        self._fatal_errors: list[WorkerFatalError] = []
        self._fatal_event = threading.Event()
        self._started = False

    @property
    def live_thread_count(self) -> int:
        return sum(thread.is_alive() for thread in self._threads)

    @property
    def fatal_errors(self) -> tuple[WorkerFatalError, ...]:
        with self._state_lock:
            return tuple(self._fatal_errors)

    def wait_for_fatal(self, timeout: float | None = None) -> bool:
        return self._fatal_event.wait(timeout)

    def wait_until_stopping(self, timeout: float | None = None) -> bool:
        return self._stop.wait(timeout)

    def health_snapshot(self) -> dict[str, int | str]:
        fatal_count = len(self.fatal_errors)
        live_workers = sum(thread.is_alive() for thread in self._threads[: self.concurrency])
        healthy = (
            self._started
            and not self._stop.is_set()
            and fatal_count == 0
            and live_workers == self.concurrency
        )
        return {
            "status": "healthy" if healthy else "unhealthy",
            "configured_workers": self.concurrency,
            "live_workers": live_workers,
            "fatal_count": fatal_count,
        }

    def start(self) -> None:
        if self._started:
            return
        self._stop.clear()
        self._shutdown_cleaned_runs.clear()
        self._parked_workers.clear()
        self._fatal_event.clear()
        with self._state_lock:
            self._fatal_errors.clear()
        pipelines: list[WorkerPipeline] = []
        try:
            for _ in range(self.concurrency):
                pipelines.append(self.pipeline_factory())
        except BaseException as exc:
            self._stop.set()
            self._record_fatal(None, "pipeline_factory", exc)
            raise WorkerStartupError("worker pipeline factory failed") from exc

        self.repository.set_worker_concurrency(self.concurrency)
        self._pipelines = pipelines
        self._threads = [
            threading.Thread(
                target=self._worker_main,
                args=(index, pipelines[index]),
                name=f"lvt-worker-{index + 1}",
                daemon=False,
            )
            for index in range(self.concurrency)
        ]
        self._started = True
        started_threads: list[threading.Thread] = []
        try:
            for thread in self._threads:
                thread.start()
                started_threads.append(thread)
        except BaseException as exc:
            self._stop.set()
            self._wake.set()
            self._threads = started_threads
            self._join_until(time.monotonic() + 5)
            self._started = False
            self._record_fatal(None, "thread_start", exc)
            raise WorkerStartupError("worker thread failed to start") from exc

    def notify(self) -> None:
        self._wake.set()

    def set_initial_concurrency(self, concurrency: int) -> None:
        if type(concurrency) is not int or concurrency not in {1, 2}:
            raise ValueError("worker concurrency must be 1 or 2")
        if self._started or self.live_thread_count:
            raise RuntimeError("initial concurrency can only be set before worker start")
        self.concurrency = concurrency

    def update_concurrency(self, concurrency: int) -> None:
        if type(concurrency) is not int or concurrency not in {1, 2}:
            raise ValueError("worker concurrency must be 1 or 2")

        with self._capacity_condition:
            if not self._started or self._stop.is_set():
                raise WorkerStartupError("worker pool is not running")
            previous = self.concurrency
            required_threads_alive = all(
                index < len(self._threads) and self._threads[index].is_alive()
                for index in range(concurrency)
            )
            if concurrency == previous and self.fatal_errors:
                raise WorkerStartupError("configured worker capacity is unhealthy")
            if concurrency == previous and required_threads_alive:
                self.repository.set_worker_concurrency(concurrency)
                self._capacity_condition.notify_all()
                self._wake.set()
                return
            if concurrency < previous:
                self.repository.set_worker_concurrency(concurrency)
                self.concurrency = concurrency
                self._capacity_condition.notify_all()
                self._wake.set()
                return

            worker_index = concurrency - 1
            if worker_index < len(self._threads) and self._threads[worker_index].is_alive():
                self.repository.set_worker_concurrency(concurrency)
                self.concurrency = concurrency
                self._wake.set()
                return
            try:
                pipeline = self.pipeline_factory()
            except BaseException as exc:
                raise WorkerStartupError("worker pipeline factory failed") from exc
            thread = threading.Thread(
                target=self._worker_main,
                args=(worker_index, pipeline),
                name=f"lvt-worker-{concurrency}",
                daemon=False,
            )
            previous_pipeline: WorkerPipeline | None = None
            previous_thread: threading.Thread | None = None
            if worker_index < len(self._pipelines):
                previous_pipeline = self._pipelines[worker_index]
                previous_thread = self._threads[worker_index]
                self._pipelines[worker_index] = pipeline
                self._threads[worker_index] = thread
            else:
                self._pipelines.append(pipeline)
                self._threads.append(thread)
            self.repository.set_worker_concurrency(concurrency)
            self.concurrency = concurrency
            try:
                thread.start()
            except BaseException as exc:
                self.concurrency = previous
                self.repository.set_worker_concurrency(previous)
                if previous_pipeline is None or previous_thread is None:
                    self._pipelines.pop()
                    self._threads.pop()
                else:
                    self._pipelines[worker_index] = previous_pipeline
                    self._threads[worker_index] = previous_thread
                raise WorkerStartupError("worker thread failed to start") from exc
            self._capacity_condition.notify_all()
            self._wake.set()

    def request_cancel(self, job_id: str) -> CancelRequestResult:
        with self._admission_lock:
            current = self.repository.get(job_id)
            if current is None:
                return CancelRequestResult.NOT_FOUND
            status = JobStatus(str(current["status"]))
            run_id = str(current["active_run_id"]) if current["active_run_id"] is not None else None
            if status is JobStatus.QUEUED:
                if self.repository.request_cancel(job_id, JobStatus.QUEUED):
                    return CancelRequestResult.CANCELLED
                return self._cancel_result_after_race(job_id)
            if status in ACTIVE_JOB_STATUSES:
                if not self.repository.request_cancel(job_id, status):
                    return self._cancel_result_after_race(job_id)
                if run_id is not None:
                    token_key = (job_id, run_id)
                    token = self._active_tokens.get(token_key)
                    if token is not None:
                        token.cancel()
                    elif (
                        token_key in self._shutdown_cleaned_runs
                        and self.repository.mark_cancelled(
                            job_id,
                            run_id,
                            JobStatus.CANCELLING,
                            now=self.clock.now(),
                        )
                    ):
                        self._shutdown_cleaned_runs.discard(token_key)
                        return CancelRequestResult.CANCELLED
                return CancelRequestResult.CANCELLING
            if status is JobStatus.CANCELLING:
                if run_id is not None:
                    token_key = (job_id, run_id)
                    token = self._active_tokens.get(token_key)
                    if token is not None:
                        token.cancel()
                    elif (
                        token_key in self._shutdown_cleaned_runs
                        and self.repository.mark_cancelled(
                            job_id,
                            run_id,
                            JobStatus.CANCELLING,
                            now=self.clock.now(),
                        )
                    ):
                        self._shutdown_cleaned_runs.discard(token_key)
                        return CancelRequestResult.CANCELLED
                return CancelRequestResult.ALREADY_CANCELLING
            if status is JobStatus.CANCELLED:
                return CancelRequestResult.ALREADY_CANCELLED
            return CancelRequestResult.CONFLICT

    def stop(
        self,
        *,
        graceful_timeout: float = 5.0,
        cancel_timeout: float = 5.0,
    ) -> None:
        if graceful_timeout < 0 or cancel_timeout <= 0:
            raise ValueError("worker shutdown timeouts are invalid")
        graceful_deadline = time.monotonic() + graceful_timeout
        self._stop.set()
        self._wake.set()
        # Admission is a barrier: any claim already inside must register its token
        # before shutdown can proceed, while later callers observe _stop and exit.
        with self._capacity_condition:
            self._capacity_condition.notify_all()
        self._join_until(graceful_deadline)
        if self.live_thread_count:
            with self._admission_lock:
                tokens = list(self._active_tokens.values())
            for token in tokens:
                token.cancel()
            self._join_until(time.monotonic() + cancel_timeout)
        if self.live_thread_count:
            raise WorkerShutdownError(
                f"{self.live_thread_count} worker thread(s) did not stop in time"
            )
        self._threads = []
        self._pipelines = []
        self._parked_workers.clear()
        self._started = False

    def run_once(
        self,
        pipeline: WorkerPipeline | None = None,
        *,
        before_claim: Callable[[], object] | None = None,
        worker_index: int | None = None,
    ) -> bool:
        if self._stop.is_set():
            return False
        now = self.clock.now()
        candidate = self.repository.peek_next_queued(now=now)
        if candidate is None:
            return False
        selected_pipeline = pipeline or self.pipeline_factory()
        job_id = str(candidate["uuid"])
        first_stage = selected_pipeline.resolve_first_required_stage(job_id)
        if before_claim is not None:
            before_claim()
        with self._admission_lock:
            if self._stop.is_set() or (
                worker_index is not None and worker_index >= self.concurrency
            ):
                return False
            claimed = self.repository.claim_next(
                expected_job_id=job_id,
                first_required_stage=first_stage,
                now=now,
            )
            if claimed is None:
                return False
            run_id = str(claimed["active_run_id"])
            token = JobCancellationToken(self.repository, job_id, run_id)
            token_key = (job_id, run_id)
            self._active_tokens[token_key] = token
        try:
            reporter = ProgressReporter(
                self.repository,
                job_id,
                run_id,
                high_water=int(claimed["overall_progress"]),
            )
            selected_pipeline.run_claimed(
                job_id=job_id,
                run_id=run_id,
                cancellation=token,
                progress_callback=reporter,
            )
            token.raise_if_cancelled()
        except ProcessCancelledError:
            if not self._converge_cancelled(job_id, run_id):
                if self._stop.is_set():
                    with self._admission_lock:
                        self._shutdown_cleaned_runs.add((job_id, run_id))
                else:
                    self._record_failure(
                        job_id,
                        run_id,
                        ErrorCode.INTERNAL_ERROR,
                        "任务执行被意外中断",
                    )
        except StaleWorkerProgressError:
            self._converge_cancelled(job_id, run_id)
        except Exception as exc:
            if not self._converge_cancelled(job_id, run_id):
                self._handle_exception(job_id, run_id, exc)
        finally:
            with self._admission_lock:
                if token_key in self._shutdown_cleaned_runs and self._converge_cancelled(
                    job_id, run_id
                ):
                    self._shutdown_cleaned_runs.discard(token_key)
                self._active_tokens.pop(token_key, None)
        return True

    def _worker_main(self, worker_index: int, pipeline: WorkerPipeline) -> None:
        try:
            while not self._stop.is_set():
                park_hook: Callable[[int], None] | None = None
                with self._capacity_condition:
                    if worker_index >= self.concurrency and not self._stop.is_set():
                        self._parked_workers.add(worker_index)
                        park_hook = self._worker_park_hook
                if park_hook is not None:
                    park_hook(worker_index)
                with self._capacity_condition:
                    while worker_index >= self.concurrency and not self._stop.is_set():
                        self._capacity_condition.wait()
                    self._parked_workers.discard(worker_index)
                if self._stop.is_set():
                    return
                if self.run_once(pipeline, worker_index=worker_index):
                    continue
                self._wake.wait(self.poll_interval)
                self._wake.clear()
        except BaseException as exc:
            self._record_fatal(worker_index, "worker_loop", exc)

    def _handle_exception(self, job_id: str, run_id: str, error: Exception) -> None:
        if isinstance(error, LVTError):
            classified = classify_error_code(error.code)
            message = error.user_message
        elif hasattr(error, "code"):
            classified = classify_error_code(str(error.code))
            message = classified.policy.user_advice
        else:
            classified = classify_error_code(ErrorCode.INTERNAL_ERROR)
            message = classified.policy.user_advice
        current = self.repository.get(job_id)
        if (
            current is None
            or current["active_run_id"] != run_id
            or current["status"] not in {status.value for status in ACTIVE_JOB_STATUSES}
        ):
            return
        expected_status = JobStatus(str(current["status"]))
        if classified.policy.auto_requeue:
            retry_index = int(current["automatic_requeue_count_in_cycle"])
            delay = JOB_BACKOFF_SECONDS[min(retry_index, len(JOB_BACKOFF_SECONDS) - 1)]
            result = self.repository.automatic_requeue(
                job_id=job_id,
                run_id=run_id,
                expected_status=expected_status,
                error_code=classified.code,
                error_message=message,
                next_attempt_at=self.clock.now() + timedelta(seconds=delay),
                now=self.clock.now(),
            )
            if result is not AutomaticRequeueResult.STALE:
                return
        self._record_failure(
            job_id,
            run_id,
            classified.code,
            message,
            expected_status=expected_status,
        )

    def _record_failure(
        self,
        job_id: str,
        run_id: str,
        error_code: ErrorCode,
        message: str,
        *,
        expected_status: JobStatus | None = None,
    ) -> None:
        status = expected_status
        if status is None:
            current = self.repository.get(job_id)
            if (
                current is None
                or current["active_run_id"] != run_id
                or current["status"] not in {candidate.value for candidate in ACTIVE_JOB_STATUSES}
            ):
                return
            status = JobStatus(str(current["status"]))
        self.repository.fail_job(
            job_id,
            run_id,
            status,
            error_code,
            message,
            now=self.clock.now(),
        )

    def _converge_cancelled(self, job_id: str, run_id: str) -> bool:
        current = self.repository.get(job_id)
        if (
            current is None
            or current["status"] != JobStatus.CANCELLING.value
            or current["active_run_id"] != run_id
        ):
            return False
        return self.repository.mark_cancelled(
            job_id,
            run_id,
            JobStatus.CANCELLING,
            now=self.clock.now(),
        )

    def _cancel_result_after_race(self, job_id: str) -> CancelRequestResult:
        current = self.repository.get(job_id)
        if current is None:
            return CancelRequestResult.NOT_FOUND
        status = JobStatus(str(current["status"]))
        if status is JobStatus.CANCELLED:
            return CancelRequestResult.ALREADY_CANCELLED
        if status is JobStatus.CANCELLING:
            return CancelRequestResult.ALREADY_CANCELLING
        if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
            return CancelRequestResult.CONFLICT
        return CancelRequestResult.STALE

    def _join_until(self, deadline: float) -> None:
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(remaining)

    def _record_fatal(
        self,
        worker_index: int | None,
        phase: str,
        error: BaseException,
    ) -> None:
        fatal = WorkerFatalError(
            worker_index=worker_index,
            phase=phase,
            error_type=type(error).__name__,
            message=str(error),
        )
        with self._state_lock:
            self._fatal_errors.append(fatal)
        self._fatal_event.set()
