from __future__ import annotations

import asyncio
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Never
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from lvt.api.control import JobFileStore, UnsafeJobPathError
from lvt.core.instance_lock import ProcessInstanceLock
from lvt.core.jobs import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobStatus,
    classify_error_code,
)
from lvt.core.models import JobOptions
from lvt.db.repository import DeleteFinalizationError, DeleteJobResult, JobRepository
from lvt.security.urls import validate_public_media_url
from lvt.workers.runner import (
    CancelRequestResult,
    Clock,
    JobWorkerPool,
    WorkerPipeline,
    WorkerStartupError,
)


class CreateJobsRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    options: JobOptions = Field(default_factory=JobOptions)


class UpdateSettingsRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    worker_concurrency: int = Field(ge=1, le=2)


def create_app(
    *,
    db_path: Path,
    api_token: str,
    capabilities: dict[str, Any] | None = None,
    work_root: Path | None = None,
    pipeline_builder: Callable[[JobRepository], WorkerPipeline] | None = None,
    worker_concurrency: int | None = None,
    worker_poll_interval: float = 0.25,
    worker_clock: Clock | None = None,
) -> FastAPI:
    repository = JobRepository(db_path)
    if worker_concurrency is not None and (
        type(worker_concurrency) is not int or worker_concurrency not in {1, 2}
    ):
        raise ValueError("worker_concurrency must be 1 or 2")
    file_store = JobFileStore(work_root or db_path.parent / "work")
    instance_lock = ProcessInstanceLock(db_path.with_name(f"{db_path.name}.instance.lock"))
    worker_pool = (
        JobWorkerPool(
            repository=repository,
            pipeline_factory=lambda: pipeline_builder(repository),
            concurrency=worker_concurrency or 1,
            clock=worker_clock,
            poll_interval=worker_poll_interval,
        )
        if pipeline_builder is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        instance_lock.acquire()
        try:
            file_store.ensure_root()
            repository.initialize()
            file_store.reconcile_deletions(repository)
            if worker_concurrency is not None:
                repository.set_worker_concurrency(worker_concurrency)
            effective_concurrency = repository.get_worker_concurrency()
            recovery = repository.recover_startup()
            _app.state.startup_recovery = recovery
            if worker_pool is not None:
                worker_pool.set_initial_concurrency(effective_concurrency)
                worker_pool.start()
            try:
                yield
            finally:
                if worker_pool is not None:
                    await asyncio.to_thread(worker_pool.stop)
        finally:
            if worker_pool is None or worker_pool.live_thread_count == 0:
                instance_lock.release()

    app = FastAPI(title="Local Video Transcriber", version="0.1.0", lifespan=lifespan)
    app.state.repository = repository
    app.state.worker_pool = worker_pool
    app.state.instance_lock = instance_lock
    app.state.file_store = file_store

    def require_token(
        supplied_token: Annotated[str | None, Header(alias="X-LVT-Token")] = None,
    ) -> None:
        if not supplied_token or not secrets.compare_digest(supplied_token, api_token):
            raise HTTPException(
                status_code=401,
                detail={"error_code": "UNAUTHORIZED", "message": "配对 Token 无效"},
            )

    @app.get("/health", response_model=None)
    def health() -> Any:
        payload: dict[str, Any] = {"status": "healthy", "version": "0.1.0"}
        if worker_pool is None:
            return payload
        worker_health = worker_pool.health_snapshot()
        payload["worker"] = worker_health
        if worker_health["status"] == "healthy":
            return payload
        payload["status"] = "unhealthy"
        return JSONResponse(status_code=503, content=payload)

    @app.get("/api/v1/capabilities", dependencies=[Depends(require_token)])
    def get_capabilities() -> dict[str, Any]:
        return capabilities or {}

    @app.post("/api/v1/jobs", dependencies=[Depends(require_token)])
    def create_jobs(payload: CreateJobsRequest) -> dict[str, list[dict[str, Any]]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for raw_url in payload.urls:
            try:
                url = validate_public_media_url(raw_url)
            except ValueError as exc:
                rejected.append(
                    {
                        "url": raw_url,
                        "error_code": "INVALID_URL",
                        "message": str(exc),
                    }
                )
                continue
            accepted.append(_public_job(repository.create(url, payload.options.model_dump())))
        if accepted and worker_pool is not None:
            worker_pool.notify()
        return {"accepted": accepted, "rejected": rejected}

    @app.get("/api/v1/jobs", dependencies=[Depends(require_token)])
    def list_jobs() -> list[dict[str, Any]]:
        return [_public_job(job) for job in repository.list()]

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    def get_job(job_id: str) -> dict[str, Any]:
        job = repository.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "JOB_NOT_FOUND", "message": "任务不存在"},
            )
        return _public_job(job)

    @app.post("/api/v1/jobs/{job_id}/retry", dependencies=[Depends(require_token)])
    def retry_job(job_id: str) -> dict[str, Any]:
        current = repository.get(job_id)
        if current is None:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        status = JobStatus(str(current["status"]))
        if status is JobStatus.QUEUED:
            return _public_job(current)
        if status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            _raise_api_error(409, "JOB_STATE_CONFLICT", "当前任务状态不允许重试")
        if (
            status is JobStatus.FAILED
            and current["error_code"] is not None
            and not classify_error_code(str(current["error_code"])).policy.manual_retry
        ):
            _raise_api_error(409, "RETRY_NOT_ALLOWED", "该错误不允许手工重试")
        if not repository.manual_retry(job_id, status):
            return _retry_after_race(repository, job_id)
        if worker_pool is not None:
            worker_pool.notify()
        retried = repository.get(job_id)
        if retried is None:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        return _public_job(retried)

    @app.post("/api/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    def cancel_job(job_id: str) -> dict[str, Any]:
        if worker_pool is not None:
            result = worker_pool.request_cancel(job_id)
        else:
            result = _request_cancel_without_worker(repository, job_id)
        if result is CancelRequestResult.NOT_FOUND:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        if result in {CancelRequestResult.CONFLICT, CancelRequestResult.STALE}:
            _raise_api_error(409, "JOB_STATE_CONFLICT", "当前任务状态不允许取消")
        current = repository.get(job_id)
        if current is None:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        return _public_job(current)

    @app.delete(
        "/api/v1/jobs/{job_id}",
        dependencies=[Depends(require_token)],
        status_code=204,
    )
    def delete_job(job_id: str, confirm: bool = False) -> Response:
        if not confirm:
            _raise_api_error(409, "DELETE_CONFIRMATION_REQUIRED", "删除任务需要明确确认")
        current = repository.get(job_id)
        if current is None:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        status = JobStatus(str(current["status"]))
        if status not in TERMINAL_JOB_STATUSES:
            _raise_api_error(409, "JOB_STATE_CONFLICT", "只能删除已结束任务")
        artifacts = repository.list_artifacts(job_id)
        try:
            result = repository.delete_terminal_job(
                job_id,
                status,
                prepare_delete=lambda: file_store.prepare_delete(
                    job=current,
                    artifacts=artifacts,
                ),
            )
        except (OSError, UnsafeJobPathError, ValueError):
            _raise_api_error(409, "UNSAFE_JOB_PATH", "任务文件路径未通过安全校验")
        except sqlite3.DatabaseError:
            _raise_api_error(500, "DELETE_FAILED", "删除任务失败")
        except DeleteFinalizationError:
            _raise_api_error(500, "DELETE_CLEANUP_PENDING", "任务已删除，文件清理将在重启后继续")
        if result is DeleteJobResult.NOT_FOUND:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        if result is DeleteJobResult.CONFLICT:
            _raise_api_error(409, "JOB_STATE_CONFLICT", "任务状态已变化，无法删除")
        return Response(status_code=204)

    @app.get("/api/v1/jobs/{job_id}/events", dependencies=[Depends(require_token)])
    def get_job_events(
        job_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        if repository.get(job_id) is None:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        return {
            "items": repository.list_events(job_id, offset=offset, limit=limit),
            "offset": offset,
            "limit": limit,
            "total": repository.count_events(job_id),
        }

    @app.get("/api/v1/jobs/{job_id}/artifacts", dependencies=[Depends(require_token)])
    def get_job_artifacts(job_id: str) -> dict[str, list[dict[str, Any]]]:
        current = repository.get(job_id)
        if current is None:
            _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
        if current["status"] != JobStatus.COMPLETED.value:
            _raise_api_error(409, "JOB_STATE_CONFLICT", "任务尚未完成")
        items = [
            {
                "id": artifact["id"],
                "kind": artifact["kind"],
                "created_at": artifact["created_at"],
                "download_url": f"/api/v1/artifacts/{artifact['id']}/download",
            }
            for artifact in repository.list_artifacts(job_id)
        ]
        return {"items": items}

    @app.get(
        "/api/v1/artifacts/{artifact_id}/download",
        dependencies=[Depends(require_token)],
    )
    def download_artifact(artifact_id: str) -> StreamingResponse:
        artifact = repository.get_artifact(artifact_id)
        if artifact is None or artifact["job_status"] != JobStatus.COMPLETED.value:
            _raise_api_error(404, "ARTIFACT_NOT_FOUND", "产物不存在或不可下载")
        try:
            stream = file_store.open_artifact(
                job_id=str(artifact["job_id"]),
                kind=str(artifact["kind"]),
                relative_path=str(artifact["path"]),
                checkpoint_pointer=str(artifact["checkpoint_pointer"]),
            )
        except (OSError, UnsafeJobPathError, ValueError):
            repository.record_artifact_unavailable(
                job_id=str(artifact["job_id"]),
                artifact_id=artifact_id,
                reason="path_validation_failed_or_missing",
            )
            _raise_api_error(404, "ARTIFACT_NOT_FOUND", "产物不存在或不可下载")
        filename = str(artifact["kind"])
        disposition = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
        return StreamingResponse(
            stream,
            media_type="application/octet-stream",
            headers={"Content-Disposition": disposition},
            background=BackgroundTask(stream.close),
        )

    @app.get("/api/v1/settings", dependencies=[Depends(require_token)])
    def get_settings() -> dict[str, Any]:
        return _settings_response(repository, worker_pool)

    @app.patch("/api/v1/settings", dependencies=[Depends(require_token)])
    def update_settings(payload: UpdateSettingsRequest) -> dict[str, Any]:
        try:
            if worker_pool is None:
                repository.set_worker_concurrency(payload.worker_concurrency)
            else:
                worker_pool.update_concurrency(payload.worker_concurrency)
        except WorkerStartupError:
            _raise_api_error(503, "SETTINGS_APPLY_FAILED", "无法启动新的工作线程")
        return _settings_response(repository, worker_pool)

    return app


def _raise_api_error(status_code: int, error_code: str, message: str) -> Never:
    raise HTTPException(
        status_code=status_code,
        detail={"error_code": error_code, "message": message},
    )


def _retry_after_race(repository: JobRepository, job_id: str) -> dict[str, Any]:
    current = repository.get(job_id)
    if current is None:
        _raise_api_error(404, "JOB_NOT_FOUND", "任务不存在")
    if current["status"] != JobStatus.QUEUED.value:
        _raise_api_error(409, "JOB_STATE_CONFLICT", "任务状态已变化，无法重试")
    return _public_job(current)


def _request_cancel_without_worker(
    repository: JobRepository,
    job_id: str,
) -> CancelRequestResult:
    current = repository.get(job_id)
    if current is None:
        return CancelRequestResult.NOT_FOUND
    status = JobStatus(str(current["status"]))
    if status is JobStatus.CANCELLED:
        return CancelRequestResult.ALREADY_CANCELLED
    if status is JobStatus.CANCELLING:
        return CancelRequestResult.ALREADY_CANCELLING
    if status is JobStatus.QUEUED:
        if repository.request_cancel(job_id, JobStatus.QUEUED):
            return CancelRequestResult.CANCELLED
        return CancelRequestResult.STALE
    if status in ACTIVE_JOB_STATUSES:
        if repository.request_cancel(job_id, status):
            return CancelRequestResult.CANCELLING
        return CancelRequestResult.STALE
    return CancelRequestResult.CONFLICT


def _settings_response(
    repository: JobRepository,
    worker_pool: JobWorkerPool | None,
) -> dict[str, Any]:
    return {
        "worker_concurrency": repository.get_worker_concurrency(),
        "runtime_effect": (
            "new_claims_only" if worker_pool is not None else "persisted_for_next_worker_start"
        ),
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in job.items() if key not in {"work_dir", "checkpoint_pointer"}
    }
