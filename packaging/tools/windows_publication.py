from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Protocol


class WindowsPublicationError(RuntimeError):
    pass


GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_RENAME_INFO_EX = 22
FILE_RENAME_POSIX_SEMANTICS = 0x00000002
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass(frozen=True)
class FileIdentity:
    volume_serial: int
    file_index: int


class WindowsPublicationApi(Protocol):
    def open_parent_chain(self, path: PureWindowsPath) -> list[object]: ...

    def open_source(self, parent: object, name: str) -> object: ...

    def handle_identity(self, handle: object) -> FileIdentity: ...

    def named_identity(self, parent: object, name: str) -> FileIdentity | None: ...

    def rename_handle_exclusive(
        self,
        source: object,
        destination_parent: object,
        destination_name: str,
    ) -> None: ...

    def flush_directory(self, handle: object) -> None: ...

    def close_handle(self, handle: object) -> None: ...


@dataclass(frozen=True)
class _NativePathHandle:
    value: int
    path: PureWindowsPath


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTimeLow", ctypes.c_uint32),
        ("ftCreationTimeHigh", ctypes.c_uint32),
        ("ftLastAccessTimeLow", ctypes.c_uint32),
        ("ftLastAccessTimeHigh", ctypes.c_uint32),
        ("ftLastWriteTimeLow", ctypes.c_uint32),
        ("ftLastWriteTimeHigh", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FileRenameInformation(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_wchar * 1),
    ]


class NativeWindowsPublicationApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsPublicationError("Win32 publication API is unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    @staticmethod
    def _raise_last_error(message: str) -> None:
        error = ctypes.get_last_error()
        raise OSError(error, message)

    def _open_path(
        self,
        path: PureWindowsPath,
        *,
        access: int,
        share: int,
    ) -> _NativePathHandle:
        function = self.kernel32.CreateFileW
        function.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        function.restype = ctypes.c_void_p
        value = function(
            str(path),
            access,
            share,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if not value or int(value) == INVALID_HANDLE_VALUE:
            self._raise_last_error("CreateFileW failed")
        handle = _NativePathHandle(int(value), path)
        information = self._information(handle)
        if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            self.close_handle(handle)
            raise WindowsPublicationError("publication path contains a reparse point")
        return handle

    def _information(self, handle: _NativePathHandle) -> _ByHandleFileInformation:
        information = _ByHandleFileInformation()
        function = self.kernel32.GetFileInformationByHandle
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)]
        function.restype = ctypes.c_int
        if not function(ctypes.c_void_p(handle.value), ctypes.byref(information)):
            self._raise_last_error("GetFileInformationByHandle failed")
        return information

    def open_parent_chain(self, path: PureWindowsPath) -> list[object]:
        _validate_path(path / "_")
        handles: list[object] = []
        current = PureWindowsPath(path.anchor)
        try:
            for part in path.parts[1:]:
                current /= part
                handle = self._open_path(
                    current,
                    access=GENERIC_READ | GENERIC_WRITE | SYNCHRONIZE,
                    share=FILE_SHARE_READ | FILE_SHARE_WRITE,
                )
                information = self._information(handle)
                if not information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
                    self.close_handle(handle)
                    raise WindowsPublicationError("publication parent is not a directory")
                handles.append(handle)
        except Exception:
            for handle in reversed(handles):
                self.close_handle(handle)
            raise
        return handles

    def open_source(self, parent: object, name: str) -> object:
        if not isinstance(parent, _NativePathHandle):
            raise WindowsPublicationError("publication parent handle is invalid")
        return self._open_path(
            parent.path / name,
            access=DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        )

    def handle_identity(self, handle: object) -> FileIdentity:
        if not isinstance(handle, _NativePathHandle):
            raise WindowsPublicationError("publication handle is invalid")
        information = self._information(handle)
        return FileIdentity(
            int(information.dwVolumeSerialNumber),
            (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
        )

    def named_identity(self, parent: object, name: str) -> FileIdentity | None:
        if not isinstance(parent, _NativePathHandle):
            raise WindowsPublicationError("publication parent handle is invalid")
        try:
            handle = self._open_path(
                parent.path / name,
                access=FILE_READ_ATTRIBUTES,
                share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            )
        except OSError as exc:
            if exc.errno in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
                return None
            raise
        try:
            return self.handle_identity(handle)
        finally:
            self.close_handle(handle)

    def rename_handle_exclusive(
        self,
        source: object,
        destination_parent: object,
        destination_name: str,
    ) -> None:
        if not isinstance(source, _NativePathHandle) or not isinstance(
            destination_parent, _NativePathHandle
        ):
            raise WindowsPublicationError("publication handle is invalid")
        destination = destination_parent.path / destination_name
        encoded_name = str(destination).encode("utf-16-le")
        size = ctypes.sizeof(_FileRenameInformation) + len(encoded_name)
        buffer = ctypes.create_string_buffer(size)
        information = _FileRenameInformation.from_buffer(buffer)
        information.Flags = FILE_RENAME_POSIX_SEMANTICS
        information.RootDirectory = None
        information.FileNameLength = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + _FileRenameInformation.FileName.offset,
            encoded_name,
            len(encoded_name),
        )
        function = self.kernel32.SetFileInformationByHandle
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        function.restype = ctypes.c_int
        if not function(
            ctypes.c_void_p(source.value),
            FILE_RENAME_INFO_EX,
            buffer,
            size,
        ):
            self._raise_last_error("SetFileInformationByHandle failed")

    def flush_directory(self, handle: object) -> None:
        if not isinstance(handle, _NativePathHandle):
            raise WindowsPublicationError("publication handle is invalid")
        function = self.kernel32.FlushFileBuffers
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int
        if not function(ctypes.c_void_p(handle.value)):
            self._raise_last_error("FlushFileBuffers failed")

    def close_handle(self, handle: object) -> None:
        if not isinstance(handle, _NativePathHandle):
            raise WindowsPublicationError("publication handle is invalid")
        function = self.kernel32.CloseHandle
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int
        if not function(ctypes.c_void_p(handle.value)):
            self._raise_last_error("CloseHandle failed")


