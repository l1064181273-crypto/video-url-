from __future__ import annotations

import ctypes
import hashlib
import ntpath
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Protocol

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_BEGIN = 0
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_INSUFFICIENT_BUFFER = 122
AF_INET = 2
TCP_TABLE_OWNER_PID_LISTENER = 3
MIB_TCP_STATE_LISTEN = 2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class WindowsProcessError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutableIdentity:
    path: str
    volume_serial: int
    file_index: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "volume_serial": self.volume_serial,
            "file_index": self.file_index,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_time: int
    executable: ExecutableIdentity

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "creation_time": self.creation_time,
            "executable": self.executable.as_dict(),
        }


class WindowsProcessApi(Protocol):
    def open_process(self, pid: int, *, terminate: bool) -> object: ...

    def close_handle(self, handle: object) -> None: ...

    def process_creation_time(self, handle: object) -> int: ...

    def process_image_path(self, handle: object) -> str: ...

    def open_executable(self, path: str) -> object: ...

    def file_identity(self, handle: object) -> tuple[int, int]: ...

    def sha256_file(self, handle: object) -> str: ...

    def terminate_process(self, handle: object, exit_code: int) -> None: ...

    def wait_process(self, handle: object, timeout_ms: int) -> bool: ...

    def process_exit_code(self, handle: object) -> int: ...

    def listener_pids(self, port: int) -> set[int]: ...


