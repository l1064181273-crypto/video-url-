from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from lvt.db.repository import DeleteHooks, JobRepository


class UnsafeJobPathError(ValueError):
    pass


class JobFileStore:
    def __init__(self, work_root: Path) -> None:
        self.work_root = work_root.expanduser().resolve()

    def ensure_root(self) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)

    def reconcile_deletions(self, repository: JobRepository) -> None:
        for quarantine in self.work_root.glob(".deleting-*-*"):
            parsed = self._parse_quarantine_name(quarantine.name)
            if parsed is None:
                continue
            job_id = parsed
            if quarantine.is_symlink() or not quarantine.is_dir():
                raise UnsafeJobPathError("delete quarantine must be a directory")
            self._assert_tree_has_no_symlinks(quarantine)
            job_root = self._job_root(job_id)
            if repository.get(job_id) is None:
                shutil.rmtree(quarantine)
            elif job_root.exists():
                raise UnsafeJobPathError("job and delete quarantine both exist")
            else:
                quarantine.rename(job_root)

    def open_artifact(
        self,
        *,
        job_id: str,
        kind: str,
        relative_path: str,
    ) -> BinaryIO:
        path = self._validated_path(job_id, relative_path, require_exists=True)
        if path.name != kind:
            raise UnsafeJobPathError("artifact path does not match its kind")
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise FileNotFoundError("artifact is not a regular file")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.fdopen(os.open(path, flags), "rb")

    def prepare_delete(
        self,
        *,
        job: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> DeleteHooks:
        job_id = str(job["uuid"])
        for artifact in artifacts:
            path = self._validated_path(
                job_id,
                str(artifact["path"]),
                require_exists=False,
            )
            if path.name != artifact["kind"]:
                raise UnsafeJobPathError("artifact path does not match its kind")
        checkpoint_pointer = job.get("checkpoint_pointer")
        if checkpoint_pointer:
            self._validated_path(job_id, str(checkpoint_pointer), require_exists=False)
        work_dir = job.get("work_dir")
        if work_dir:
            self._validated_work_dir(job_id, str(work_dir))

        job_root = self._job_root(job_id)
        if not job_root.exists():
            return DeleteHooks(rollback=lambda: None, finalize=lambda: None)
        if job_root.is_symlink() or not job_root.is_dir():
            raise UnsafeJobPathError("job root must be a non-symlink directory")

        quarantine = self.work_root / f".deleting-{job_id}-{uuid.uuid4().hex}"
        job_root.rename(quarantine)
        try:
            self._assert_tree_has_no_symlinks(quarantine)
        except BaseException:
            quarantine.rename(job_root)
            raise

        def rollback() -> None:
            if quarantine.exists() and not job_root.exists():
                quarantine.rename(job_root)

        def finalize() -> None:
            if quarantine.exists():
                shutil.rmtree(quarantine)

        return DeleteHooks(rollback=rollback, finalize=finalize)

    def _validated_path(
        self,
        job_id: str,
        relative_path: str,
        *,
        require_exists: bool,
    ) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts:
            raise UnsafeJobPathError("stored path must be relative")
        if relative.parts[0] != job_id or any(part in {"", ".", ".."} for part in relative.parts):
            raise UnsafeJobPathError("stored path escapes job root")
        current = self.work_root
        for index, part in enumerate(relative.parts):
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if require_exists:
                    raise
                if index == 0:
                    return current.joinpath(*relative.parts[index + 1 :])
                return current.joinpath(*relative.parts[index + 1 :])
            if stat.S_ISLNK(mode):
                raise UnsafeJobPathError("stored path contains a symlink")
        return current

    def _job_root(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or job_id in {".", ".."}:
            raise UnsafeJobPathError("invalid job id")
        return self.work_root / job_id

    def _validated_work_dir(self, job_id: str, stored_path: str) -> Path:
        path = Path(stored_path).expanduser()
        if path.is_absolute():
            try:
                relative = path.relative_to(self.work_root)
            except ValueError as exc:
                raise UnsafeJobPathError("work directory escapes work root") from exc
            return self._validated_path(
                job_id,
                PurePosixPath(*relative.parts).as_posix(),
                require_exists=False,
            )
        relative_path = PurePosixPath(stored_path)
        if relative_path.parts and relative_path.parts[0] in {
            "work",
            self.work_root.name,
        }:
            relative_path = PurePosixPath(*relative_path.parts[1:])
        return self._validated_path(
            job_id,
            relative_path.as_posix(),
            require_exists=False,
        )

    @staticmethod
    def _parse_quarantine_name(name: str) -> str | None:
        prefix = ".deleting-"
        if not name.startswith(prefix):
            return None
        job_id, separator, nonce = name[len(prefix) :].rpartition("-")
        if (
            not separator
            or not job_id
            or len(nonce) != 32
            or any(character not in "0123456789abcdef" for character in nonce)
        ):
            return None
        return job_id

    @staticmethod
    def _assert_tree_has_no_symlinks(root: Path) -> None:
        for directory, directories, files in os.walk(root, followlinks=False):
            base = Path(directory)
            for name in [*directories, *files]:
                if stat.S_ISLNK((base / name).lstat().st_mode):
                    raise UnsafeJobPathError("job tree contains a symlink")
