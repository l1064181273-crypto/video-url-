from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from lvt.api.control import JobFileStore, UnsafeJobPathError


def _completed_export(
    tmp_path: Path,
) -> tuple[JobFileStore, dict[str, Any]]:
    job_id = "job-1"
    run_id = "run-1"
    kind = "source.txt"
    work_root = tmp_path / "work"
    stage = work_root / job_id / "runs" / run_id / "export_manifest"
    artifact = stage / "exports" / kind
    artifact.parent.mkdir(parents=True)
    data = b"verified artifact"
    artifact.write_bytes(data)
    relative_path = artifact.relative_to(work_root).as_posix()
    checkpoint_pointer = (
        stage.relative_to(work_root) / "manifest.json"
    ).as_posix()
    (stage / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "run_id": run_id,
                "stage": "export_manifest",
                "outputs": [
                    {
                        "kind": kind,
                        "relative_path": relative_path,
                        "byte_size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (stage / ".published").write_text("published", encoding="utf-8")
    return JobFileStore(work_root), {
        "job_id": job_id,
        "kind": kind,
        "relative_path": relative_path,
        "manifest_parts": PurePosixPath(checkpoint_pointer).parts,
        "artifact_parts": PurePosixPath(relative_path).parts,
        "stage": stage,
        "artifact": artifact,
        "data": data,
    }


def _open_windows(store: JobFileStore, export: dict[str, Any]) -> Any:
    return store._open_artifact_windows(
        job_id=export["job_id"],
        kind=export["kind"],
        relative_path=export["relative_path"],
        manifest_parts=export["manifest_parts"],
        artifact_parts=export["artifact_parts"],
    )


def test_windows_artifact_open_returns_verified_stream(tmp_path: Path) -> None:
    store, export = _completed_export(tmp_path)

    with _open_windows(store, export) as stream:
        assert stream.read() == export["data"]


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows dispatch")
def test_windows_public_artifact_open_uses_native_branch(tmp_path: Path) -> None:
    store, export = _completed_export(tmp_path)
    checkpoint_pointer = "/".join(export["manifest_parts"])

    with store.open_artifact(
        job_id=export["job_id"],
        kind=export["kind"],
        relative_path=export["relative_path"],
        checkpoint_pointer=checkpoint_pointer,
    ) as stream:
        assert stream.read() == export["data"]


def test_windows_artifact_open_rejects_hash_mismatch(tmp_path: Path) -> None:
    store, export = _completed_export(tmp_path)
    export["artifact"].write_bytes(b"tampered artifact")

    with pytest.raises(UnsafeJobPathError, match="hash does not match"):
        _open_windows(store, export)


def test_windows_artifact_open_rejects_stage_replaced_during_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, export = _completed_export(tmp_path)
    stage: Path = export["stage"]
    backup = stage.with_name("export_manifest.original")
    original_read = store._windows_read_json

    def replace_stage(path: Path) -> dict[str, Any]:
        manifest = original_read(path)
        stage.rename(backup)
        shutil.copytree(backup, stage)
        return manifest

    monkeypatch.setattr(store, "_windows_read_json", replace_stage)

    with pytest.raises(UnsafeJobPathError, match="stage directory was replaced"):
        _open_windows(store, export)


def test_windows_artifact_open_rejects_parent_replacement_before_file_open(
    tmp_path: Path,
) -> None:
    store, export = _completed_export(tmp_path)
    artifact_parent: Path = export["artifact"].parent
    backup = artifact_parent.with_name("exports.original")

    def replace_parent() -> None:
        artifact_parent.rename(backup)
        shutil.copytree(backup, artifact_parent)

    store.artifact_open_hook = replace_parent

    with pytest.raises(UnsafeJobPathError, match="parent directory was replaced"):
        _open_windows(store, export)
