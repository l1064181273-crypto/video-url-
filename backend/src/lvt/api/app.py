from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lvt.core.models import JobOptions
from lvt.db.repository import JobRepository
from lvt.security.urls import validate_public_media_url
from lvt.workers.runner import Clock, JobWorkerPool, WorkerPipeline


class CreateJobsRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    options: JobOptions = Field(default_factory=JobOptions)


def create_app(
    *,
    db_path: Path,
    api_token: str,
    capabilities: dict[str, Any] | None = None,
    pipeline_builder: Callable[[JobRepository], WorkerPipeline] | None = None,
    worker_concurrency: int = 1,
    worker_poll_interval: float = 0.25,
    worker_clock: Clock | None = None,
) -> FastAPI:
    repository = JobRepository(db_path)
    repository.initialize()
    if type(worker_concurrency) is not int or worker_concurrency not in {1, 2}:
        raise ValueError("worker_concurrency must be 1 or 2")
    repository.set_worker_concurrency(worker_concurrency)
    worker_pool = (
        JobWorkerPool(
            repository=repository,
            pipeline_factory=lambda: pipeline_builder(repository),
            concurrency=worker_concurrency,
            clock=worker_clock,
            poll_interval=worker_poll_interval,
        )
        if pipeline_builder is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        if worker_pool is not None:
            worker_pool.start()
        try:
            yield
        finally:
            if worker_pool is not None:
                await asyncio.to_thread(worker_pool.stop)

    app = FastAPI(title="Local Video Transcriber", version="0.1.0", lifespan=lifespan)
    app.state.repository = repository
    app.state.worker_pool = worker_pool

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
            accepted.append(repository.create(url, payload.options.model_dump()))
        if accepted and worker_pool is not None:
            worker_pool.notify()
        return {"accepted": accepted, "rejected": rejected}

    @app.get("/api/v1/jobs", dependencies=[Depends(require_token)])
    def list_jobs() -> list[dict[str, Any]]:
        return repository.list()

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    def get_job(job_id: str) -> dict[str, Any]:
        job = repository.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "JOB_NOT_FOUND", "message": "任务不存在"},
            )
        return job

    return app
