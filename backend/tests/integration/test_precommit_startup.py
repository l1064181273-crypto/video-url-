from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.jobs import JobStatus
from lvt.core.processes import CancellationToken
from lvt.db.repository import JobRepository


class RecordingPipeline:
    def __init__(self, executed: threading.Event | None = None) -> None:
        self.executed = executed

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
        if self.executed is not None:
            self.executed.set()
        cancellation.raise_if_cancelled()


def _queued_database(path: Path) -> str:
    repository = JobRepository(path)
    repository.initialize()
    return str(repository.create("https://example.test/precommit")["uuid"])


def _job(path: Path, job_id: str) -> dict[str, object]:
    job = JobRepository(path).get(job_id)
    assert job is not None
    return job


def test_precommit_initializes_pipeline_and_threads_but_claims_only_after_activate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "precommit.sqlite3"
    job_id = _queued_database(database)
    activation_read, activation_write = os.pipe()
    pipeline_created = threading.Event()
    executed = threading.Event()

    def builder(_repository: JobRepository) -> RecordingPipeline:
        pipeline_created.set()
        return RecordingPipeline(executed)

    app = create_app(
        db_path=database,
        api_token="token",
        pipeline_builder=builder,
        worker_poll_interval=60,
        precommit_activation_fd=activation_read,
    )
    try:
        with TestClient(app) as client:
            assert pipeline_created.wait(timeout=2)
            assert app.state.worker_pool.live_thread_count == 1
            assert _job(database, job_id)["execution_count_total"] == 0
            assert _job(database, job_id)["status"] == JobStatus.QUEUED.value
            health = client.get("/health")
            assert health.status_code == 200
            assert set(health.json()) == {"status", "version", "worker"}
            assert not app.state.activation_barrier.activated

            os.write(activation_write, b"A")
            os.close(activation_write)
            activation_write = -1

            assert executed.wait(timeout=2)
            assert app.state.activation_barrier.activation_count == 1
            assert _job(database, job_id)["execution_count_total"] == 1
    finally:
        if activation_write >= 0:
            os.close(activation_write)
        os.close(activation_read)


def test_precommit_pipe_eof_keeps_execution_count_zero(tmp_path: Path) -> None:
    database = tmp_path / "eof.sqlite3"
    job_id = _queued_database(database)
    activation_read, activation_write = os.pipe()
    app = create_app(
        db_path=database,
        api_token="token",
        pipeline_builder=lambda _repository: RecordingPipeline(),
        worker_poll_interval=60,
        precommit_activation_fd=activation_read,
    )
    try:
        with TestClient(app):
            os.close(activation_write)
            activation_write = -1
            assert app.state.activation_barrier.wait_closed(timeout=2)
            assert not app.state.activation_barrier.activated
            assert _job(database, job_id)["execution_count_total"] == 0
            assert _job(database, job_id)["status"] == JobStatus.QUEUED.value
    finally:
        if activation_write >= 0:
            os.close(activation_write)
        os.close(activation_read)


def _precommit_child(database: str, activation_fd: int, ready_fd: int) -> None:
    app = create_app(
        db_path=Path(database),
        api_token="token",
        pipeline_builder=lambda _repository: RecordingPipeline(),
        worker_poll_interval=60,
        precommit_activation_fd=activation_fd,
    )
    with TestClient(app):
        os.write(ready_fd, b"R")
        os.close(ready_fd)
        threading.Event().wait()


def test_precommit_sigkill_before_activate_keeps_execution_count_zero(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sigkill.sqlite3"
    job_id = _queued_database(database)
    activation_read, activation_write = os.pipe()
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(activation_write)
            os.close(ready_read)
            _precommit_child(str(database), activation_read, ready_write)
        finally:
            os._exit(0)

    os.close(activation_read)
    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b"R"
        os.kill(pid, signal.SIGKILL)
        found, status = os.waitpid(pid, 0)
        assert found == pid
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
        assert _job(database, job_id)["execution_count_total"] == 0
        assert _job(database, job_id)["status"] == JobStatus.QUEUED.value
    finally:
        os.close(activation_write)
        os.close(ready_read)
