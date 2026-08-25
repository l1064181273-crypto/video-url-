#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Protocol, TypedDict, cast

OPERATIONS = {"install", "start", "stop", "upgrade", "uninstall"}


class LockBusyError(RuntimeError):
    pass


class LockUnsafeError(RuntimeError):
    pass


class ProcessInspector(Protocol):
    def identity(self, pid: int) -> str | None: ...


class OwnerMetadata(TypedDict):
    schema_version: int
    pid: int
    start_time: str
    nonce: str
    operation: str


class SystemProcessInspector:
    def identity(self, pid: int) -> str | None:
        if type(pid) is not int or pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return None
        value = " ".join(completed.stdout.split())
        return value or None


class LifecycleLock:
    def __init__(
        self,
        application_parent: Path,
        *,
        operation: str,
        process_inspector: ProcessInspector | None = None,
        nonce: str | None = None,
    ) -> None:
        if operation not in OPERATIONS:
            raise ValueError("unsupported lifecycle operation")
        self.application_parent = application_parent
        self.operation = operation
        self.process_inspector = process_inspector or SystemProcessInspector()
        self.nonce = nonce or uuid.uuid4().hex
        self.lifecycle_root = application_parent / ".LocalVideoTranscriber.lifecycle"
        self.lock_path = self.lifecycle_root / "lock"
        self.bootstrap_path = self.lifecycle_root / "bootstrap.lock"
        self._lock_fd: int | None = None
        self._bootstrap_owned = False
        self._start_time = self.process_inspector.identity(os.getpid())
        if self._start_time is None:
            raise LockUnsafeError("unable to establish process identity")

    @property
    def lock_fd(self) -> int | None:
        return self._lock_fd

    @property
    def bootstrap_owned(self) -> bool:
        return self._bootstrap_owned

    def acquire_bootstrap(self) -> None:
        self._prepare_lifecycle_root()
        candidate = self.lifecycle_root / f".bootstrap.{self.nonce}.tmp"
        if candidate.exists():
            raise LockUnsafeError("bootstrap candidate already exists")
        candidate.mkdir(mode=0o700)
        try:
            self._write_owner(candidate / "owner.json")
            _fsync_directory(candidate)
            try:
                candidate.rename(self.bootstrap_path)
            except OSError as exc:
                if not self.bootstrap_path.exists():
                    raise LockUnsafeError("cannot publish bootstrap lease") from exc
                self._handle_existing_bootstrap()
                try:
                    candidate.rename(self.bootstrap_path)
                except OSError as retry_exc:
                    if self.bootstrap_path.exists():
                        raise LockBusyError("bootstrap lease is held") from retry_exc
                    raise LockUnsafeError("cannot publish bootstrap lease") from retry_exc
            _fsync_directory(self.lifecycle_root)
            self._bootstrap_owned = True
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)

    def acquire_flock(self, *, blocking: bool = False) -> None:
        self._prepare_lifecycle_root()
        if self.lock_path.is_symlink():
            raise LockUnsafeError("lifecycle lock cannot be a symlink")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise LockUnsafeError("cannot open lifecycle lock") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise LockUnsafeError("lifecycle lock is not a regular file")
            os.fchmod(fd, 0o600)
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, operation)
            except BlockingIOError as exc:
                raise LockBusyError("lifecycle lock is held") from exc
            os.set_inheritable(fd, False)
            self._lock_fd = fd
        except Exception:
            os.close(fd)
            raise

    def acquire_bootstrap_then_flock(self) -> None:
        self.acquire_bootstrap()
        try:
            self.acquire_flock()
        except Exception:
            self.release_bootstrap()
            raise
        self.release_bootstrap()

    def release_bootstrap(self) -> None:
        if not self._bootstrap_owned:
            return
        metadata = self._read_owner(self.bootstrap_path)
        if not self._metadata_matches_owner(metadata):
            raise LockUnsafeError("bootstrap ownership changed")
        tombstone = self.lifecycle_root / f".bootstrap.release.{self.nonce}"
        try:
            self.bootstrap_path.rename(tombstone)
        except OSError as exc:
            raise LockUnsafeError("cannot release bootstrap lease") from exc
        _fsync_directory(self.lifecycle_root)
        shutil.rmtree(tombstone)
        _fsync_directory(self.lifecycle_root)
        self._bootstrap_owned = False

    def close(self) -> None:
        bootstrap_error: Exception | None = None
        try:
            if self._bootstrap_owned:
                self.release_bootstrap()
        except Exception as exc:
            bootstrap_error = exc
        finally:
            if self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None
        if bootstrap_error is not None:
            raise bootstrap_error

    def __enter__(self) -> LifecycleLock:
        self.acquire_flock(blocking=True)
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def _prepare_lifecycle_root(self) -> None:
        if not self.application_parent.is_absolute():
            raise LockUnsafeError("application parent must be absolute")
        if not self.application_parent.exists() or not self.application_parent.is_dir():
            raise LockUnsafeError("application parent is unsafe")
        if _has_symlink_component(self.application_parent):
            raise LockUnsafeError("application parent contains a symlink")
        resolved_parent = self.application_parent.resolve(strict=True)
        created = False
        try:
            self.lifecycle_root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        if self.lifecycle_root.is_symlink() or not self.lifecycle_root.is_dir():
            raise LockUnsafeError("lifecycle root is unsafe")
        self.lifecycle_root.resolve(strict=True).relative_to(resolved_parent)
        if created:
            _fsync_directory(resolved_parent)
        self.lifecycle_root.chmod(0o700)

    def _write_owner(self, path: Path) -> None:
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "start_time": self._start_time,
            "nonce": self.nonce,
            "operation": self.operation,
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _handle_existing_bootstrap(self) -> None:
        metadata = self._read_owner(self.bootstrap_path)
        pid = metadata["pid"]
        recorded_start = metadata["start_time"]
        current_start = self.process_inspector.identity(pid)
        if current_start == recorded_start:
            raise LockBusyError("bootstrap lease is held")
        tombstone = self.lifecycle_root / f".bootstrap.stale.{uuid.uuid4().hex}"
        try:
            self.bootstrap_path.rename(tombstone)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LockUnsafeError("cannot isolate stale bootstrap lease") from exc
        _fsync_directory(self.lifecycle_root)
        shutil.rmtree(tombstone)
        _fsync_directory(self.lifecycle_root)

    def _read_owner(self, bootstrap: Path) -> OwnerMetadata:
        try:
            if bootstrap.is_symlink() or not bootstrap.is_dir():
                raise ValueError
            bootstrap_metadata = bootstrap.stat()
            if (
                bootstrap_metadata.st_uid != os.getuid()
                or bootstrap_metadata.st_mode & 0o777 != 0o700
            ):
                raise ValueError
            owner = bootstrap / "owner.json"
            metadata = owner.lstat()
            if owner.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o600:
                raise ValueError
            payload = json.loads(owner.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or type(payload.get("pid")) is not int
                or not isinstance(payload.get("start_time"), str)
                or not payload["start_time"]
                or not isinstance(payload.get("nonce"), str)
                or not payload["nonce"]
                or payload.get("operation") not in OPERATIONS
            ):
                raise ValueError
            return cast(OwnerMetadata, payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise LockUnsafeError("bootstrap metadata is invalid") from exc

    def _metadata_matches_owner(self, metadata: OwnerMetadata) -> bool:
        return (
            metadata["pid"] == os.getpid()
            and metadata["start_time"] == self._start_time
            and metadata["nonce"] == self.nonce
            and metadata["operation"] == self.operation
        )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在应用生命周期锁下运行命令")
    parser.add_argument("--application-parent", required=True, type=Path)
    parser.add_argument("--operation", required=True, choices=sorted(OPERATIONS))
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.command and arguments.command[0] == "--":
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("a command is required")
    lock = LifecycleLock(arguments.application_parent, operation=arguments.operation)
    try:
        if arguments.bootstrap:
            lock.acquire_bootstrap_then_flock()
        else:
            lock.acquire_flock(blocking=True)
        completed = subprocess.run(arguments.command, close_fds=True, check=False)
        return completed.returncode
    except LockBusyError:
        return 1
    except LockUnsafeError:
        return 2
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
