from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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


class JobWorkerPool:
    def __init__(
        self,
        *,
        repository: JobRepository,
        pipeline_factory: Callable[[], WorkerPipeline],
        concurrency: int = 1,
        clock: Clock | None = None,
        poll_interval: float = 0.25,
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
        self._active_tokens: dict[int, CancellationToken] = {}
        self._active_lock = threading.Lock()

    @property
    def live_thread_count(self) -> int:
        return sum(thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        if self.live_thread_count:
            return
        self.repository.set_worker_concurrency(self.concurrency)
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._worker_main,
                args=(index,),
                name=f"lvt-worker-{index + 1}",
                daemon=False,
            )
            for index in range(self.concurrency)
        ]
        for thread in self._threads:
            thread.start()

    def notify(self) -> None:
        self._wake.set()

    def stop(
        self,
        *,
        graceful_timeout: float = 5.0,
        cancel_timeout: float = 5.0,
    ) -> None:
        if graceful_timeout < 0 or cancel_timeout <= 0:
            raise ValueError("worker shutdown timeouts are invalid")
        self._stop.set()
        self._wake.set()
        self._join_until(time.monotonic() + graceful_timeout)
        if self.live_thread_count:
            with self._active_lock:
                tokens = list(self._active_tokens.values())
            for token in tokens:
                token.cancel()
            self._join_until(time.monotonic() + cancel_timeout)
        if self.live_thread_count:
            raise WorkerShutdownError(
                f"{self.live_thread_count} worker thread(s) did not stop in time"
            )
        self._threads = []

    def run_once(
        self,
        pipeline: WorkerPipeline | None = None,
        *,
        before_claim: Callable[[], object] | None = None,
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
        if self._stop.is_set():
            return False
        claimed = self.repository.claim_next(
            expected_job_id=job_id,
            first_required_stage=first_stage,
            now=now,
        )
        if claimed is None:
            return False

        run_id = str(claimed["active_run_id"])
        token = CancellationToken()
        worker_key = threading.get_ident()
        with self._active_lock:
            self._active_tokens[worker_key] = token
        reporter = ProgressReporter(
            self.repository,
            job_id,
            run_id,
            high_water=int(claimed["overall_progress"]),
        )
        try:
            selected_pipeline.run_claimed(
                job_id=job_id,
                run_id=run_id,
                cancellation=token,
                progress_callback=reporter,
            )
        except ProcessCancelledError:
            if not self._stop.is_set():
                self._record_failure(
                    job_id,
                    run_id,
                    ErrorCode.INTERNAL_ERROR,
                    "任务执行被意外中断",
                )
        except StaleWorkerProgressError:
            pass
        except Exception as exc:
            self._handle_exception(job_id, run_id, exc)
        finally:
            with self._active_lock:
                self._active_tokens.pop(worker_key, None)
        return True

    def _worker_main(self, _worker_index: int) -> None:
        pipeline = self.pipeline_factory()
        while not self._stop.is_set():
            if self.run_once(pipeline):
                continue
            self._wake.wait(self.poll_interval)
            self._wake.clear()

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

    def _join_until(self, deadline: float) -> None:
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            thread.join(remaining)
