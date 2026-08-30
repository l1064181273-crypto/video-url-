from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from runtime_layout import path_is_link_like
from windows_job import NativeWindowsJobApi
from windows_process import (
    NativeWindowsProcessApi,
    ProcessIdentity,
    WindowsProcessApi,
    WindowsProcessError,
    open_verified_process,
    process_identity_from_dict,
)
from windows_publication import NativeWindowsPublicationApi, rename_exclusive

SERVICE_PORTS = {
    "backend": 8765,
    "ollama": 11435,
}
NONCE = re.compile(r"[0-9a-f]{32}")
RECORD_NAME = re.compile(
    r"(?:backend|ollama)\.pid|(?:backend|ollama)-[1-9][0-9]*-[0-9a-f]{32}\.json"
)


class WindowsServiceError(RuntimeError):
    pass


class WindowsServiceApi(WindowsProcessApi, Protocol):
    def open_job(self, name: str) -> object: ...

    def terminate_job(self, job: object, exit_code: int) -> None: ...


class NativeWindowsServiceApi(NativeWindowsProcessApi, NativeWindowsJobApi):
    def __init__(self) -> None:
        NativeWindowsProcessApi.__init__(self)


@dataclass(frozen=True)
class WindowsServiceRecord:
    kind: str
    port: int
    nonce: str
    generation: int
    job_name: str
    supervisor: ProcessIdentity
    service: ProcessIdentity

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "port": self.port,
            "nonce": self.nonce,
            "generation": self.generation,
            "job_name": self.job_name,
            "supervisor": self.supervisor.as_dict(),
            "service": self.service.as_dict(),
        }


def service_record_from_dict(payload: Any) -> WindowsServiceRecord:
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "kind",
            "port",
            "nonce",
            "generation",
            "job_name",
            "supervisor",
            "service",
        }:
            raise ValueError
        kind = payload["kind"]
        port = payload["port"]
        nonce = payload["nonce"]
        generation = payload["generation"]
        job_name = payload["job_name"]
        if (
            payload["schema_version"] != 1
            or not isinstance(kind, str)
            or kind not in SERVICE_PORTS
            or type(port) is not int
            or port != SERVICE_PORTS[kind]
            or not isinstance(nonce, str)
            or NONCE.fullmatch(nonce) is None
            or type(generation) is not int
            or generation <= 0
            or not isinstance(job_name, str)
            or job_name != f"LocalVideoTranscriber-{kind}-{nonce}"
        ):
            raise ValueError
        supervisor = process_identity_from_dict(payload["supervisor"])
        service = process_identity_from_dict(payload["service"])
    except (KeyError, TypeError, ValueError, WindowsProcessError) as exc:
        raise WindowsServiceError("service record is invalid") from exc
    return WindowsServiceRecord(
        kind=kind,
        port=port,
        nonce=nonce,
        generation=generation,
        job_name=job_name,
        supervisor=supervisor,
        service=service,
    )


