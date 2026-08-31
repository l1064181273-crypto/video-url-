from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from lvt.db.repository import DeleteHooks, JobRepository


class UnsafeJobPathError(ValueError):
    pass


class JobFileStore:
    def __init__(self, work_root: Path) -> None:
        self.work_root = work_root.expanduser().resolve()
        self.artifact_open_hook: Callable[[], None] | None = None

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
        checkpoint_pointer: str,
    ) -> BinaryIO:
        manifest_parts = self._relative_parts(job_id, checkpoint_pointer)
        if (
            len(manifest_parts) != 5
            or manifest_parts[1] != "runs"
            or manifest_parts[3] != "export_manifest"
            or manifest_parts[4] != "manifest.json"
        ):
            raise UnsafeJobPathError("checkpoint pointer is not an export manifest")
        artifact_parts = self._relative_parts(job_id, relative_path)
        stage_parts = manifest_parts[:-1]
        if (
            len(artifact_parts) <= len(stage_parts)
            or artifact_parts[: len(stage_parts)] != stage_parts
            or artifact_parts[-1] != kind
        ):
            raise UnsafeJobPathError("artifact is outside the completed export stage")
        if sys.platform == "win32":
            return self._open_artifact_windows(
                job_id=job_id,
                kind=kind,
                relative_path=relative_path,
                manifest_parts=manifest_parts,
                artifact_parts=artifact_parts,
            )

        root_fd = self._open_root_fd()
        stage_fd = -1
        artifact_parent_fd = -1
        try:
            stage_fd = self._open_directory_chain(root_fd, stage_parts)
            self._require_regular_at(stage_fd, ".published")
            manifest = self._read_json_at(stage_fd, "manifest.json")
            manifest_output = self._manifest_output(manifest, kind, relative_path)
            if (
                manifest.get("job_id") != job_id
                or manifest.get("run_id") != manifest_parts[2]
                or manifest.get("stage") != "export_manifest"
                or manifest_output is None
            ):
                raise UnsafeJobPathError("artifact does not match export manifest")

            artifact_subparts = artifact_parts[len(stage_parts) :]
            artifact_parent_fd = self._open_directory_chain(
                stage_fd,
                artifact_subparts[:-1],
            )
            if self.artifact_open_hook is not None:
                self.artifact_open_hook()
            verification_fd = self._open_directory_chain(root_fd, stage_parts)
            try:
                verification_stat = os.fstat(verification_fd)
                stage_stat = os.fstat(stage_fd)
                if (verification_stat.st_dev, verification_stat.st_ino) != (
                    stage_stat.st_dev,
                    stage_stat.st_ino,
                ):
                    raise UnsafeJobPathError("export stage directory was replaced")
            finally:
                os.close(verification_fd)
            verification_parent_fd = self._open_directory_chain(
                stage_fd,
                artifact_subparts[:-1],
            )
            try:
                if self._inode(verification_parent_fd) != self._inode(artifact_parent_fd):
                    raise UnsafeJobPathError("artifact parent directory was replaced")
            finally:
                os.close(verification_parent_fd)

            return self._open_verified_file(
                artifact_parent_fd,
                artifact_subparts[-1],
                manifest_output,
            )
        finally:
            if artifact_parent_fd >= 0:
                os.close(artifact_parent_fd)
            if stage_fd >= 0:
                os.close(stage_fd)
            os.close(root_fd)

    def _open_artifact_windows(
        self,
        *,
        job_id: str,
        kind: str,
        relative_path: str,
        manifest_parts: tuple[str, ...],
        artifact_parts: tuple[str, ...],
    ) -> BinaryIO:
        stage_parts = manifest_parts[:-1]
        artifact_subparts = artifact_parts[len(stage_parts) :]
        stage = self.work_root.joinpath(*stage_parts)
        artifact_parent = stage.joinpath(*artifact_subparts[:-1])
        root_identity = self._windows_directory_identity(self.work_root)
        self._windows_validate_chain(self.work_root, stage_parts, directories_only=True)
        stage_identity = self._windows_directory_identity(stage)
        self._windows_require_regular(stage / ".published")
        manifest = self._windows_read_json(stage / "manifest.json")
        if self._windows_directory_identity(self.work_root) != root_identity:
            raise UnsafeJobPathError("work root directory was replaced")
        if self._windows_directory_identity(stage) != stage_identity:
            raise UnsafeJobPathError("export stage directory was replaced")
        manifest_output = self._manifest_output(manifest, kind, relative_path)
        if (
            manifest.get("job_id") != job_id
            or manifest.get("run_id") != manifest_parts[2]
            or manifest.get("stage") != "export_manifest"
            or manifest_output is None
        ):
            raise UnsafeJobPathError("artifact does not match export manifest")
        self._windows_validate_chain(
            stage,
            artifact_subparts[:-1],
            directories_only=True,
        )
        parent_identity = self._windows_directory_identity(artifact_parent)
        if self.artifact_open_hook is not None:
            self.artifact_open_hook()
        if self._windows_directory_identity(self.work_root) != root_identity:
            raise UnsafeJobPathError("work root directory was replaced")
        if self._windows_directory_identity(stage) != stage_identity:
            raise UnsafeJobPathError("export stage directory was replaced")
        if self._windows_directory_identity(artifact_parent) != parent_identity:
            raise UnsafeJobPathError("artifact parent directory was replaced")
        stream = self._windows_open_verified_file(
            artifact_parent / artifact_subparts[-1],
            manifest_output,
        )
        try:
            if self._windows_directory_identity(self.work_root) != root_identity:
                raise UnsafeJobPathError("work root directory was replaced")
            if self._windows_directory_identity(stage) != stage_identity:
                raise UnsafeJobPathError("export stage directory was replaced")
            if self._windows_directory_identity(artifact_parent) != parent_identity:
                raise UnsafeJobPathError("artifact parent directory was replaced")
        except BaseException:
            stream.close()
            raise
        return stream

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

    @staticmethod
    def _windows_identity(path: Path, *, directory: bool) -> tuple[int, int, int, int]:
        metadata = path.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if (
            path.is_symlink()
            or attributes & 0x400
            or (directory and not stat.S_ISDIR(metadata.st_mode))
            or (not directory and not stat.S_ISREG(metadata.st_mode))
        ):
            raise UnsafeJobPathError("artifact path contains an unsafe entry")
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    @classmethod
    def _windows_directory_identity(cls, path: Path) -> tuple[int, int]:
        identity = cls._windows_identity(path, directory=True)
        return identity[0], identity[1]

    @classmethod
    def _windows_validate_chain(
        cls,
        root: Path,
        parts: tuple[str, ...],
        *,
        directories_only: bool,
    ) -> None:
        cls._windows_identity(root, directory=True)
        current = root
        for index, part in enumerate(parts):
            current /= part
            cls._windows_identity(
                current,
                directory=directories_only or index < len(parts) - 1,
            )

    @classmethod
    def _windows_require_regular(cls, path: Path) -> None:
        cls._windows_identity(path, directory=False)

    @classmethod
    def _windows_read_json(cls, path: Path) -> dict[str, Any]:
        identity = cls._windows_identity(path, directory=False)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        try:
            if cls._descriptor_identity(descriptor) != identity:
                raise UnsafeJobPathError("manifest changed during open")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                value = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, dict):
            raise UnsafeJobPathError("manifest must be an object")
        return value

    @classmethod
    def _windows_open_verified_file(
        cls,
        path: Path,
        manifest_output: dict[str, Any],
    ) -> BinaryIO:
        named_before = cls._windows_identity(path, directory=False)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        try:
            opened = cls._descriptor_identity(descriptor)
            named_after = cls._windows_identity(path, directory=False)
            if opened != named_before or named_after != named_before:
                raise UnsafeJobPathError("artifact changed during open")
            expected_size = manifest_output.get("byte_size")
            expected_sha256 = manifest_output.get("sha256")
            if (
                type(expected_size) is not int
                or expected_size < 0
                or not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
                or opened[2] != expected_size
            ):
                raise UnsafeJobPathError("artifact manifest integrity fields are invalid")
            stream = os.fdopen(descriptor, "rb")
            descriptor = -1
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                stream.close()
                raise UnsafeJobPathError("artifact hash does not match export manifest")
            stream.seek(0)
            return stream
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _descriptor_identity(descriptor: int) -> tuple[int, int, int, int]:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeJobPathError("artifact is not a regular file")
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    def _open_root_fd(self) -> int:
        return os.open(
            self.work_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )

    @staticmethod
    def _open_directory_chain(base_fd: int, parts: tuple[str, ...]) -> int:
        current_fd = os.dup(base_fd)
        try:
            for part in parts:
                next_fd = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _open_verified_file(
        directory_fd: int,
        name: str,
        manifest_output: dict[str, Any],
    ) -> BinaryIO:
        file_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnsafeJobPathError("artifact is not a regular file")
            expected_size = manifest_output.get("byte_size")
            expected_sha256 = manifest_output.get("sha256")
            if (
                type(expected_size) is not int
                or expected_size < 0
                or not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
            ):
                raise UnsafeJobPathError("artifact manifest integrity fields are invalid")
            if file_stat.st_size != expected_size:
                raise UnsafeJobPathError("artifact size does not match export manifest")
            stream = os.fdopen(file_fd, "rb")
            file_fd = -1
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                stream.close()
                raise UnsafeJobPathError("artifact hash does not match export manifest")
            stream.seek(0)
            return stream
        finally:
            if file_fd >= 0:
                os.close(file_fd)

    @staticmethod
    def _inode(descriptor: int) -> tuple[int, int]:
        file_stat = os.fstat(descriptor)
        return file_stat.st_dev, file_stat.st_ino

    @staticmethod
    def _require_regular_at(directory_fd: int, name: str) -> None:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafeJobPathError("checkpoint file is not regular")
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_json_at(directory_fd: int, name: str) -> dict[str, Any]:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UnsafeJobPathError("manifest is not a regular file")
            value = json.load(handle)
        if not isinstance(value, dict):
            raise UnsafeJobPathError("manifest must be an object")
        return value

    @staticmethod
    def _manifest_output(
        manifest: dict[str, Any],
        kind: str,
        relative_path: str,
    ) -> dict[str, Any] | None:
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list):
            return None
        for output in outputs:
            if (
                isinstance(output, dict)
                and output.get("kind") == kind
                and output.get("relative_path") == relative_path
            ):
                return output
        return None

    @staticmethod
    def _relative_parts(job_id: str, relative_path: str) -> tuple[str, ...]:
        relative = PurePosixPath(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.parts[0] != job_id
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise UnsafeJobPathError("stored path escapes job root")
        return relative.parts

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
