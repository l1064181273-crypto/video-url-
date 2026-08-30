#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Protocol, TypedDict, cast

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ModuleNotFoundError:
    msvcrt = None  # type: ignore[assignment]

OPERATIONS = {"install", "start", "stop", "upgrade", "uninstall"}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class LockBusyError(RuntimeError):
    pass


class LockUnsafeError(RuntimeError):
    pass


def path_is_link_like(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


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
        if sys.platform == "win32":
            return _windows_process_identity(pid)
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
        if path_is_link_like(self.lock_path):
            raise LockUnsafeError("lifecycle lock cannot be a symlink")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
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
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            _lock_descriptor(fd, blocking=blocking)
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
                _unlock_descriptor(self._lock_fd)
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
        if path_is_link_like(self.lifecycle_root) or not self.lifecycle_root.is_dir():
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
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
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
            if path_is_link_like(bootstrap) or not bootstrap.is_dir():
                raise ValueError
            bootstrap_metadata = bootstrap.stat()
            if not _owned_by_current_user(bootstrap_metadata) or (
                sys.platform != "win32" and bootstrap_metadata.st_mode & 0o777 != 0o700
            ):
                raise ValueError
            owner = bootstrap / "owner.json"
            metadata = owner.lstat()
            if (
                path_is_link_like(owner)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise ValueError
            if not _owned_by_current_user(metadata) or (
                sys.platform != "win32" and metadata.st_mode & 0o777 != 0o600
            ):
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
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    return getuid is None or metadata.st_uid == getuid()


def _lock_descriptor(fd: int, *, blocking: bool) -> None:
    if sys.platform == "win32":
        if msvcrt is None:
            raise LockUnsafeError("Windows file locking is unavailable")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            raise LockBusyError("lifecycle lock is held") from exc
        return
    if fcntl is None:
        raise LockUnsafeError("POSIX file locking is unavailable")
    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, operation)
    except BlockingIOError as exc:
        raise LockBusyError("lifecycle lock is held") from exc


def _unlock_descriptor(fd: int) -> None:
    if sys.platform == "win32":
        if msvcrt is None:
            raise LockUnsafeError("Windows file locking is unavailable")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is None:
        raise LockUnsafeError("POSIX file locking is unavailable")
    fcntl.flock(fd, fcntl.LOCK_UN)


def _windows_process_identity(pid: int) -> str | None:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    get_process_times.restype = ctypes.c_int
    handle = open_process(0x1000, 0, pid)
    if not handle:
        return None
    creation = ctypes.c_uint64()
    exit_time = ctypes.c_uint64()
    kernel_time = ctypes.c_uint64()
    user_time = ctypes.c_uint64()
    try:
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return f"{creation.value:016x}"
    finally:
        close_handle(handle)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if path_is_link_like(current):
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
