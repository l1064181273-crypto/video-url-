from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ModuleNotFoundError:
    msvcrt = None  # type: ignore[assignment]

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class InstanceAlreadyRunningError(RuntimeError):
    pass


def _path_is_link_like(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _lock_descriptor(fd: int) -> None:
    if sys.platform == "win32":
        if msvcrt is None:
            raise RuntimeError("Windows file locking is unavailable")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise InstanceAlreadyRunningError("Local Video Transcriber is already running") from exc
        return
    if fcntl is None:
        raise RuntimeError("POSIX file locking is unavailable")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise InstanceAlreadyRunningError("Local Video Transcriber is already running") from exc


def _unlock_descriptor(fd: int) -> None:
    if sys.platform == "win32":
        if msvcrt is None:
            raise RuntimeError("Windows file locking is unavailable")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    if fcntl is None:
        raise RuntimeError("POSIX file locking is unavailable")
    fcntl.flock(fd, fcntl.LOCK_UN)


class ProcessInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("instance lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if _path_is_link_like(self.path):
            raise RuntimeError("instance lock path is unsafe")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError("instance lock path is unsafe")
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            _lock_descriptor(fd)
            os.ftruncate(fd, 0)
            content = f"pid={os.getpid()}\n".encode("ascii")
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise RuntimeError("instance lock write made no progress")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock_descriptor(fd)
        finally:
            os.close(fd)
