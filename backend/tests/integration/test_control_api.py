from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.jobs import ErrorCode, JobStatus
from lvt.core.processes import CancellationToken
from lvt.db.repository import (
    REQUIRED_ARTIFACT_KINDS,
    ArtifactCompletionResult,
    ArtifactSpec,
    JobRepository,
)

TOKEN_HEADER = {"X-LVT-Token": "token"}


class PassivePipeline:
    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return JobStatus.DOWNLOADING

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Any,
    ) -> None:
        cancellation.raise_if_cancelled()


def _create_failed(repository: JobRepository, suffix: str) -> str:
    job_id = str(repository.create(f"https://example.test/{suffix}")["uuid"])
    claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claim is not None
    assert repository.fail_job(
        job_id,
        str(claim["active_run_id"]),
        JobStatus.DOWNLOADING,
        ErrorCode.MEDIA_INVALID,
        "invalid",
    )
    return job_id


def _create_non_retryable_failure(repository: JobRepository) -> str:
    job_id = str(repository.create("https://example.test/non-retryable")["uuid"])
    claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claim is not None
    assert repository.fail_job(
        job_id,
        str(claim["active_run_id"]),
        JobStatus.DOWNLOADING,
        ErrorCode.INVALID_URL,
        "invalid URL",
    )
    return job_id


def _create_cancelled(repository: JobRepository, suffix: str) -> str:
    job_id = str(repository.create(f"https://example.test/{suffix}")["uuid"])
    assert repository.request_cancel(job_id, JobStatus.QUEUED)
    return job_id


def _complete_job(
    repository: JobRepository,
    work_root: Path,
    suffix: str,
    *,
    artifact_path_override: str | None = None,
) -> tuple[str, list[ArtifactSpec]]:
    job_id = str(repository.create(f"https://example.test/{suffix}")["uuid"])
    claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.EXPORTING,
    )
    assert claim is not None
    run_id = str(claim["active_run_id"])
    assert repository.update_worker_metadata(
        job_id,
        run_id,
        JobStatus.EXPORTING,
        work_dir=f"work/{job_id}/runs/{run_id}",
        checkpoint_pointer=f"{job_id}/runs/{run_id}/export_manifest/manifest.json",
    )
    export_stage_dir = work_root / job_id / "runs" / run_id / "export_manifest"
    artifact_dir = export_stage_dir / "exports"
    artifact_dir.mkdir(parents=True)
    artifacts: list[ArtifactSpec] = []
    manifest_outputs: list[dict[str, object]] = []
    for index, kind in enumerate(sorted(REQUIRED_ARTIFACT_KINDS)):
        path = artifact_dir / kind
        data = f"{job_id}:{kind}".encode()
        path.write_bytes(data)
        relative_path = path.relative_to(work_root).as_posix()
        manifest_outputs.append(
            {
                "kind": kind,
                "relative_path": relative_path,
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        if index == 0 and artifact_path_override is not None:
            relative_path = artifact_path_override
        artifacts.append(
            ArtifactSpec(
                artifact_id=f"{job_id}-{index}",
                kind=kind,
                path=relative_path,
            )
        )
    (export_stage_dir / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "run_id": run_id,
                "stage": "export_manifest",
                "outputs": manifest_outputs,
            }
        ),
        encoding="utf-8",
    )
    (export_stage_dir / ".published").write_text("published", encoding="utf-8")
    assert (
        repository.complete_job_with_artifacts(
            job_id=job_id,
            run_id=run_id,
            artifacts=artifacts,
        )
        is ArtifactCompletionResult.COMPLETED
    )
    return job_id, artifacts


