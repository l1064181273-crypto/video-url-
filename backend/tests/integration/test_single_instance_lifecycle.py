from __future__ import annotations

import multiprocessing
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.instance_lock import InstanceAlreadyRunningError
from lvt.core.jobs import JobEventType, JobStatus
from lvt.core.processes import CancellationToken
from lvt.db.repository import JobRepository
from lvt.workers.runner import (
    JobCancellationToken,
    WorkerPipeline,
    WorkerStartupError,
)


class ProcessBlockingPipeline:
    def __init__(self, claimed: Connection, release: Any) -> None:
        self.claimed = claimed
        self.release = release

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
        self.claimed.send((job_id, run_id))
        assert self.release.wait(timeout=10)
        cancellation.raise_if_cancelled()


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


def _run_owner(
    db_path: str,
    control: Connection,
    claimed: Connection,
    release: Any,
) -> None:
    def builder(_repository: JobRepository) -> WorkerPipeline:
        return ProcessBlockingPipeline(claimed, release)

    app = create_app(
        db_path=Path(db_path),
        api_token="token",
        pipeline_builder=builder,
        worker_poll_interval=60,
    )
    with TestClient(app):
        recovery = app.state.startup_recovery
        control.send(
            (
                "started",
                recovery.interrupted_requeued,
                recovery.cancelling_cancelled,
            )
        )
        assert control.recv() == "stop"
    control.send(
        (
            "stopped",
            [
                thread.name
                for thread in threading.enumerate()
                if thread.name.startswith("lvt-worker-")
            ],
        )
    )


def _attempt_contender(
    db_path: str,
    result: Connection,
    factory_called: Any,
) -> None:
    def builder(_repository: JobRepository) -> WorkerPipeline:
        factory_called.set()
        return PassivePipeline()

    app = create_app(
        db_path=Path(db_path),
        api_token="token",
        pipeline_builder=builder,
        worker_poll_interval=60,
    )
    try:
        with TestClient(app):
            result.send(("unexpected_start", None, []))
    except InstanceAlreadyRunningError as exc:
        result.send(
            (
                "locked",
                str(exc),
                [
                    thread.name
                    for thread in threading.enumerate()
                    if thread.name.startswith("lvt-worker-")
                ],
            )
        )


def _run_successor(
    db_path: str,
    control: Connection,
    claimed: Connection,
    release: Any,
) -> None:
    _run_owner(db_path, control, claimed, release)


def _join_process(process: multiprocessing.Process) -> None:
    process.join(timeout=10)
    assert not process.is_alive()
    assert process.exitcode == 0


def _recv(connection: Connection) -> Any:
    assert connection.poll(timeout=10)
    return connection.recv()