def _validate_path(path: PureWindowsPath) -> None:
    if (
        not path.is_absolute()
        or not path.drive
        or path.anchor.startswith("\\\\")
        or path.name in {"", ".", ".."}
        or ":" in path.name
    ):
        raise WindowsPublicationError("publication path is unsafe")


def rename_exclusive(
    source: PureWindowsPath,
    destination: PureWindowsPath,
    api: WindowsPublicationApi,
    *,
    before_effect: Callable[[], None] | None = None,
) -> None:
    _validate_path(source)
    _validate_path(destination)
    if source == destination:
        raise WindowsPublicationError("publication paths must differ")
    source_chain: list[object] = []
    destination_chain: list[object] = []
    source_handle: object | None = None
    operation_error: Exception | None = None
    try:
        source_chain = api.open_parent_chain(source.parent)
        destination_chain = api.open_parent_chain(destination.parent)
        if not source_chain or not destination_chain:
            raise WindowsPublicationError("publication parent is unavailable")
        source_parent = source_chain[-1]
        destination_parent = destination_chain[-1]
        source_handle = api.open_source(source_parent, source.name)
        source_identity = api.handle_identity(source_handle)
        if api.named_identity(source_parent, source.name) != source_identity:
            raise WindowsPublicationError("publication source identity changed")
        if api.named_identity(destination_parent, destination.name) is not None:
            raise WindowsPublicationError("publication destination is occupied")
        if before_effect is not None:
            before_effect()
        if api.named_identity(source_parent, source.name) != source_identity:
            raise WindowsPublicationError("publication source identity changed")
        if api.named_identity(destination_parent, destination.name) is not None:
            raise WindowsPublicationError("publication destination is occupied")
        try:
            api.rename_handle_exclusive(
                source_handle,
                destination_parent,
                destination.name,
            )
        except Exception as exc:
            raise WindowsPublicationError("handle-bound publication rename failed") from exc
        if api.named_identity(destination_parent, destination.name) != source_identity:
            raise WindowsPublicationError("publication destination identity changed")
        if api.named_identity(source_parent, source.name) is not None:
            raise WindowsPublicationError("publication source still exists")
        api.flush_directory(destination_parent)
        if source.parent != destination.parent:
            api.flush_directory(source_parent)
    except Exception as exc:
        operation_error = exc
    cleanup_errors: list[Exception] = []
    resources = [
        source_handle,
        *reversed(destination_chain),
        *reversed(source_chain),
    ]
    for handle in resources:
        if handle is None:
            continue
        try:
            api.close_handle(handle)
        except Exception as cleanup_error:
            cleanup_errors.append(cleanup_error)
    if operation_error is not None:
        if cleanup_errors:
            raise ExceptionGroup(
                "publication and handle cleanup failed",
                [operation_error, *cleanup_errors],
            ) from operation_error
        raise operation_error
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        raise ExceptionGroup("publication handle cleanup failed", cleanup_errors)