def _detail_code(response: Any) -> str:
    return str(response.json()["detail"]["error_code"])


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/api/v1/jobs/missing/retry", {}),
        ("POST", "/api/v1/jobs/missing/cancel", {}),
        ("DELETE", "/api/v1/jobs/missing?confirm=true", {}),
        ("GET", "/api/v1/jobs/missing/events", {}),
        ("GET", "/api/v1/jobs/missing/artifacts", {}),
        ("GET", "/api/v1/artifacts/missing/download", {}),
        ("GET", "/api/v1/settings", {}),
        ("PATCH", "/api/v1/settings", {"json": {"worker_concurrency": 1}}),
    ],
)
def test_control_routes_require_token(
    tmp_path: Path,
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    app = create_app(db_path=tmp_path / "auth.sqlite3", api_token="token")
    with TestClient(app) as client:
        response = client.request(method, path, **kwargs)
    assert response.status_code == 401
    assert _detail_code(response) == "UNAUTHORIZED"


def test_retry_cancel_events_and_error_contracts(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    app = create_app(
        db_path=database,
        api_token="token",
        work_root=work_root,
    )
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        failed_id = _create_failed(repository, "failed")
        non_retryable_id = _create_non_retryable_failure(repository)
        cancelled_id = _create_cancelled(repository, "cancelled")
        completed_id, _ = _complete_job(repository, work_root, "completed")
        active_id = str(repository.create("https://example.test/active")["uuid"])
        active = repository.claim_next(
            expected_job_id=active_id,
            first_required_stage=JobStatus.DOWNLOADING,
        )
        assert active is not None
        queued_id = str(repository.create("https://example.test/queued")["uuid"])

        retried = client.post(f"/api/v1/jobs/{failed_id}/retry", headers=TOKEN_HEADER)
        assert retried.status_code == 200
        assert retried.json()["status"] == JobStatus.QUEUED.value
        assert retried.json()["retry_cycle"] == 1
        assert "work_dir" not in retried.json()
        assert "checkpoint_pointer" not in retried.json()

        repeated = client.post(f"/api/v1/jobs/{failed_id}/retry", headers=TOKEN_HEADER)
        assert repeated.status_code == 200
        assert repeated.json()["retry_cycle"] == 1

        retried_cancelled = client.post(
            f"/api/v1/jobs/{cancelled_id}/retry",
            headers=TOKEN_HEADER,
        )
        assert retried_cancelled.status_code == 200
        assert retried_cancelled.json()["retry_cycle"] == 1

        queued_retry = client.post(f"/api/v1/jobs/{queued_id}/retry", headers=TOKEN_HEADER)
        assert queued_retry.status_code == 200
        assert queued_retry.json()["retry_cycle"] == 0

        for conflict_id in (active_id, completed_id):
            conflict = client.post(
                f"/api/v1/jobs/{conflict_id}/retry",
                headers=TOKEN_HEADER,
            )
            assert conflict.status_code == 409
            assert _detail_code(conflict) == "JOB_STATE_CONFLICT"

        missing = client.post("/api/v1/jobs/missing/retry", headers=TOKEN_HEADER)
        assert missing.status_code == 404
        assert _detail_code(missing) == "JOB_NOT_FOUND"
        non_retryable = client.post(
            f"/api/v1/jobs/{non_retryable_id}/retry",
            headers=TOKEN_HEADER,
        )
        assert non_retryable.status_code == 409
        assert _detail_code(non_retryable) == "RETRY_NOT_ALLOWED"

        cancel_active = client.post(
            f"/api/v1/jobs/{active_id}/cancel",
            headers=TOKEN_HEADER,
        )
        assert cancel_active.status_code == 200
        assert cancel_active.json()["status"] == JobStatus.CANCELLING.value
        repeated_cancel = client.post(
            f"/api/v1/jobs/{active_id}/cancel",
            headers=TOKEN_HEADER,
        )
        assert repeated_cancel.status_code == 200

        cancel_completed = client.post(
            f"/api/v1/jobs/{completed_id}/cancel",
            headers=TOKEN_HEADER,
        )
        assert cancel_completed.status_code == 409
        assert _detail_code(cancel_completed) == "JOB_STATE_CONFLICT"

        events = client.get(
            f"/api/v1/jobs/{failed_id}/events?offset=0&limit=2",
            headers=TOKEN_HEADER,
        )
        assert events.status_code == 200
        assert events.json()["offset"] == 0
        assert events.json()["limit"] == 2
        assert events.json()["total"] >= 3
        assert len(events.json()["items"]) == 2
        assert all("message" in event for event in events.json()["items"])

        assert client.get("/api/v1/jobs/missing/events", headers=TOKEN_HEADER).status_code == 404


def test_artifact_list_download_and_path_attacks(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    app = create_app(db_path=database, api_token="token", work_root=work_root)

    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        completed_id, artifacts = _complete_job(repository, work_root, "download")

        listed = client.get(
            f"/api/v1/jobs/{completed_id}/artifacts",
            headers=TOKEN_HEADER,
        )
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 8
        assert all("path" not in item for item in items)
        assert all(item["download_url"].startswith("/api/v1/artifacts/") for item in items)

        artifact = artifacts[0]
        downloaded = client.get(
            f"/api/v1/artifacts/{artifact.artifact_id}/download",
            headers=TOKEN_HEADER,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == f"{completed_id}:{artifact.kind}".encode()
        assert artifact.kind in downloaded.headers["content-disposition"]

        unknown = client.get(
            "/api/v1/artifacts/unknown/download",
            headers=TOKEN_HEADER,
        )
        assert unknown.status_code == 404
        assert _detail_code(unknown) == "ARTIFACT_NOT_FOUND"

        failed_id = _create_failed(repository, "not-completed")
        assert (
            repository.register_artifact(
                job_id=failed_id,
                run_id="stale",
                expected_status=JobStatus.EXPORTING,
                artifact_id="never",
                kind="source.txt",
                path=f"{failed_id}/source.txt",
            ).value
            == "stale"
        )
        assert (
            client.get(
                f"/api/v1/jobs/{failed_id}/artifacts",
                headers=TOKEN_HEADER,
            ).status_code
            == 409
        )

        attacked_id, attacked = _complete_job(
            repository,
            work_root,
            "traversal",
            artifact_path_override="../outside.txt",
        )
        traversal = client.get(
            f"/api/v1/artifacts/{attacked[0].artifact_id}/download",
            headers=TOKEN_HEADER,
        )
        assert traversal.status_code == 404
        assert traversal.content != b"secret"
        assert str(outside) not in traversal.text
        assert repository.list_events(attacked_id)[-1]["status"] == "artifact_unavailable"

        absolute_id, absolute_artifacts = _complete_job(
            repository,
            work_root,
            "absolute-path",
            artifact_path_override=str(outside),
        )
        absolute = client.get(
            f"/api/v1/artifacts/{absolute_artifacts[0].artifact_id}/download",
            headers=TOKEN_HEADER,
        )
        assert absolute.status_code == 404
        assert absolute.content != b"secret"
        assert str(outside) not in absolute.text
        assert repository.list_events(absolute_id)[-1]["status"] == "artifact_unavailable"

        cross_id, cross_artifacts = _complete_job(
            repository,
            work_root,
            "cross-job",
            artifact_path_override=artifacts[0].path,
        )
        cross_job = client.get(
            f"/api/v1/artifacts/{cross_artifacts[0].artifact_id}/download",
            headers=TOKEN_HEADER,
        )
        assert cross_job.status_code == 404
        assert cross_job.content != downloaded.content
        assert repository.list_events(cross_id)[-1]["status"] == "artifact_unavailable"

        symlink_id, symlink_artifacts = _complete_job(repository, work_root, "symlink")
        symlink_path = work_root / symlink_artifacts[0].path
        symlink_path.unlink()
        symlink_path.symlink_to(outside)
        symlink = client.get(
            f"/api/v1/artifacts/{symlink_artifacts[0].artifact_id}/download",
            headers=TOKEN_HEADER,
        )
        assert symlink.status_code == 404
        assert symlink.content != b"secret"
        assert repository.list_events(symlink_id)[-1]["status"] == "artifact_unavailable"
        assert repository.get(attacked_id) is not None
        assert repository.get(symlink_id) is not None


def test_artifact_rejects_same_job_non_export_path(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    app = create_app(db_path=database, api_token="token", work_root=work_root)
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        job_id, artifacts = _complete_job(repository, work_root, "private-artifact")
        job = repository.get(job_id)
        assert job is not None
        run_id = str(job["checkpoint_pointer"]).split("/")[2]
        invalid_paths = [
            work_root / job_id / "private" / artifacts[0].kind,
            work_root
            / job_id
            / "runs"
            / "other-run"
            / "export_manifest"
            / "exports"
            / artifacts[0].kind,
            work_root / job_id / "runs" / run_id / "downloaded_media" / artifacts[0].kind,
        ]
        for index, invalid_path in enumerate(invalid_paths):
            invalid_path.parent.mkdir(parents=True, exist_ok=True)
            invalid_path.write_text(f"invalid-{index}", encoding="utf-8")
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE artifacts SET path = ? WHERE id = ?",
                    (
                        invalid_path.relative_to(work_root).as_posix(),
                        artifacts[0].artifact_id,
                    ),
                )

            response = client.get(
                f"/api/v1/artifacts/{artifacts[0].artifact_id}/download",
                headers=TOKEN_HEADER,
            )
            assert response.status_code == 404
            assert response.content != f"invalid-{index}".encode()
            assert repository.list_events(job_id)[-1]["status"] == "artifact_unavailable"


def test_artifact_directory_replacement_after_manifest_validation_is_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    outside = tmp_path / "outside"
    app = create_app(db_path=database, api_token="token", work_root=work_root)
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        job_id, artifacts = _complete_job(repository, work_root, "replace-stage")
        job = repository.get(job_id)
        assert job is not None
        export_stage = work_root / str(job["checkpoint_pointer"]).removesuffix("/manifest.json")
        backup = export_stage.with_name("export_manifest.backup")
        outside_artifact = outside / "exports" / artifacts[0].kind
        outside_artifact.parent.mkdir(parents=True)
        outside_artifact.write_text("outside", encoding="utf-8")

        def replace_stage() -> None:
            export_stage.rename(backup)
            export_stage.symlink_to(outside, target_is_directory=True)

        app.state.file_store.artifact_open_hook = replace_stage
        try:
            response = client.get(
                f"/api/v1/artifacts/{artifacts[0].artifact_id}/download",
                headers=TOKEN_HEADER,
            )
        finally:
            app.state.file_store.artifact_open_hook = None
            if export_stage.is_symlink():
                export_stage.unlink()
            if backup.exists():
                backup.rename(export_stage)

        assert response.status_code == 404
        assert response.content != b"outside"
        assert repository.list_events(job_id)[-1]["status"] == "artifact_unavailable"


def test_open_artifact_fd_survives_concurrent_delete(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    app = create_app(db_path=database, api_token="token", work_root=work_root)
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        job_id, artifacts = _complete_job(repository, work_root, "download-delete")
        job = repository.get(job_id)
        assert job is not None
        artifact = repository.get_artifact(artifacts[0].artifact_id)
        assert artifact is not None
        stream = app.state.file_store.open_artifact(
            job_id=job_id,
            kind=str(artifact["kind"]),
            relative_path=str(artifact["path"]),
            checkpoint_pointer=str(job["checkpoint_pointer"]),
        )
        try:
            deleted = client.delete(
                f"/api/v1/jobs/{job_id}?confirm=true",
                headers=TOKEN_HEADER,
            )
            assert deleted.status_code == 204
            assert stream.read() == f"{job_id}:{artifacts[0].kind}".encode()
        finally:
            stream.close()


def test_delete_requires_confirmation_cleans_files_and_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    app = create_app(db_path=database, api_token="token", work_root=work_root)

    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        completed_id, _ = _complete_job(repository, work_root, "delete")
        job_root = work_root / completed_id

        missing_confirm = client.delete(
            f"/api/v1/jobs/{completed_id}",
            headers=TOKEN_HEADER,
        )
        assert missing_confirm.status_code == 409
        assert job_root.exists()

        deleted = client.delete(
            f"/api/v1/jobs/{completed_id}?confirm=true",
            headers=TOKEN_HEADER,
        )
        assert deleted.status_code == 204
        assert repository.get(completed_id) is None
        assert not job_root.exists()
        assert (
            client.delete(
                f"/api/v1/jobs/{completed_id}?confirm=true",
                headers=TOKEN_HEADER,
            ).status_code
            == 404
        )

        active_id = str(repository.create("https://example.test/active-delete")["uuid"])
        active = repository.claim_next(
            expected_job_id=active_id,
            first_required_stage=JobStatus.DOWNLOADING,
        )
        assert active is not None
        conflict = client.delete(
            f"/api/v1/jobs/{active_id}?confirm=true",
            headers=TOKEN_HEADER,
        )
        assert conflict.status_code == 409

        unsafe_id, _ = _complete_job(
            repository,
            work_root,
            "unsafe-delete",
            artifact_path_override="../outside/keep.txt",
        )
        unsafe = client.delete(
            f"/api/v1/jobs/{unsafe_id}?confirm=true",
            headers=TOKEN_HEADER,
        )
        assert unsafe.status_code == 409
        assert _detail_code(unsafe) == "UNSAFE_JOB_PATH"
        assert protected.read_text(encoding="utf-8") == "keep"
        assert repository.get(unsafe_id) is not None

        symlink_id, symlink_artifacts = _complete_job(
            repository,
            work_root,
            "unsafe-symlink-delete",
        )
        linked_artifact = work_root / symlink_artifacts[0].path
        linked_artifact.unlink()
        linked_artifact.symlink_to(protected)
        symlink_delete = client.delete(
            f"/api/v1/jobs/{symlink_id}?confirm=true",
            headers=TOKEN_HEADER,
        )
        assert symlink_delete.status_code == 409
        assert _detail_code(symlink_delete) == "UNSAFE_JOB_PATH"
        assert protected.read_text(encoding="utf-8") == "keep"
        assert repository.get(symlink_id) is not None


def test_delete_database_failure_restores_quarantined_job_tree(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    app = create_app(db_path=database, api_token="token", work_root=work_root)
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        job_id, artifacts = _complete_job(repository, work_root, "rollback-delete")
        job_root = work_root / job_id
        with sqlite3.connect(database) as connection:
            connection.execute(
                f"""
                CREATE TRIGGER fail_job_delete
                BEFORE DELETE ON jobs
                WHEN OLD.uuid = '{job_id}'
                BEGIN
                    SELECT RAISE(ABORT, 'injected delete failure');
                END
                """
            )

        response = client.delete(
            f"/api/v1/jobs/{job_id}?confirm=true",
            headers=TOKEN_HEADER,
        )
        assert response.status_code == 500
        assert _detail_code(response) == "DELETE_FAILED"
        assert repository.get(job_id) is not None
        assert job_root.is_dir()
        assert (work_root / artifacts[0].path).is_file()
        assert not list(work_root.glob(f".deleting-{job_id}-*"))


def test_startup_reconciles_interrupted_delete_quarantines(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    repository = JobRepository(database)
    repository.initialize()
    retained_id = _create_cancelled(repository, "retained")
    retained_root = work_root / retained_id
    retained_root.mkdir(parents=True)
    (retained_root / "checkpoint.txt").write_text("retained", encoding="utf-8")
    retained_quarantine = work_root / f".deleting-{retained_id}-{'a' * 32}"
    retained_root.rename(retained_quarantine)

    deleted_id = "deleted-job"
    deleted_quarantine = work_root / f".deleting-{deleted_id}-{'b' * 32}"
    deleted_quarantine.mkdir(parents=True)
    (deleted_quarantine / "artifact.txt").write_text("orphan", encoding="utf-8")

    app = create_app(db_path=database, api_token="token", work_root=work_root)
    with TestClient(app):
        assert (work_root / retained_id / "checkpoint.txt").read_text(
            encoding="utf-8"
        ) == "retained"
        assert not retained_quarantine.exists()
        assert not deleted_quarantine.exists()


def test_settings_validation_and_persistence(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    app = create_app(db_path=database, api_token="token")
    with TestClient(app) as client:
        current = client.get("/api/v1/settings", headers=TOKEN_HEADER)
        assert current.status_code == 200
        assert current.json() == {
            "worker_concurrency": 1,
            "runtime_effect": "persisted_for_next_worker_start",
        }

        updated = client.patch(
            "/api/v1/settings",
            headers=TOKEN_HEADER,
            json={"worker_concurrency": 2},
        )
        assert updated.status_code == 200
        assert updated.json()["worker_concurrency"] == 2
        assert updated.json()["runtime_effect"] == "persisted_for_next_worker_start"

        for invalid in (0, 3, True, "2", 1.5):
            response = client.patch(
                "/api/v1/settings",
                headers=TOKEN_HEADER,
                json={"worker_concurrency": invalid},
            )
            assert response.status_code == 422

    repository = JobRepository(database)
    assert repository.get_worker_concurrency() == 2
    restarted_app = create_app(db_path=database, api_token="token")
    with TestClient(restarted_app) as restarted:
        restored = restarted.get("/api/v1/settings", headers=TOKEN_HEADER)
        assert restored.status_code == 200
        assert restored.json()["worker_concurrency"] == 2


def test_settings_update_applies_to_new_claims_in_running_pool(tmp_path: Path) -> None:
    database = tmp_path / "runtime-settings.sqlite3"
    app = create_app(
        db_path=database,
        api_token="token",
        pipeline_builder=lambda _repository: PassivePipeline(),
        worker_concurrency=1,
        worker_poll_interval=60,
    )
    with TestClient(app) as client:
        increased = client.patch(
            "/api/v1/settings",
            headers=TOKEN_HEADER,
            json={"worker_concurrency": 2},
        )
        assert increased.status_code == 200
        assert increased.json() == {
            "worker_concurrency": 2,
            "runtime_effect": "new_claims_only",
        }
        assert app.state.worker_pool.concurrency == 2
        assert app.state.worker_pool.live_thread_count == 2

        decreased = client.patch(
            "/api/v1/settings",
            headers=TOKEN_HEADER,
            json={"worker_concurrency": 1},
        )
        assert decreased.status_code == 200
        assert decreased.json()["worker_concurrency"] == 1
        assert app.state.worker_pool.concurrency == 1


def test_settings_same_value_rejects_recorded_fatal_before_worker_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        db_path=tmp_path / "settings-same-value-fatal.sqlite3",
        api_token="token",
        pipeline_builder=lambda _repository: PassivePipeline(),
        worker_concurrency=2,
        worker_poll_interval=60,
    )
    repository: JobRepository = app.state.repository
    worker_pool = app.state.worker_pool
    assert worker_pool is not None
    failure_enabled = threading.Event()
    fatal_recorded = threading.Event()
    release_fatal_worker = threading.Event()
    injection_lock = threading.Lock()
    failure_injected = False
    original_peek = repository.peek_next_queued

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failure_injected
        if failure_enabled.is_set():
            with injection_lock:
                if not failure_injected:
                    failure_injected = True
                    raise RuntimeError("injected worker loop failure")
        return original_peek(*args, **kwargs)

    original_record_fatal = worker_pool._record_fatal

    def record_fatal_and_block(
        worker_index: int | None,
        phase: str,
        error: BaseException,
    ) -> None:
        original_record_fatal(worker_index, phase, error)
        fatal_recorded.set()
        assert release_fatal_worker.wait(timeout=5)

    monkeypatch.setattr(repository, "peek_next_queued", fail_once)
    monkeypatch.setattr(worker_pool, "_record_fatal", record_fatal_and_block)
    with TestClient(app) as client:
        try:
            healthy_no_op = client.patch(
                "/api/v1/settings",
                headers=TOKEN_HEADER,
                json={"worker_concurrency": 2},
            )
            assert healthy_no_op.status_code == 200

            failure_enabled.set()
            worker_pool.notify()
            assert fatal_recorded.wait(timeout=2)
            assert all(thread.is_alive() for thread in worker_pool._threads)

            failed_no_op = client.patch(
                "/api/v1/settings",
                headers=TOKEN_HEADER,
                json={"worker_concurrency": 2},
            )
            assert failed_no_op.status_code == 503
            assert _detail_code(failed_no_op) == "SETTINGS_APPLY_FAILED"

            health = client.get("/health")
            assert health.status_code == 503
            assert health.json()["worker"] == {
                "status": "unhealthy",
                "configured_workers": 2,
                "live_workers": 2,
                "fatal_count": 1,
            }
        finally:
            release_fatal_worker.set()


def test_settings_runtime_increase_failure_keeps_previous_value(tmp_path: Path) -> None:
    calls = 0

    def builder(_repository: JobRepository) -> PassivePipeline:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected pipeline factory failure")
        return PassivePipeline()

    app = create_app(
        db_path=tmp_path / "settings-failure.sqlite3",
        api_token="token",
        pipeline_builder=builder,
        worker_concurrency=1,
        worker_poll_interval=60,
    )
    with TestClient(app) as client:
        response = client.patch(
            "/api/v1/settings",
            headers=TOKEN_HEADER,
            json={"worker_concurrency": 2},
        )
        assert response.status_code == 503
        assert _detail_code(response) == "SETTINGS_APPLY_FAILED"
        assert app.state.repository.get_worker_concurrency() == 1
        assert app.state.worker_pool.concurrency == 1
        assert app.state.worker_pool.live_thread_count == 1
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["worker"] == {
            "status": "healthy",
            "configured_workers": 1,
            "live_workers": 1,
            "fatal_count": 0,
        }


def test_settings_thread_start_failure_keeps_health_and_previous_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        db_path=tmp_path / "settings-thread-failure.sqlite3",
        api_token="token",
        pipeline_builder=lambda _repository: PassivePipeline(),
        worker_concurrency=1,
        worker_poll_interval=60,
    )
    with TestClient(app) as client:
        original_start = threading.Thread.start

        def fail_second_worker(thread: threading.Thread) -> None:
            if thread.name == "lvt-worker-2":
                raise RuntimeError("injected thread start failure")
            original_start(thread)

        monkeypatch.setattr(threading.Thread, "start", fail_second_worker)
        response = client.patch(
            "/api/v1/settings",
            headers=TOKEN_HEADER,
            json={"worker_concurrency": 2},
        )
        assert response.status_code == 503
        assert _detail_code(response) == "SETTINGS_APPLY_FAILED"
        assert app.state.repository.get_worker_concurrency() == 1
        assert app.state.worker_pool.concurrency == 1
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["worker"] == {
            "status": "healthy",
            "configured_workers": 1,
            "live_workers": 1,
            "fatal_count": 0,
        }


def test_delete_and_retry_race_has_one_transactional_winner(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    app = create_app(db_path=database, api_token="token", work_root=work_root)
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        job_id = _create_cancelled(repository, "delete-retry-race")
        (work_root / job_id).mkdir(parents=True)
        barrier = threading.Barrier(2)

        def retry() -> int:
            barrier.wait()
            return client.post(f"/api/v1/jobs/{job_id}/retry", headers=TOKEN_HEADER).status_code

        def delete() -> int:
            barrier.wait()
            return client.delete(
                f"/api/v1/jobs/{job_id}?confirm=true",
                headers=TOKEN_HEADER,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            retry_future = executor.submit(retry)
            delete_future = executor.submit(delete)
            outcomes = (retry_future.result(), delete_future.result())

        assert outcomes in {(200, 409), (404, 204)}
        final = repository.get(job_id)
        if final is None:
            assert not (work_root / job_id).exists()
        else:
            assert final["status"] == JobStatus.QUEUED.value


def test_cancel_and_worker_complete_race_has_one_cas_winner(tmp_path: Path) -> None:
    database = tmp_path / "lvt.sqlite3"
    work_root = tmp_path / "work"
    app = create_app(
        db_path=database,
        api_token="token",
        work_root=work_root,
        pipeline_builder=lambda _repository: PassivePipeline(),
        worker_poll_interval=60,
    )
    with TestClient(app) as client:
        repository: JobRepository = app.state.repository
        job_id = str(repository.create("https://example.test/cancel-complete")["uuid"])
        claim = repository.claim_next(
            expected_job_id=job_id,
            first_required_stage=JobStatus.EXPORTING,
        )
        assert claim is not None
        artifacts = []
        for index, kind in enumerate(sorted(REQUIRED_ARTIFACT_KINDS)):
            path = work_root / job_id / "runs" / str(claim["active_run_id"]) / kind
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(kind, encoding="utf-8")
            artifacts.append(
                ArtifactSpec(
                    artifact_id=f"{job_id}-{index}",
                    kind=kind,
                    path=path.relative_to(work_root).as_posix(),
                )
            )
        barrier = threading.Barrier(2)

        def cancel() -> int:
            barrier.wait()
            return client.post(f"/api/v1/jobs/{job_id}/cancel", headers=TOKEN_HEADER).status_code

        def complete() -> ArtifactCompletionResult:
            barrier.wait()
            return JobRepository(database).complete_job_with_artifacts(
                job_id=job_id,
                run_id=str(claim["active_run_id"]),
                artifacts=artifacts,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancel_future = executor.submit(cancel)
            complete_future = executor.submit(complete)
            cancel_status = cancel_future.result()
            completion = complete_future.result()

        final = repository.get(job_id)
        assert final is not None
        if final["status"] == JobStatus.COMPLETED.value:
            assert cancel_status == 409
            assert completion is ArtifactCompletionResult.COMPLETED
        else:
            assert final["status"] == JobStatus.CANCELLING.value
            assert cancel_status == 200
            assert completion is ArtifactCompletionResult.STALE