def test_late_second_process_fails_before_recovery_or_worker_start(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    database = tmp_path / "lvt.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job_id = str(repository.create("https://example.test/restart-window")["uuid"])
    crashed_claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert crashed_claim is not None
    crashed_run_id = str(crashed_claim["active_run_id"])

    owner_parent, owner_child = context.Pipe()
    owner_claimed_parent, owner_claimed_child = context.Pipe(duplex=False)
    owner_release = context.Event()
    owner = context.Process(
        target=_run_owner,
        args=(str(database), owner_child, owner_claimed_child, owner_release),
    )
    owner.start()
    assert _recv(owner_parent) == ("started", 1, 0)
    claimed_job_id, owner_run_id = _recv(owner_claimed_parent)
    assert claimed_job_id == job_id
    assert owner_run_id != crashed_run_id

    before = repository.get(job_id)
    before_events = repository.list_events(job_id)
    assert before is not None
    assert before["status"] == JobStatus.DOWNLOADING.value
    assert before["active_run_id"] == owner_run_id
    assert sum(event["status"] == JobEventType.INTERRUPTED.value for event in before_events) == 1

    contender_parent, contender_child = context.Pipe(duplex=False)
    factory_called = context.Event()
    contender = context.Process(
        target=_attempt_contender,
        args=(str(database), contender_child, factory_called),
    )
    contender.start()
    outcome, message, worker_threads = _recv(contender_parent)
    _join_process(contender)

    assert outcome == "locked"
    assert "already running" in message
    assert not factory_called.is_set()
    assert worker_threads == []
    assert repository.get(job_id) == before
    assert repository.list_events(job_id) == before_events

    owner_release.set()
    owner_parent.send("stop")
    stopped, owner_threads = _recv(owner_parent)
    assert stopped == "stopped"
    assert owner_threads == []
    _join_process(owner)

    successor_parent, successor_child = context.Pipe()
    successor_claimed_parent, successor_claimed_child = context.Pipe(duplex=False)
    successor_release = context.Event()
    successor = context.Process(
        target=_run_successor,
        args=(
            str(database),
            successor_child,
            successor_claimed_child,
            successor_release,
        ),
    )
    successor.start()
    assert _recv(successor_parent) == ("started", 1, 0)
    successor_job_id, successor_run_id = _recv(successor_claimed_parent)
    assert successor_job_id == job_id
    assert successor_run_id not in {crashed_run_id, owner_run_id}

    recovered = repository.get(job_id)
    assert recovered is not None
    assert recovered["active_run_id"] == successor_run_id
    events = repository.list_events(job_id)
    assert sum(event["status"] == JobEventType.INTERRUPTED.value for event in events) == 2

    successor_release.set()
    successor_parent.send("stop")
    assert _recv(successor_parent) == ("stopped", [])
    _join_process(successor)


def test_pipeline_factory_failure_releases_instance_ownership(tmp_path: Path) -> None:
    database = tmp_path / "factory.sqlite3"

    def failing_builder(_repository: JobRepository) -> WorkerPipeline:
        raise RuntimeError("factory failed")

    failed_app = create_app(
        db_path=database,
        api_token="token",
        pipeline_builder=failing_builder,
    )
    with pytest.raises(WorkerStartupError), TestClient(failed_app):
        pass

    successor = create_app(db_path=database, api_token="token")
    with TestClient(successor):
        assert successor.state.startup_recovery.interrupted_requeued == 0


def test_worker_thread_start_failure_releases_instance_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "thread-start.sqlite3"
    original_start = threading.Thread.start

    def fail_worker_start(thread: threading.Thread) -> None:
        if thread.name == "lvt-worker-2":
            raise RuntimeError("thread start failed")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_worker_start)
    failed_app = create_app(
        db_path=database,
        api_token="token",
        pipeline_builder=lambda _repository: PassivePipeline(),
        worker_concurrency=2,
    )
    with pytest.raises(WorkerStartupError), TestClient(failed_app):
        pass
    assert failed_app.state.worker_pool.live_thread_count == 0

    monkeypatch.setattr(threading.Thread, "start", original_start)
    successor = create_app(db_path=database, api_token="token")
    with TestClient(successor):
        assert successor.state.startup_recovery.interrupted_requeued == 0


def test_lifespan_body_exception_releases_instance_ownership(tmp_path: Path) -> None:
    database = tmp_path / "lifespan-error.sqlite3"
    failed_app = create_app(db_path=database, api_token="token")

    with pytest.raises(RuntimeError, match="application failed"), TestClient(failed_app):
        raise RuntimeError("application failed")

    successor = create_app(db_path=database, api_token="token")
    with TestClient(successor):
        assert successor.state.startup_recovery.interrupted_requeued == 0


def test_token_stops_after_run_ownership_loss_but_not_normal_completion(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "token.sqlite3")
    repository.initialize()
    job_id = str(repository.create("https://example.test/lost-owner")["uuid"])
    claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claim is not None
    run_id = str(claim["active_run_id"])
    stale_token = JobCancellationToken(repository, job_id, run_id)

    assert repository.recover_startup().interrupted_requeued == 1
    assert stale_token.cancelled

    class CompletedRepository:
        def get(self, requested_job_id: str) -> dict[str, object]:
            assert requested_job_id == job_id
            return {
                "status": JobStatus.COMPLETED.value,
                "active_run_id": None,
            }

    completed_token = JobCancellationToken(CompletedRepository(), job_id, run_id)  # type: ignore[arg-type]
    assert not completed_token.cancelled
