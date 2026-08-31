from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Protocol

from windows_job import WindowsJobApi, launch_suspended_in_job
from windows_process import WindowsProcessApi, capture_process_identity
from windows_service import (
    SERVICE_PORTS,
    NativeWindowsServiceApi,
    WindowsServiceError,
    WindowsServiceRecord,
    publish_service_record,
    retire_service_record,
)


class WindowsSupervisorApi(WindowsJobApi, WindowsProcessApi, Protocol):
    pass


def supervise_service(
    *,
    record_path: Path,
    kind: str,
    port: int,
    nonce: str,
    generation: int,
    command: list[str],
    environment: dict[str, str],
    cwd: str,
    api: WindowsSupervisorApi | None = None,
    current_pid: int | None = None,
    readiness_timeout: float = 15.0,
    poll_interval: float = 0.05,
) -> int:
    if kind not in SERVICE_PORTS or port != SERVICE_PORTS[kind]:
        raise WindowsServiceError("service supervisor configuration is invalid")
    selected = NativeWindowsServiceApi() if api is None else api
    supervisor_pid = os.getpid() if current_pid is None else current_pid
    launched = None
    record: WindowsServiceRecord | None = None
    published = False
    service_exited = False
    result: int | None = None
    operation_error: BaseException | None = None
    try:
        job_name = f"LocalVideoTranscriber-{kind}-{nonce}"
        launched = launch_suspended_in_job(
            command,
            environment,
            cwd,
            job_name,
            selected,
        )
        supervisor_handle = selected.open_process(supervisor_pid, terminate=False)
        try:
            supervisor_identity = capture_process_identity(
                supervisor_pid,
                supervisor_handle,
                selected,
            )
        finally:
            selected.close_handle(supervisor_handle)
        service_identity = None
        deadline = time.monotonic() + readiness_timeout
        while time.monotonic() < deadline:
            listeners = selected.listener_pids(port)
            if len(listeners) == 1:
                listener_pid = next(iter(listeners))
                listener_handle = selected.open_process(listener_pid, terminate=False)
                try:
                    if selected.process_in_job(listener_handle, launched.job_handle):
                        service_identity = capture_process_identity(
                            listener_pid,
                            listener_handle,
                            selected,
                        )
                        break
                finally:
                    selected.close_handle(listener_handle)
            time.sleep(poll_interval)
        if service_identity is None:
            raise WindowsServiceError("service listener did not become owned")
        record = WindowsServiceRecord(
            kind=kind,
            port=port,
            nonce=nonce,
            generation=generation,
            job_name=job_name,
            supervisor=supervisor_identity,
            service=service_identity,
        )
        publish_service_record(record_path, record)
        published = True
        while not selected.wait_process(launched.process_handle, 250):
            pass
        service_exited = True
        result = selected.process_exit_code(launched.process_handle)
    except BaseException as exc:
        operation_error = exc

    cleanup_errors: list[BaseException] = []
    if launched is not None:
        if not service_exited:
            try:
                launched.terminate(exit_code=125)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        try:
            launched.close()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
    if published and record is not None:
        try:
            retire_service_record(record_path, record)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)

    if operation_error is not None:
        if cleanup_errors:
            raise BaseExceptionGroup(
                "service supervision and cleanup failed",
                [operation_error, *cleanup_errors],
            ) from operation_error
        raise operation_error
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        raise BaseExceptionGroup("service supervisor cleanup failed", cleanup_errors)
    if result is None:
        raise WindowsServiceError("service supervisor produced no exit code")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one Windows application service")
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=sorted(SERVICE_PORTS))
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        return supervise_service(
            record_path=arguments.record,
            kind=arguments.kind,
            port=arguments.port,
            nonce=arguments.nonce,
            generation=arguments.generation,
            command=command,
            environment=dict(os.environ),
            cwd=arguments.cwd,
        )
    except BaseException:
        return 70


if __name__ == "__main__":
    sys.exit(main())
