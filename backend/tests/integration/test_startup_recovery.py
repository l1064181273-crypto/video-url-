import threading
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.jobs import ErrorCode, JobEventType, JobStatus
from lvt.core.processes import CancellationToken
from lvt.db.repository import JobRepository
from lvt.workers.runner import WorkerPipeline


class ShutdownWaitingPipeline:
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
        assert cancellation.wait(timeout=2)
        cancellation.raise_if_cancelled()


def test_lifespan_recovery_commits_before_pipeline_factory_and_claim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lvt.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job_id = str(repository.create("https://example.test/interrupted")["uuid"])
    first_claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert first_claim is not None
    old_run_id = str(first_claim["active_run_id"])
    factory_observed = threading.Event()

    def builder(current_repository: JobRepository) -> WorkerPipeline:
        recovered = current_repository.get(job_id)
        assert recovered is not None
        assert recovered["status"] == JobStatus.QUEUED.value
        assert recovered["active_run_id"] is None
        events = current_repository.list_events(job_id)
        assert events[-1]["status"] == JobEventType.INTERRUPTED.value
        factory_observed.set()
        return ShutdownWaitingPipeline()

    app = create_app(
        db_path=database,
        api_token="token",
        pipeline_builder=builder,
        worker_poll_interval=60,
    )
    with TestClient(app):
        assert factory_observed.wait(timeout=2)
        new_claim = repository.get(job_id)
        assert new_claim is not None
        assert new_claim["active_run_id"] != old_run_id
        assert app.state.startup_recovery.interrupted_requeued == 1

    statuses = [event["status"] for event in repository.list_events(job_id)]
    interrupted_index = statuses.index(JobEventType.INTERRUPTED.value)
    assert statuses[interrupted_index + 1] == JobEventType.CLAIMED.value


def test_repeated_lifespan_keeps_terminal_jobs_without_recovery_events(
    tmp_path: Path,
) -> None:
    database = tmp_path / "terminal.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    cancelled_id = str(repository.create("https://example.test/cancelled")["uuid"])
    failed_id = str(repository.create("https://example.test/failed")["uuid"])
    assert repository.request_cancel(cancelled_id, JobStatus.QUEUED)
    claimed = repository.claim_next(
        expected_job_id=failed_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claimed is not None
    assert repository.fail_job(
        failed_id,
        str(claimed["active_run_id"]),
        JobStatus.DOWNLOADING,
        ErrorCode.MEDIA_INVALID,
        "invalid",
    )
    before = {job_id: repository.list_events(job_id) for job_id in (cancelled_id, failed_id)}
    app = create_app(db_path=database, api_token="token")

    with TestClient(app):
        assert app.state.startup_recovery.interrupted_requeued == 0
        assert app.state.startup_recovery.cancelling_cancelled == 0
    with TestClient(app):
        assert app.state.startup_recovery.interrupted_requeued == 0
        assert app.state.startup_recovery.cancelling_cancelled == 0

    for job_id in (cancelled_id, failed_id):
        assert repository.list_events(job_id) == before[job_id]