@dataclass
class OpenedVerifiedProcess:
    api: WindowsProcessApi
    process_handle: object
    executable_handle: object
    identity: ProcessIdentity
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.api.close_handle(self.executable_handle)
        finally:
            self.api.close_handle(self.process_handle)
            self.closed = True

    def __enter__(self) -> OpenedVerifiedProcess:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _normalized_windows_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def process_identity_from_dict(payload: Any) -> ProcessIdentity:
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "pid",
            "creation_time",
            "executable",
        }:
            raise ValueError
        executable = payload["executable"]
        if not isinstance(executable, dict) or set(executable) != {
            "path",
            "volume_serial",
            "file_index",
            "sha256",
        }:
            raise ValueError
        pid = payload["pid"]
        creation_time = payload["creation_time"]
        path = executable["path"]
        volume_serial = executable["volume_serial"]
        file_index = executable["file_index"]
        sha256 = executable["sha256"]
        windows_path = PureWindowsPath(path)
        if (
            type(pid) is not int
            or pid <= 0
            or type(creation_time) is not int
            or creation_time <= 0
            or not isinstance(path, str)
            or not path
            or "\x00" in path
            or not windows_path.is_absolute()
            or not windows_path.drive
            or windows_path.anchor.startswith("\\\\")
            or type(volume_serial) is not int
            or volume_serial < 0
            or type(file_index) is not int
            or file_index <= 0
            or not isinstance(sha256, str)
            or not _valid_sha256(sha256)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise WindowsProcessError("process identity is invalid") from exc
    return ProcessIdentity(
        pid=pid,
        creation_time=creation_time,
        executable=ExecutableIdentity(
            path=path,
            volume_serial=volume_serial,
            file_index=file_index,
            sha256=sha256,
        ),
    )


def capture_process_identity(
    pid: int,
    process_handle: object,
    api: WindowsProcessApi,
) -> ProcessIdentity:
    if type(pid) is not int or pid <= 0:
        raise WindowsProcessError("process identity is invalid")
    creation_time = api.process_creation_time(process_handle)
    image_path = api.process_image_path(process_handle)
    executable_handle = api.open_executable(image_path)
    try:
        volume_serial, file_index = api.file_identity(executable_handle)
        digest = api.sha256_file(executable_handle)
    finally:
        api.close_handle(executable_handle)
    payload = {
        "pid": pid,
        "creation_time": creation_time,
        "executable": {
            "path": image_path,
            "volume_serial": volume_serial,
            "file_index": file_index,
            "sha256": digest,
        },
    }
    return process_identity_from_dict(payload)


def open_verified_process(
    expected: ProcessIdentity,
    api: WindowsProcessApi,
    *,
    terminate: bool = False,
) -> OpenedVerifiedProcess:
    if (
        type(expected.pid) is not int
        or expected.pid <= 0
        or type(expected.creation_time) is not int
        or expected.creation_time <= 0
        or not _valid_sha256(expected.executable.sha256)
    ):
        raise WindowsProcessError("process identity is invalid")
    process_handle = api.open_process(expected.pid, terminate=terminate)
    executable_handle: object | None = None
    try:
        if api.process_creation_time(process_handle) != expected.creation_time:
            raise WindowsProcessError("process creation time changed")
        observed_path = api.process_image_path(process_handle)
        if _normalized_windows_path(observed_path) != _normalized_windows_path(
            expected.executable.path
        ):
            raise WindowsProcessError("process executable path changed")
        executable_handle = api.open_executable(observed_path)
        observed_file_identity = api.file_identity(executable_handle)
        if observed_file_identity != (
            expected.executable.volume_serial,
            expected.executable.file_index,
        ):
            raise WindowsProcessError("process executable identity changed")
        if api.sha256_file(executable_handle) != expected.executable.sha256:
            raise WindowsProcessError("process executable digest changed")
        return OpenedVerifiedProcess(
            api=api,
            process_handle=process_handle,
            executable_handle=executable_handle,
            identity=expected,
        )
    except Exception:
        if executable_handle is not None:
            api.close_handle(executable_handle)
        api.close_handle(process_handle)
        raise


def safe_terminate(
    expected: ProcessIdentity,
    api: WindowsProcessApi,
    *,
    timeout_ms: int = 10_000,
) -> None:
    with open_verified_process(expected, api, terminate=True) as opened:
        api.terminate_process(opened.process_handle, 143)
        if not api.wait_process(opened.process_handle, timeout_ms):
            raise WindowsProcessError("verified process did not exit")


def owned_listener_matches(
    expected: ProcessIdentity,
    port: int,
    api: WindowsProcessApi,
) -> bool:
    if type(port) is not int or not 1 <= port <= 65_535:
        raise WindowsProcessError("listener port is invalid")
    with open_verified_process(expected, api):
        try:
            listeners = api.listener_pids(port)
        except OSError as exc:
            raise WindowsProcessError("listener ownership query failed") from exc
        return listeners == {expected.pid}


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


class NativeWindowsProcessApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsProcessError("Win32 process API is unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

    @staticmethod
    def _handle(value: object) -> ctypes.c_void_p:
        return ctypes.c_void_p(int(value))

    @staticmethod
    def _raise_last_error(message: str) -> None:
        error = ctypes.get_last_error()
        raise OSError(error, message)

    def open_process(self, pid: int, *, terminate: bool) -> object:
        access = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if terminate:
            access |= PROCESS_TERMINATE
        function = self.kernel32.OpenProcess
        function.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        function.restype = ctypes.c_void_p
        handle = function(access, 0, pid)
        if not handle:
            self._raise_last_error("OpenProcess failed")
        return int(handle)

    def close_handle(self, handle: object) -> None:
        function = self.kernel32.CloseHandle
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int
        if not function(self._handle(handle)):
            self._raise_last_error("CloseHandle failed")

    def process_creation_time(self, handle: object) -> int:
        values = [ctypes.c_uint64() for _ in range(4)]
        function = self.kernel32.GetProcessTimes
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        function.restype = ctypes.c_int
        if not function(
            self._handle(handle),
            *(ctypes.byref(value) for value in values),
        ):
            self._raise_last_error("GetProcessTimes failed")
        return int(values[0].value)

    def process_image_path(self, handle: object) -> str:
        capacity = 32_768
        buffer = ctypes.create_unicode_buffer(capacity)
        length = ctypes.c_uint32(capacity)
        function = self.kernel32.QueryFullProcessImageNameW
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        function.restype = ctypes.c_int
        if not function(self._handle(handle), 0, buffer, ctypes.byref(length)):
            self._raise_last_error("QueryFullProcessImageNameW failed")
        return buffer.value

    def open_executable(self, path: str) -> object:
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
        handle = function(
            path,
            GENERIC_READ,
            FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
        if int(handle) == INVALID_HANDLE_VALUE:
            self._raise_last_error("CreateFileW failed")
        return int(handle)

    def file_identity(self, handle: object) -> tuple[int, int]:
        information = _ByHandleFileInformation()
        function = self.kernel32.GetFileInformationByHandle
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation)]
        function.restype = ctypes.c_int
        if not function(self._handle(handle), ctypes.byref(information)):
            self._raise_last_error("GetFileInformationByHandle failed")
        file_index = (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow)
        return int(information.dwVolumeSerialNumber), file_index

    def sha256_file(self, handle: object) -> str:
        offset = ctypes.c_int64(0)
        function = self.kernel32.SetFilePointerEx
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        function.restype = ctypes.c_int
        if not function(self._handle(handle), offset, None, FILE_BEGIN):
            self._raise_last_error("SetFilePointerEx failed")
        read_file = self.kernel32.ReadFile
        read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        read_file.restype = ctypes.c_int
        digest = hashlib.sha256()
        buffer = ctypes.create_string_buffer(1024 * 1024)
        while True:
            read = ctypes.c_uint32()
            if not read_file(
                self._handle(handle),
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                self._raise_last_error("ReadFile failed")
            if read.value == 0:
                break
            digest.update(buffer.raw[: read.value])
        return digest.hexdigest()

    def terminate_process(self, handle: object, exit_code: int) -> None:
        function = self.kernel32.TerminateProcess
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        if not function(self._handle(handle), exit_code):
            self._raise_last_error("TerminateProcess failed")

    def wait_process(self, handle: object, timeout_ms: int) -> bool:
        function = self.kernel32.WaitForSingleObject
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        result = int(function(self._handle(handle), timeout_ms))
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        self._raise_last_error("WaitForSingleObject failed")
        return False

    def process_exit_code(self, handle: object) -> int:
        exit_code = ctypes.c_uint32()
        function = self.kernel32.GetExitCodeProcess
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        function.restype = ctypes.c_int
        if not function(self._handle(handle), ctypes.byref(exit_code)):
            self._raise_last_error("GetExitCodeProcess failed")
        return int(exit_code.value)

    def listener_pids(self, port: int) -> set[int]:
        size = ctypes.c_uint32()
        function = self.iphlpapi.GetExtendedTcpTable
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        function.restype = ctypes.c_uint32
        result = int(
            function(
                None,
                ctypes.byref(size),
                1,
                AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        )
        if result != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(result, "GetExtendedTcpTable sizing failed")
        buffer = ctypes.create_string_buffer(size.value)
        result = int(
            function(
                buffer,
                ctypes.byref(size),
                1,
                AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        )
        if result != 0:
            raise OSError(result, "GetExtendedTcpTable failed")
        count = struct.unpack_from("<I", buffer.raw, 0)[0]
        row_size = 24
        if 4 + count * row_size > size.value:
            raise OSError("GetExtendedTcpTable returned a truncated table")
        listeners: set[int] = set()
        for index in range(count):
            state, _local_address, local_port, _remote_address, _remote_port, pid = (
                struct.unpack_from("<6I", buffer.raw, 4 + index * row_size)
            )
            if state == MIB_TCP_STATE_LISTEN and socket.ntohs(local_port & 0xFFFF) == port:
                listeners.add(pid)
        return listeners
