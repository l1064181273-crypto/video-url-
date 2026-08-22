from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from lvt.db.repository import JobRepository
from lvt.security.urls import validate_public_media_url


class JobOptions(BaseModel):
    asr_model: str = "default"
    translate_to: str = "zh-CN"
    diarization: bool = True


class CreateJobsRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    options: JobOptions = Field(default_factory=JobOptions)


def create_app(
    *,
    db_path: Path,
    api_token: str,
    capabilities: dict[str, Any] | None = None,
) -> FastAPI:
    repository = JobRepository(db_path)
    repository.initialize()
    app = FastAPI(title="Local Video Transcriber", version="0.1.0")
    app.state.repository = repository

    def require_token(
        supplied_token: Annotated[str | None, Header(alias="X-LVT-Token")] = None,
    ) -> None:
        if not supplied_token or not secrets.compare_digest(supplied_token, api_token):
            raise HTTPException(
                status_code=401,
                detail={"error_code": "UNAUTHORIZED", "message": "配对 Token 无效"},
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "version": "0.1.0"}

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