def _read_service_record(path: Path) -> WindowsServiceRecord:
    if RECORD_NAME.fullmatch(path.name) is None or path_is_link_like(path):
        raise WindowsServiceError("service record path is unsafe")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 2 <= metadata.st_size <= 65_536
        ):
            raise WindowsServiceError("service record metadata is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        named = path.stat(follow_symlinks=False)
        if (
            len(encoded) != metadata.st_size
            or named.st_dev != metadata.st_dev
            or named.st_ino != metadata.st_ino
            or named.st_size != metadata.st_size
        ):
            raise WindowsServiceError("service record changed during read")
        payload = json.loads(encoded)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise WindowsServiceError("service record is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return service_record_from_dict(payload)


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_service_record(path: Path, record: WindowsServiceRecord) -> None:
    if (
        path.name != f"{record.kind}.pid"
        or not path.parent.is_dir()
        or path_is_link_like(path.parent)
        or path.exists()
        or path_is_link_like(path)
    ):
        raise WindowsServiceError("service record destination is occupied or unsafe")
    encoded = (
        json.dumps(record.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    staged = path.parent / f".{path.name}.staged-{uuid.uuid4().hex}"
    descriptor: int | None = None
    published = False
    staged_metadata: os.stat_result | None = None
    try:
        descriptor = os.open(
            staged,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise WindowsServiceError("service record write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        staged_metadata = os.fstat(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            if sys.platform == "win32":
                rename_exclusive(
                    PureWindowsPath(str(staged)),
                    PureWindowsPath(str(path)),
                    NativeWindowsPublicationApi(),
                )
            else:
                os.link(staged, path, follow_symlinks=False)
                staged.unlink()
        except FileExistsError as exc:
            raise WindowsServiceError("service record destination is occupied") from exc
        published = True
        named = path.stat(follow_symlinks=False)
        if (
            staged_metadata.st_dev != named.st_dev
            or staged_metadata.st_ino != named.st_ino
            or named.st_size != len(encoded)
            or path_is_link_like(path)
        ):
            raise WindowsServiceError("service record publication identity changed")
        _fsync_directory(path.parent)
    except Exception:
        if published:
            try:
                named = path.stat(follow_symlinks=False)
                if (
                    staged_metadata is not None
                    and named.st_dev == staged_metadata.st_dev
                    and named.st_ino == staged_metadata.st_ino
                ):
                    path.unlink()
                    _fsync_directory(path.parent)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staged.exists() or path_is_link_like(staged):
            staged.unlink()


def retire_service_record(
    path: Path,
    expected: WindowsServiceRecord,
) -> Path:
    current = _read_service_record(path)
    if current != expected:
        raise WindowsServiceError("service record changed before retirement")
    history = path.parent / "history"
    history.mkdir(mode=0o700, exist_ok=True)
    if path_is_link_like(history) or not history.is_dir():
        raise WindowsServiceError("service record history is unsafe")
    destination = history / f"{expected.kind}-{expected.generation}-{expected.nonce}.json"
    if destination.exists() or path_is_link_like(destination):
        raise WindowsServiceError("service record history destination is occupied")
    try:
        if sys.platform == "win32":
            rename_exclusive(
                PureWindowsPath(str(path)),
                PureWindowsPath(str(destination)),
                NativeWindowsPublicationApi(),
            )
        else:
            os.link(path, destination, follow_symlinks=False)
            retained = service_record_from_dict(json.loads(destination.read_bytes()))
            if retained != expected:
                destination.unlink()
                raise WindowsServiceError("service record changed during retirement")
            source_metadata = path.stat(follow_symlinks=False)
            destination_metadata = destination.stat(follow_symlinks=False)
            if (
                source_metadata.st_dev != destination_metadata.st_dev
                or source_metadata.st_ino != destination_metadata.st_ino
            ):
                destination.unlink()
                raise WindowsServiceError("service record identity changed during retirement")
            path.unlink()
            _fsync_directory(path.parent)
            _fsync_directory(history)
    except WindowsServiceError:
        raise
    except Exception as exc:
        raise WindowsServiceError("service record retirement failed") from exc
    return destination


def verify_owned_service_record(
    path: Path,
    kind: str,
    port: int,
    require_listener: bool = True,
    *,
    api: WindowsServiceApi | None = None,
) -> bool:
    selected = NativeWindowsServiceApi() if api is None else api
    supervisor = None
    service = None
    job_handle: object | None = None
    try:
        record = _read_service_record(path)
        if record.kind != kind or record.port != port:
            return False
        supervisor = open_verified_process(record.supervisor, selected)
        service = open_verified_process(record.service, selected)
        job_handle = selected.open_job(record.job_name)
        return not require_listener or selected.listener_pids(port) == {record.service.pid}
    except Exception:
        return False
    finally:
        if job_handle is not None:
            selected.close_handle(job_handle)
        if service is not None:
            service.close()
        if supervisor is not None:
            supervisor.close()


def stop_verified_service(
    record: WindowsServiceRecord,
    api: WindowsServiceApi,
    *,
    service_timeout_ms: int = 10_000,
    supervisor_timeout_ms: int = 5_000,
) -> None:
    supervisor = None
    service = None
    job_handle: object | None = None
    operation_error: Exception | None = None
    try:
        supervisor = open_verified_process(record.supervisor, api, terminate=True)
        service = open_verified_process(record.service, api)
        try:
            listeners = api.listener_pids(record.port)
        except OSError as exc:
            raise WindowsServiceError("service listener ownership query failed") from exc
        if listeners != {record.service.pid}:
            raise WindowsServiceError("service listener ownership changed")
        job_handle = api.open_job(record.job_name)
        api.terminate_job(job_handle, 143)
        if not api.wait_process(service.process_handle, service_timeout_ms):
            raise WindowsServiceError("service Job Object did not stop")
        if not api.wait_process(supervisor.process_handle, supervisor_timeout_ms):
            api.terminate_process(supervisor.process_handle, 143)
            if not api.wait_process(supervisor.process_handle, supervisor_timeout_ms):
                raise WindowsServiceError("service supervisor did not stop")
    except WindowsProcessError as exc:
        operation_error = WindowsServiceError("service process identity validation failed")
        operation_error.__cause__ = exc
    except Exception as exc:
        operation_error = exc
    cleanup_errors: list[Exception] = []
    for resource in (job_handle, service, supervisor):
        if resource is None:
            continue
        try:
            if hasattr(resource, "close"):
                resource.close()
            else:
                api.close_handle(resource)
        except Exception as cleanup_error:
            cleanup_errors.append(cleanup_error)
    if operation_error is not None:
        if cleanup_errors:
            raise ExceptionGroup(
                "service stop and cleanup failed",
                [operation_error, *cleanup_errors],
            ) from operation_error
        raise operation_error
    if len(cleanup_errors) == 1:
        raise cleanup_errors[0]
    if cleanup_errors:
        raise ExceptionGroup("service stop cleanup failed", cleanup_errors)
