from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
DUPLICATE_SAME_ACCESS = 0x00000002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
STILL_ACTIVE = 259
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
ERROR_ALREADY_EXISTS = 183
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
JOB_NAME = re.compile(r"LocalVideoTranscriber-[a-z]+-[0-9a-f]{32}")


class WindowsJobError(RuntimeError):
    pass


class WindowsJobApi(Protocol):
    def create_kill_on_close_job(self, name: str) -> object: ...

    def create_process_suspended(
        self,
        command: list[str],
        environment: dict[str, str],
        cwd: str,
    ) -> tuple[object, object, int]: ...

    def assign_process_to_job(self, job: object, process: object) -> None: ...

    def resume_thread(self, thread: object) -> None: ...

    def terminate_process(self, process: object, exit_code: int) -> None: ...

    def terminate_job(self, job: object, exit_code: int) -> None: ...

    def process_in_job(self, process: object, job: object) -> bool: ...

    def close_handle(self, handle: object) -> None: ...


@dataclass
class LaunchedJob:
    api: WindowsJobApi
    job_handle: object
    process_handle: object
    pid: int
    closed: bool = False

    def terminate(self, *, exit_code: int) -> None:
        if self.closed:
            raise WindowsJobError("job handle is closed")
        self.api.terminate_job(self.job_handle, exit_code)

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.api.close_handle(self.process_handle)
        finally:
            self.api.close_handle(self.job_handle)
            self.closed = True

    def __enter__(self) -> LaunchedJob:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def launch_suspended_in_job(
    command: list[str],
    environment: dict[str, str],
    cwd: str,
    job_name: str,
    api: WindowsJobApi,
) -> LaunchedJob:
    if (
        not command
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in command
        )
        or not cwd
        or "\x00" in cwd
        or JOB_NAME.fullmatch(job_name) is None
        or any(
            not key or key.startswith("=") or "\x00" in key or "\x00" in value
            for key, value in environment.items()
        )
    ):
        raise WindowsJobError("job launch arguments are invalid")
    job_handle: object | None = None
    process_handle: object | None = None
    thread_handle: object | None = None
    try:
        job_handle = api.create_kill_on_close_job(job_name)
        process_handle, thread_handle, pid = api.create_process_suspended(
            command,
            environment,
            cwd,
        )
        api.assign_process_to_job(job_handle, process_handle)
        api.resume_thread(thread_handle)
        api.close_handle(thread_handle)
        thread_handle = None
        return LaunchedJob(api, job_handle, process_handle, pid)
    except Exception as exc:
        cleanup_errors: list[Exception] = []
        if process_handle is not None:
            try:
                api.terminate_process(process_handle, 125)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        for handle in (thread_handle, process_handle, job_handle):
            if handle is None:
                continue
            try:
                api.close_handle(handle)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        wrapped = WindowsJobError("Windows Job Object launch failed")
        if cleanup_errors:
            raise ExceptionGroup(
                "Windows Job Object launch and cleanup failed",
                [wrapped, *cleanup_errors],
            ) from exc
        raise wrapped from exc


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _StartupInfoW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class NativeWindowsJobApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsJobError("Win32 Job Object API is unavailable")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    @staticmethod
    def _handle(value: object) -> ctypes.c_void_p:
        return ctypes.c_void_p(int(value))

    @staticmethod
    def _raise_last_error(message: str) -> None:
        error = ctypes.get_last_error()
        raise OSError(error, message)

    def create_kill_on_close_job(self, name: str) -> object:
        create = self.kernel32.CreateJobObjectW
        create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        create.restype = ctypes.c_void_p
        job = create(None, name)
        if not job:
            self._raise_last_error("CreateJobObjectW failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            self.close_handle(int(job))
            raise WindowsJobError("Job Object name is already in use")
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configure = self.kernel32.SetInformationJobObject
        configure.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        configure.restype = ctypes.c_int
        if not configure(
            self._handle(job),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self.close_handle(int(job))
            self._raise_last_error("SetInformationJobObject failed")
        return int(job)

    def open_job(self, name: str) -> object:
        if JOB_NAME.fullmatch(name) is None:
            raise WindowsJobError("Job Object name is invalid")
        function = self.kernel32.OpenJobObjectW
        function.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        function.restype = ctypes.c_void_p
        job = function(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, 0, name)
        if not job:
            self._raise_last_error("OpenJobObjectW failed")
        return int(job)

    def create_process_suspended(
        self,
        command: list[str],
        environment: dict[str, str],
        cwd: str,
    ) -> tuple[object, object, int]:
        startup = _StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        get_standard_handle = self.kernel32.GetStdHandle
        get_standard_handle.argtypes = [ctypes.c_int32]
        get_standard_handle.restype = ctypes.c_void_p
        get_current_process = self.kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = ctypes.c_void_p
        duplicate_handle = self.kernel32.DuplicateHandle
        duplicate_handle.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        duplicate_handle.restype = ctypes.c_int
        current_process = get_current_process()
        inherited_handles: list[int] = []
        attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
        attribute_initialized = False
        process = _ProcessInformation()
        try:
            for identifier in (STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
                source = get_standard_handle(identifier)
                if not source or int(source) == INVALID_HANDLE_VALUE:
                    raise WindowsJobError("standard process handle is unavailable")
                duplicate = ctypes.c_void_p()
                if not duplicate_handle(
                    current_process,
                    ctypes.c_void_p(source),
                    current_process,
                    ctypes.byref(duplicate),
                    0,
                    1,
                    DUPLICATE_SAME_ACCESS,
                ):
                    self._raise_last_error("DuplicateHandle failed")
                if duplicate.value is None:
                    raise WindowsJobError("duplicated process handle is unavailable")
                inherited_handles.append(int(duplicate.value))

            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = inherited_handles[0]
            startup.StartupInfo.hStdOutput = inherited_handles[1]
            startup.StartupInfo.hStdError = inherited_handles[2]
            handle_list = (ctypes.c_void_p * len(inherited_handles))(*inherited_handles)

            initialize_attributes = self.kernel32.InitializeProcThreadAttributeList
            initialize_attributes.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_size_t),
            ]
            initialize_attributes.restype = ctypes.c_int
            attribute_size = ctypes.c_size_t()
            initialize_attributes(None, 1, 0, ctypes.byref(attribute_size))
            if attribute_size.value == 0:
                self._raise_last_error("InitializeProcThreadAttributeList sizing failed")
            attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
            attribute_pointer = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            if not initialize_attributes(
                attribute_pointer,
                1,
                0,
                ctypes.byref(attribute_size),
            ):
                self._raise_last_error("InitializeProcThreadAttributeList failed")
            attribute_initialized = True
            startup.lpAttributeList = attribute_pointer.value

            update_attribute = self.kernel32.UpdateProcThreadAttribute
            update_attribute.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            update_attribute.restype = ctypes.c_int
            if not update_attribute(
                attribute_pointer,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_list, ctypes.c_void_p),
                ctypes.sizeof(handle_list),
                None,
                None,
            ):
                self._raise_last_error("UpdateProcThreadAttribute failed")

            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            environment_block = "\0".join(
                f"{key}={value}"
                for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
            )
            environment_buffer = ctypes.create_unicode_buffer(environment_block + "\0\0")
            create = self.kernel32.CreateProcessW
            create.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessInformation),
            ]
            create.restype = ctypes.c_int
            if not create(
                command[0],
                command_line,
                None,
                None,
                1,
                CREATE_SUSPENDED
                | CREATE_UNICODE_ENVIRONMENT
                | CREATE_NO_WINDOW
                | EXTENDED_STARTUPINFO_PRESENT,
                environment_buffer,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(process),
            ):
                self._raise_last_error("CreateProcessW failed")
            return int(process.hProcess), int(process.hThread), int(process.dwProcessId)
        finally:
            if attribute_initialized and startup.lpAttributeList:
                delete_attributes = self.kernel32.DeleteProcThreadAttributeList
                delete_attributes.argtypes = [ctypes.c_void_p]
                delete_attributes.restype = None
                delete_attributes(ctypes.c_void_p(startup.lpAttributeList))
            for handle in reversed(inherited_handles):
                self.close_handle(handle)

    def assign_process_to_job(self, job: object, process: object) -> None:
        function = self.kernel32.AssignProcessToJobObject
        function.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        function.restype = ctypes.c_int
        if not function(self._handle(job), self._handle(process)):
            self._raise_last_error("AssignProcessToJobObject failed")

    def resume_thread(self, thread: object) -> None:
        function = self.kernel32.ResumeThread
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_uint32
        if int(function(self._handle(thread))) == 0xFFFFFFFF:
            self._raise_last_error("ResumeThread failed")

    def terminate_process(self, process: object, exit_code: int) -> None:
        function = self.kernel32.TerminateProcess
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        if not function(self._handle(process), exit_code):
            self._raise_last_error("TerminateProcess failed")

    def terminate_job(self, job: object, exit_code: int) -> None:
        function = self.kernel32.TerminateJobObject
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        if not function(self._handle(job), exit_code):
            self._raise_last_error("TerminateJobObject failed")

    def process_in_job(self, process: object, job: object) -> bool:
        result = ctypes.c_int()
        function = self.kernel32.IsProcessInJob
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        function.restype = ctypes.c_int
        if not function(
            self._handle(process),
            self._handle(job),
            ctypes.byref(result),
        ):
            self._raise_last_error("IsProcessInJob failed")
        return bool(result.value)

    def wait_process(self, process: object, timeout_ms: int) -> bool:
        function = self.kernel32.WaitForSingleObject
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        result = int(function(self._handle(process), timeout_ms))
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        self._raise_last_error("WaitForSingleObject failed")

    def process_exit_code(self, process: object) -> int:
        exit_code = ctypes.c_uint32()
        function = self.kernel32.GetExitCodeProcess
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        function.restype = ctypes.c_int
        if not function(self._handle(process), ctypes.byref(exit_code)):
            self._raise_last_error("GetExitCodeProcess failed")
        if exit_code.value == STILL_ACTIVE:
            raise WindowsJobError("process is still active")
        return int(exit_code.value)

    def close_handle(self, handle: object) -> None:
        function = self.kernel32.CloseHandle
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int
        if not function(self._handle(handle)):
            self._raise_last_error("CloseHandle failed")
