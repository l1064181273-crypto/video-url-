#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import http.client
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lifecycle_lock import LifecycleLock


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidationOutcome:
    exit_code: int
    status: str


@dataclass(frozen=True)
class LifecycleResult:
    exit_code: int
    status: str


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    pgid: int
    start_time: str
    executable: Path
    device: int
    inode: int
    sha256: str
    signal_token: tuple[int, ...]


@dataclass(frozen=True)
class ServiceIdentity:
    kind: str
    nonce: str
    supervisor: ProcessSnapshot
    service: ProcessSnapshot


@dataclass
class ClaimedRecord:
    parent_descriptor: int
    descriptor: int
    name: str
    payload: dict[str, Any]

    def close(self) -> None:
        os.close(self.descriptor)
        os.close(self.parent_descriptor)


@dataclass
class RecordEntry:
    generation: int
    name: str
    payload: dict[str, Any]
    descriptor: int
    metadata: os.stat_result
    retained: bool

    def close(self) -> None:
        os.close(self.descriptor)


class ServiceOperations(Protocol):
    def reconcile(self) -> None: ...

    def validate(self, phase: str) -> ValidationOutcome: ...

    def state(self, kind: str) -> str: ...

    def launch(self, kind: str, activation_fd: int | None = None) -> ServiceIdentity: ...

    def backend_healthy(self) -> bool: ...

    def stop(self, kind: str) -> None: ...

    def ownership_records_converged(self) -> bool: ...


_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


def _local_lock(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


class LifecycleManager:
    def __init__(
        self,
        data_root: Path,
        release_root: Path,
        *,
        operations: ServiceOperations | None = None,
    ) -> None:
        self.data_root = data_root
        self.release_root = release_root
        self.operations = operations or SystemServiceOperations(data_root, release_root)

    def prestart(self, *, lock_held: bool = False) -> LifecycleResult:
        if not lock_held:
            return self._under_lock("start", self.prestart)
        outcome = self.operations.validate("installed-prerequisites")
        expected = {0: "healthy", 1: "warning", 2: "failed"}
        if expected.get(outcome.exit_code) != outcome.status:
            return LifecycleResult(2, "unsafe_or_corrupt")
        if outcome.exit_code == 1:
            return LifecycleResult(1, "missing_prerequisite")
        if outcome.exit_code == 2:
            return LifecycleResult(2, "unsafe_or_corrupt")
        if outcome.exit_code != 0:
            return LifecycleResult(2, "unsafe_or_corrupt")
        backend_state = self.operations.state("backend")
        if backend_state == "absent":
            return LifecycleResult(0, "ready_to_start")
        if backend_state == "owned":
            return LifecycleResult(10, "already_running")
        return LifecycleResult(2, "unsafe_or_corrupt")

    def start(
        self,
        *,
        activation_fd: int | None = None,
        lock_held: bool = False,
    ) -> LifecycleResult:
        if not lock_held:
            return self._under_lock(
                "start",
                lambda *, lock_held: self.start(
                    activation_fd=activation_fd,
                    lock_held=lock_held,
                ),
            )
        self.operations.reconcile()
        prestart = self.prestart(lock_held=True)
        if prestart.status == "already_running":
            runtime = self.operations.validate("runtime-full")
            if runtime != ValidationOutcome(0, "healthy"):
                raise ServiceError("running services failed runtime validation")
            return LifecycleResult(0, "already_running")
        if prestart != LifecycleResult(0, "ready_to_start"):
            return prestart

        ollama_state = self.operations.state("ollama")
        if ollama_state == "unsafe":
            raise ServiceError("project Ollama ownership is unsafe")
        created: list[str] = []
        try:
            if ollama_state == "absent":
                self.operations.launch("ollama")
                created.append("ollama")
            self.operations.launch("backend", activation_fd)
            created.append("backend")
            if not self.operations.backend_healthy():
                raise ServiceError("backend health check failed")
            runtime = self.operations.validate("runtime-full")
            if runtime != ValidationOutcome(0, "healthy"):
                raise ServiceError("runtime validation failed")
            return LifecycleResult(0, "started")
        except Exception as start_error:
            cleanup_errors: list[Exception] = []
            for kind in reversed(created):
                try:
                    self.operations.stop(kind)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExceptionGroup(
                    "start and cleanup failed",
                    [start_error, *cleanup_errors],
                ) from start_error
            raise

    def stop(self, *, lock_held: bool = False) -> LifecycleResult:
        if not lock_held:
            return self._under_lock("stop", self.stop)
        self.operations.reconcile()
        backend_state = self.operations.state("backend")
        if backend_state == "unsafe":
            raise ServiceError("backend ownership is unsafe")
        if backend_state == "owned":
            self.operations.stop("backend")
        if not self.operations.ownership_records_converged():
            raise ServiceError("tool ownership records did not converge")
        ollama_state = self.operations.state("ollama")
        if ollama_state == "unsafe":
            raise ServiceError("project Ollama ownership is unsafe")
        if ollama_state == "owned":
            self.operations.stop("ollama")
        if not self.operations.ownership_records_converged():
            raise ServiceError("process ownership records did not converge")
        return LifecycleResult(0, "stopped")

    def _under_lock(
        self,
        operation: str,
        callback: object,
    ) -> LifecycleResult:
        local = _local_lock(self.data_root / "app")
        with local:
            lock = LifecycleLock(self.data_root / "app", operation=operation)
            lock.acquire_flock(blocking=True)
            try:
                return callback(lock_held=True)  # type: ignore[operator]
            finally:
                lock.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _executable_path(pid: int) -> Path | None:
    if sys.platform != "darwin":
        return None
    buffer = ctypes.create_string_buffer(4096)
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        length = libproc.proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            return None
        return Path(os.fsdecode(buffer.value)).resolve(strict=True)
    except (AttributeError, OSError):
        return None


def _audit_token(pid: int) -> tuple[int, ...] | None:
    if sys.platform != "darwin":
        return None
    token = (ctypes.c_uint * 8)()
    count = ctypes.c_uint(len(token))
    task = ctypes.c_uint()
    system: Any = None
    self_task = 0
    try:
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self_task = system.mach_task_self()
        if system.task_name_for_pid(self_task, pid, ctypes.byref(task)) != 0:
            return None
        if system.task_info(task.value, 15, token, ctypes.byref(count)) != 0:
            return None
        if count.value != len(token):
            return None
        return tuple(token)
    except (AttributeError, OSError):
        return None
    finally:
        if system is not None and task.value:
            with suppress(AttributeError, OSError):
                system.mach_port_deallocate(self_task, task.value)


def _snapshot(pid: int) -> ProcessSnapshot | None:
    before = _audit_token(pid)
    if before is None:
        return None
    try:
        os.kill(pid, 0)
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None
    start = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    executable = _executable_path(pid)
    if start.returncode != 0 or executable is None:
        return None
    start_time = " ".join(start.stdout.split())
    try:
        metadata = executable.stat()
        digest = _sha256(executable)
    except OSError:
        return None
    after = _audit_token(pid)
    if not start_time or after != before or after is None:
        return None
    return ProcessSnapshot(
        pid=pid,
        pgid=pgid,
        start_time=start_time,
        executable=executable,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        sha256=digest,
        signal_token=after,
    )


def _stable_snapshot(pid: int, *, timeout: float = 1.0) -> ProcessSnapshot | None:
    deadline = time.monotonic() + timeout
    previous = _snapshot(pid)
    while previous is not None and time.monotonic() < deadline:
        time.sleep(0.01)
        current = _snapshot(pid)
        if current is None:
            return None
        if (
            current.signal_token == previous.signal_token
            and _snapshot_matches(current, _snapshot_payload(previous))
            and _token_is_live(current)
        ):
            return current
        previous = current
    return None


def _snapshot_payload(snapshot: ProcessSnapshot) -> dict[str, Any]:
    return {
        "pid": snapshot.pid,
        "pgid": snapshot.pgid,
        "start_time": snapshot.start_time,
        "audit_token": list(snapshot.signal_token),
        "executable": {
            "realpath": str(snapshot.executable),
            "device": snapshot.device,
            "inode": snapshot.inode,
            "sha256": snapshot.sha256,
        },
    }


def _snapshot_matches(snapshot: ProcessSnapshot, expected: Any) -> bool:
    return bool(
        isinstance(expected, dict)
        and set(expected) == {"pid", "pgid", "start_time", "audit_token", "executable"}
        and snapshot.pid == expected.get("pid")
        and snapshot.pgid == expected.get("pgid")
        and snapshot.start_time == expected.get("start_time")
        and expected.get("audit_token") == list(snapshot.signal_token)
        and isinstance(expected.get("executable"), dict)
        and str(snapshot.executable) == expected["executable"].get("realpath")
        and snapshot.device == expected["executable"].get("device")
        and snapshot.inode == expected["executable"].get("inode")
        and snapshot.sha256 == expected["executable"].get("sha256")
    )


def _snapshot_payload_is_valid(expected: Any) -> bool:
    if (
        not isinstance(expected, dict)
        or set(expected) != {"pid", "pgid", "start_time", "audit_token", "executable"}
        or type(expected.get("pid")) is not int
        or expected["pid"] <= 0
        or type(expected.get("pgid")) is not int
        or expected["pgid"] <= 0
        or not isinstance(expected.get("start_time"), str)
        or not expected["start_time"]
        or not isinstance(expected.get("audit_token"), list)
        or len(expected["audit_token"]) != 8
        or not all(
            type(value) is int and 0 <= value <= 0xFFFFFFFF for value in expected["audit_token"]
        )
    ):
        return False
    executable = expected.get("executable")
    return bool(
        isinstance(executable, dict)
        and set(executable) == {"realpath", "device", "inode", "sha256"}
        and isinstance(executable.get("realpath"), str)
        and Path(executable["realpath"]).is_absolute()
        and type(executable.get("device")) is int
        and executable["device"] >= 0
        and type(executable.get("inode")) is int
        and executable["inode"] > 0
        and isinstance(executable.get("sha256"), str)
        and len(executable["sha256"]) == 64
        and all(character in "0123456789abcdef" for character in executable["sha256"])
    )


def _process_command(pid: int) -> str | None:
    completed = subprocess.run(
        ["/bin/ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    command = completed.stdout.strip()
    return command if completed.returncode == 0 and command else None


def _verified_record(
    path: Path,
    kind: str,
    port: int,
) -> tuple[ProcessSnapshot, ProcessSnapshot] | None:
    payload = _read_record_payload(path, kind, port)
    if payload is None:
        return None
    return _verified_record_payload(payload, kind)


def _verified_record_payload(
    payload: dict[str, Any],
    kind: str,
) -> tuple[ProcessSnapshot, ProcessSnapshot] | None:
    try:
        supervisor = _snapshot(payload["supervisor"]["pid"])
        service = _snapshot(payload["service"]["pid"])
        command = _process_command(payload["supervisor"]["pid"])
        if (
            supervisor is None
            or service is None
            or not _snapshot_matches(supervisor, payload["supervisor"])
            or not _snapshot_matches(service, payload["service"])
            or command is None
            or f"--nonce {payload['nonce']}" not in command
            or f"--kind {kind}" not in command
            or supervisor.pid != supervisor.pgid
            or service.pgid != supervisor.pgid
            or os.getsid(supervisor.pid) != supervisor.pid
            or os.getsid(service.pid) != supervisor.pid
        ):
            return None
        members = _verified_record_members(payload)
        if members is None or not any(member.pid == service.pid for member in members):
            return None
        return supervisor, service
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _verified_record_eventually(
    path: Path,
    kind: str,
    port: int,
    *,
    timeout: float = 0.5,
) -> tuple[ProcessSnapshot, ProcessSnapshot] | None:
    deadline = time.monotonic() + timeout
    while True:
        verified = _verified_record(path, kind, port)
        if verified is not None:
            return verified
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _verified_record_payload_eventually(
    payload: dict[str, Any],
    kind: str,
    *,
    timeout: float = 0.5,
) -> tuple[ProcessSnapshot, ProcessSnapshot] | None:
    deadline = time.monotonic() + timeout
    while True:
        verified = _verified_record_payload(payload, kind)
        if verified is not None:
            return verified
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _service_identity(
    kind: str,
    payload: dict[str, Any],
    verified: tuple[ProcessSnapshot, ProcessSnapshot],
) -> ServiceIdentity:
    return ServiceIdentity(
        kind=kind,
        nonce=str(payload["nonce"]),
        supervisor=verified[0],
        service=verified[1],
    )


def _record_matches_identity(payload: dict[str, Any], expected: ServiceIdentity) -> bool:
    return bool(
        payload.get("kind") == expected.kind
        and payload.get("nonce") == expected.nonce
        and payload.get("supervisor") == _snapshot_payload(expected.supervisor)
        and payload.get("service") == _snapshot_payload(expected.service)
    )


def _open_record_parent(path: Path) -> int:
    parent = path.parent
    if not parent.is_absolute() or ".." in parent.parts:
        raise ServiceError("service runtime parent is unsafe")
    descriptor = os.open(
        "/",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for component in parent.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(path.parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
            or metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
        ):
            raise ServiceError("service runtime parent is unsafe")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _entry_is_absent(parent_descriptor: int, name: str) -> bool:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        return exc.errno == errno.ENOENT
    os.close(descriptor)
    return False


def _record_is_absent(path: Path) -> bool:
    try:
        parent_descriptor = _open_record_parent(path)
    except (OSError, ServiceError):
        return False
    try:
        return _entry_is_absent(parent_descriptor, path.name)
    finally:
        os.close(parent_descriptor)


def _open_record_at(
    parent_descriptor: int,
    name: str,
    kind: str,
    port: int,
) -> tuple[dict[str, Any], int, os.stat_result] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
    except OSError:
        return None
    keep_open = False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o777 != 0o600
            or metadata.st_size > 1024 * 1024
        ):
            return None
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        os.lseek(descriptor, 0, os.SEEK_SET)
        repeated = b""
        while len(repeated) <= metadata.st_size:
            chunk = os.read(descriptor, min(metadata.st_size + 1 - len(repeated), 64 * 1024))
            if not chunk:
                break
            repeated += chunk
        final_metadata = os.fstat(descriptor)
        if (
            len(encoded) != metadata.st_size
            or repeated != encoded
            or final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            return None
        payload = json.loads(encoded)
        if (
            not isinstance(payload, dict)
            or set(payload)
            not in (
                {
                    "schema_version",
                    "kind",
                    "nonce",
                    "port",
                    "supervisor",
                    "service",
                    "members",
                },
                {
                    "schema_version",
                    "record_generation",
                    "kind",
                    "nonce",
                    "port",
                    "supervisor",
                    "service",
                    "members",
                },
            )
            or payload.get("schema_version") != 1
            or (
                "record_generation" in payload
                and (
                    type(payload["record_generation"]) is not int
                    or payload["record_generation"] <= 0
                )
            )
            or payload.get("kind") != kind
            or payload.get("port") != port
            or not isinstance(payload.get("nonce"), str)
            or len(payload["nonce"]) != 32
            or any(character not in "0123456789abcdef" for character in payload["nonce"])
        ):
            return None
        for field in ("supervisor", "service"):
            if not _snapshot_payload_is_valid(payload.get(field)):
                return None
        members = payload.get("members")
        if (
            not isinstance(members, list)
            or not members
            or any(not _snapshot_payload_is_valid(member) for member in members)
            or len({member["pid"] for member in members}) != len(members)
            or payload["service"] not in members
            or payload["supervisor"]["pid"] in {member["pid"] for member in members}
            or any(member["pgid"] != payload["supervisor"]["pgid"] for member in members)
        ):
            return None
        keep_open = True
        return payload, descriptor, metadata
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if not keep_open:
            os.close(descriptor)


def _scan_record_entries(
    parent_descriptor: int,
    base_name: str,
    kind: str,
    port: int,
) -> tuple[list[RecordEntry], list[RecordEntry]]:
    generation_prefix = f".{base_name}.generation-"
    claim_prefix = f".{base_name}.claim-"
    temporary_prefix = f".{base_name}.tmp-"
    names = os.listdir(parent_descriptor)
    if any(name.startswith(temporary_prefix) for name in names):
        raise ServiceError("service ownership record temporary is unverified")
    public_names = [
        name for name in names if name == base_name or name.startswith(generation_prefix)
    ]
    claim_names = [name for name in names if name.startswith(claim_prefix)]
    records: list[RecordEntry] = []
    claims: list[RecordEntry] = []
    try:
        for name, retained in (
            *((name, False) for name in public_names),
            *((name, True) for name in claim_names),
        ):
            opened = _open_record_at(parent_descriptor, name, kind, port)
            if opened is None:
                raise ServiceError(
                    f"{kind} {'claimed ' if retained else ''}ownership record is unverified"
                )
            payload, descriptor, metadata = opened
            generation = int(payload.get("record_generation", 1))
            nonce = str(payload["nonce"])
            if name.startswith(generation_prefix) and name != (
                f"{generation_prefix}{nonce}-{generation:020d}"
            ):
                os.close(descriptor)
                raise ServiceError(f"{kind} ownership record generation name is invalid")
            if name.startswith(claim_prefix):
                encoded_generation = name.removeprefix(claim_prefix).split("-", 1)[0]
                if encoded_generation != f"{generation:020d}":
                    os.close(descriptor)
                    raise ServiceError(f"{kind} claimed ownership generation is invalid")
            entry = RecordEntry(generation, name, payload, descriptor, metadata, retained)
            (claims if retained else records).append(entry)
        seen: set[tuple[str, int]] = set()
        for entry in [*records, *claims]:
            key = (str(entry.payload["nonce"]), entry.generation)
            if key in seen:
                raise ServiceError(f"{kind} ownership record generation is duplicate")
            seen.add(key)
        return records, claims
    except Exception:
        for entry in [*records, *claims]:
            with suppress(OSError):
                entry.close()
        raise


def _close_record_entries(entries: list[RecordEntry], *, except_fd: int = -1) -> None:
    for entry in entries:
        if entry.descriptor != except_fd:
            with suppress(OSError):
                entry.close()


def _select_record_entry(
    records: list[RecordEntry],
    claims: list[RecordEntry],
    kind: str,
    *,
    preferred_nonce: str | None = None,
) -> RecordEntry | None:
    newest_record_by_nonce: dict[str, RecordEntry] = {}
    newest_claim_by_nonce: dict[str, RecordEntry] = {}
    for entry, destination in (
        *((entry, newest_record_by_nonce) for entry in records),
        *((entry, newest_claim_by_nonce) for entry in claims),
    ):
        nonce = str(entry.payload["nonce"])
        previous = destination.get(nonce)
        if previous is None or entry.generation > previous.generation:
            destination[nonce] = entry
    for nonce, record in newest_record_by_nonce.items():
        claim = newest_claim_by_nonce.get(nonce)
        if claim is not None and record.generation <= claim.generation:
            raise ServiceError(f"{kind} ownership record generation rolled back")
    newest_by_nonce = dict(newest_claim_by_nonce)
    newest_by_nonce.update(newest_record_by_nonce)
    if not newest_by_nonce:
        return None
    active: list[RecordEntry] = []
    for entry in newest_by_nonce.values():
        members = _verified_record_members_eventually(entry.payload)
        if members is None:
            raise ServiceError(f"{kind} ownership record is unverified")
        if members:
            active.append(entry)
    if len(active) > 1:
        raise ServiceError(f"{kind} ownership record generation is ambiguous")
    if active:
        return active[0]
    if preferred_nonce is not None and preferred_nonce in newest_by_nonce:
        return newest_by_nonce[preferred_nonce]
    return max(
        newest_by_nonce.values(),
        key=lambda entry: (entry.generation, str(entry.payload["nonce"])),
    )


def _read_record_payload(path: Path, kind: str, port: int) -> dict[str, Any] | None:
    try:
        parent_descriptor = _open_record_parent(path)
    except (OSError, ServiceError):
        return None
    try:
        records, claims = _scan_record_entries(parent_descriptor, path.name, kind, port)
        try:
            selected = _select_record_entry(records, claims, kind)
            return None if selected is None else selected.payload
        finally:
            _close_record_entries([*records, *claims])
    except ServiceError:
        return None
    finally:
        os.close(parent_descriptor)


def _verified_record_members(payload: dict[str, Any]) -> tuple[ProcessSnapshot, ...] | None:
    expected_by_pid = {
        member["pid"]: member for member in [payload["supervisor"], *payload["members"]]
    }
    live: list[ProcessSnapshot] = []
    for expected in expected_by_pid.values():
        snapshot = _snapshot(expected["pid"])
        if snapshot is None:
            continue
        if not _snapshot_matches(snapshot, expected) or not _token_is_live(snapshot):
            return None
        live.append(snapshot)
    group = _group_snapshots(payload["supervisor"]["pgid"])
    if group is None:
        return None
    for snapshot in group:
        expected = expected_by_pid.get(snapshot.pid)
        if (
            expected is None
            or not _snapshot_matches(snapshot, expected)
            or not _token_is_live(snapshot)
        ):
            return None
    return tuple(live)


def _verified_record_members_eventually(
    payload: dict[str, Any],
    *,
    timeout: float = 0.5,
) -> tuple[ProcessSnapshot, ...] | None:
    deadline = time.monotonic() + timeout
    while True:
        members = _verified_record_members(payload)
        if members is not None:
            return members
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def verify_owned_service_record(
    path: Path,
    kind: str,
    port: int,
    *,
    require_listener: bool = False,
) -> bool:
    verified = _verified_record_eventually(path, kind, port)
    if verified is None:
        return False
    return not require_listener or _owned_group_listens_eventually(verified[1], port)


def _signal_snapshot(snapshot: ProcessSnapshot, requested: signal.Signals) -> bool:
    if not _token_is_live(snapshot):
        return False
    encoded = (ctypes.c_uint * 8)(*snapshot.signal_token)
    try:
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        return system.proc_signal_with_audittoken(ctypes.byref(encoded), int(requested)) == 0
    except (AttributeError, OSError):
        return False


def _token_is_live(snapshot: ProcessSnapshot) -> bool:
    if sys.platform != "darwin" or len(snapshot.signal_token) != 8:
        return False
    encoded = (ctypes.c_uint * 8)(*snapshot.signal_token)
    buffer = ctypes.create_string_buffer(4096)
    try:
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        return system.proc_pidpath_audittoken(ctypes.byref(encoded), buffer, len(buffer)) > 0
    except (AttributeError, OSError):
        return False


def _group_snapshots(pgid: int) -> tuple[ProcessSnapshot, ...] | None:
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,state="],
        capture_output=True,
        text=True,
        check=False,
        start_new_session=True,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if completed.returncode != 0:
        return None
    snapshots: list[ProcessSnapshot] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[2].startswith("Z"):
            continue
        try:
            pid = int(fields[0])
            candidate_pgid = int(fields[1])
        except ValueError:
            return None
        if candidate_pgid != pgid:
            continue
        snapshot = _snapshot(pid)
        if snapshot is None or snapshot.pgid != pgid:
            return None
        snapshots.append(snapshot)
    return tuple(snapshots)


def _signal_owned_group(anchor: ProcessSnapshot, requested: signal.Signals) -> bool:
    group = _group_snapshots(anchor.pgid)
    if group is None or not group:
        return False
    live_anchor = next(
        (
            snapshot
            for snapshot in group
            if snapshot.pid == anchor.pid
            and snapshot.signal_token == anchor.signal_token
            and _snapshot_matches(snapshot, _snapshot_payload(anchor))
        ),
        None,
    )
    if live_anchor is None or any(not _token_is_live(snapshot) for snapshot in group):
        return False
    ordered = [snapshot for snapshot in group if snapshot.pid != anchor.pid]
    ordered.append(live_anchor)
    return all(_signal_snapshot(snapshot, requested) for snapshot in ordered)


def _extend_tracked_group(
    anchor: ProcessSnapshot,
    tracked: dict[int, ProcessSnapshot],
) -> tuple[ProcessSnapshot, ...] | None:
    group = _group_snapshots(anchor.pgid)
    if group is None:
        return None
    if _token_is_live(anchor):
        for snapshot in group:
            if not _token_is_live(snapshot):
                return None
            tracked[snapshot.pid] = snapshot
        return group if _token_is_live(anchor) else None
    for snapshot in group:
        expected = tracked.get(snapshot.pid)
        if (
            expected is None
            or expected.signal_token != snapshot.signal_token
            or not _snapshot_matches(snapshot, _snapshot_payload(expected))
            or not _token_is_live(snapshot)
        ):
            return None
    return group


def _signal_tracked_group(
    anchor: ProcessSnapshot,
    tracked: dict[int, ProcessSnapshot],
    requested: signal.Signals,
) -> bool:
    group = _extend_tracked_group(anchor, tracked)
    if group is None:
        return False
    if not group:
        return True
    ordered = [snapshot for snapshot in group if snapshot.pid != anchor.pid]
    ordered.extend(snapshot for snapshot in group if snapshot.pid == anchor.pid)
    return all(_signal_snapshot(snapshot, requested) for snapshot in ordered)


def _tracked_group_eventually(
    anchor: ProcessSnapshot,
    tracked: dict[int, ProcessSnapshot],
    *,
    timeout: float = 0.1,
    on_change: Callable[[dict[int, ProcessSnapshot]], None] | None = None,
) -> tuple[ProcessSnapshot, ...] | None:
    deadline = time.monotonic() + timeout
    while True:
        previous = {pid: snapshot.signal_token for pid, snapshot in tracked.items()}
        group = _extend_tracked_group(anchor, tracked)
        if group is not None:
            current = {pid: snapshot.signal_token for pid, snapshot in tracked.items()}
            if on_change is not None and current != previous:
                on_change(tracked)
            return group
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _signal_group_members(
    group: tuple[ProcessSnapshot, ...],
    anchor_pid: int,
    requested: signal.Signals,
    *,
    protected_pids: frozenset[int] = frozenset(),
) -> bool:
    signalable = [snapshot for snapshot in group if snapshot.pid not in protected_pids]
    ordered = [snapshot for snapshot in signalable if snapshot.pid != anchor_pid]
    ordered.extend(snapshot for snapshot in signalable if snapshot.pid == anchor_pid)
    return all(_signal_snapshot(snapshot, requested) for snapshot in ordered)


def _pid_listens_on_port(snapshot: ProcessSnapshot, port: int) -> bool:
    if port not in {8765, 11435} or not _token_is_live(snapshot):
        return False
    completed = subprocess.run(
        [
            "/usr/sbin/lsof",
            "-nP",
            "-a",
            "-p",
            str(snapshot.pid),
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
            "-Fn",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    return (
        completed.returncode == 0
        and any(line == f"n127.0.0.1:{port}" for line in completed.stdout.splitlines())
        and _token_is_live(snapshot)
    )


def _owned_group_listens(anchor: ProcessSnapshot, port: int) -> bool:
    group = _group_snapshots(anchor.pgid)
    if group is None:
        return False
    live_anchor = next(
        (
            snapshot
            for snapshot in group
            if snapshot.pid == anchor.pid and snapshot.signal_token == anchor.signal_token
        ),
        None,
    )
    return live_anchor is not None and any(
        _pid_listens_on_port(snapshot, port) for snapshot in group
    )


def _owned_group_listens_eventually(
    anchor: ProcessSnapshot,
    port: int,
    *,
    timeout: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _owned_group_listens(anchor, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _http_health(port: int, path: str, key: str, value: str) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(65_537)
        if response.status != 200 or len(body) > 65_536:
            return False
        payload = json.loads(body)
        return isinstance(payload, dict) and payload.get(key) == value
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        connection.close()


def _open_record_lock(parent_descriptor: int) -> int:
    descriptor = os.open(
        ".service-record.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_descriptor,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o777 != 0o600
    ):
        os.close(descriptor)
        raise ServiceError("service ownership lock is unsafe")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _record_boundary(_name: str) -> None:
    return


def _write_record(
    path: Path,
    payload: dict[str, Any],
    *,
    parent_descriptor: int | None = None,
) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    owned_parent_descriptor = (
        _open_record_parent(path) if parent_descriptor is None else os.dup(parent_descriptor)
    )
    try:
        lock_descriptor = _open_record_lock(owned_parent_descriptor)
    except Exception:
        os.close(owned_parent_descriptor)
        raise
    entries: list[RecordEntry] = []
    try:
        records, claims = _scan_record_entries(
            owned_parent_descriptor,
            path.name,
            str(payload["kind"]),
            int(payload["port"]),
        )
        entries = [*records, *claims]
        generation = payload.get("record_generation")
        if type(generation) is not int or generation <= 0:
            raise ServiceError("service ownership record generation is invalid")
        nonce = str(payload["nonce"])
        matching = [entry for entry in entries if entry.payload["nonce"] == nonce]
        if matching:
            newest = max(matching, key=lambda entry: entry.generation)
            if generation != newest.generation + 1:
                raise ServiceError("service ownership record generation changed")
        else:
            if generation != 1:
                raise ServiceError("service ownership record publication is blocked")
            for entry in entries:
                if _verified_record_members(entry.payload) != ():
                    raise ServiceError("service ownership record publication is blocked")
            newest = None
        if newest is not None:
            _record_boundary("write:before_source_check")
            reopened = _open_record_at(
                owned_parent_descriptor,
                newest.name,
                str(payload["kind"]),
                int(payload["port"]),
            )
            if reopened is None:
                raise ServiceError("service ownership record changed before publication")
            reopened_payload, reopened_descriptor, reopened_metadata = reopened
            try:
                if (
                    reopened_payload != newest.payload
                    or reopened_metadata.st_dev != newest.metadata.st_dev
                    or reopened_metadata.st_ino != newest.metadata.st_ino
                ):
                    raise ServiceError("service ownership record changed before publication")
            finally:
                os.close(reopened_descriptor)
        target_name = (
            path.name
            if not entries and _entry_is_absent(owned_parent_descriptor, path.name)
            else f".{path.name}.generation-{nonce}-{generation:020d}"
        )
        _record_boundary("write:before_destination_publish")
        try:
            descriptor = os.open(
                target_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
                dir_fd=owned_parent_descriptor,
            )
        except FileExistsError as exc:
            raise ServiceError("service ownership record publication is blocked") from exc
        try:
            published_metadata = os.fstat(descriptor)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(owned_parent_descriptor)
        published = _open_record_at(
            owned_parent_descriptor,
            target_name,
            str(payload["kind"]),
            int(payload["port"]),
        )
        if published is None:
            raise ServiceError("service ownership record publication is unverified")
        published_payload, published_descriptor, published_final_metadata = published
        try:
            if (
                published_payload != payload
                or published_final_metadata.st_dev != published_metadata.st_dev
                or published_final_metadata.st_ino != published_metadata.st_ino
            ):
                raise ServiceError("service ownership record publication changed")
        finally:
            os.close(published_descriptor)
    finally:
        _close_record_entries(entries)
        os.close(lock_descriptor)
        os.close(owned_parent_descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_child(
    process: subprocess.Popen[bytes],
    *,
    terminate_grace: float,
    kill_wait: float,
    anchor: ProcessSnapshot | None = None,
    tracked: dict[int, ProcessSnapshot] | None = None,
    protected_pids: frozenset[int] = frozenset(),
    on_tracked_change: Callable[[dict[int, ProcessSnapshot]], None] | None = None,
) -> None:
    owned_anchor = anchor or _snapshot(process.pid)
    if owned_anchor is None:
        if process.poll() is not None:
            return
        raise ServiceError("service process group ownership changed before TERM")
    owned = tracked if tracked is not None else {}
    group = _tracked_group_eventually(
        owned_anchor,
        owned,
        on_change=on_tracked_change,
    )
    if group is None:
        raise ServiceError("service process group ownership changed before TERM")
    remaining = tuple(snapshot for snapshot in group if snapshot.pid not in protected_pids)
    if remaining and not _signal_group_members(
        remaining,
        owned_anchor.pid,
        signal.SIGTERM,
        protected_pids=protected_pids,
    ):
        raise ServiceError("service process group ownership changed before TERM")
    term_deadline = time.monotonic() + terminate_grace
    while time.monotonic() < term_deadline:
        if process.poll() is not None:
            break
        observed = _tracked_group_eventually(
            owned_anchor,
            owned,
            on_change=on_tracked_change,
        )
        if observed is None:
            raise ServiceError("service process group ownership changed after TERM")
        remaining = tuple(snapshot for snapshot in observed if snapshot.pid not in protected_pids)
        if not remaining:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0)
            return
        time.sleep(0.02)
    observed = _tracked_group_eventually(
        owned_anchor,
        owned,
        on_change=on_tracked_change,
    )
    if observed is None:
        raise ServiceError("service process group ownership changed before KILL")
    remaining = tuple(snapshot for snapshot in observed if snapshot.pid not in protected_pids)
    if remaining and not _signal_group_members(
        remaining,
        owned_anchor.pid,
        signal.SIGKILL,
        protected_pids=protected_pids,
    ):
        raise ServiceError("service process group ownership changed before KILL")
    deadline = time.monotonic() + kill_wait
    while time.monotonic() < deadline:
        observed = _tracked_group_eventually(
            owned_anchor,
            owned,
            on_change=on_tracked_change,
        )
        if observed is None:
            raise ServiceError("service process group ownership changed after KILL")
        remaining = tuple(snapshot for snapshot in observed if snapshot.pid not in protected_pids)
        if not remaining:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0)
            return
        time.sleep(0.02)
    observed = _tracked_group_eventually(
        owned_anchor,
        owned,
        on_change=on_tracked_change,
    )
    if observed is None or any(snapshot.pid not in protected_pids for snapshot in observed):
        raise ServiceError("service process group did not converge")


def _supervise(arguments: argparse.Namespace) -> int:
    record = arguments.record
    try:
        runtime_metadata = os.fstat(arguments.record_parent_fd)
    except OSError as exc:
        raise ServiceError("service runtime parent descriptor is unavailable") from exc
    if (
        arguments.kind not in {"backend", "ollama"}
        or len(arguments.nonce) != 32
        or not all(character in "0123456789abcdef" for character in arguments.nonce)
        or arguments.port not in {8765, 11435}
        or not arguments.command
        or record.name != f"{arguments.kind}.pid"
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != os.geteuid()
        or runtime_metadata.st_mode & 0o022
    ):
        raise ServiceError("service supervisor arguments are invalid")
    guard = _snapshot(os.getpid())
    try:
        session_id = os.getsid(os.getpid())
    except (ProcessLookupError, PermissionError):
        session_id = -1
    if guard is None or guard.pid != guard.pgid or session_id != guard.pid:
        raise ServiceError("service supervisor is not a private session guard")
    environment = os.environ.copy()
    pass_fds: tuple[int, ...] = ()
    if arguments.activation_fd is not None:
        environment["LVT_PRECOMMIT_ACTIVATION_FD"] = str(arguments.activation_fd)
        pass_fds = (arguments.activation_fd,)
    process = subprocess.Popen(
        arguments.command,
        env=environment,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=pass_fds,
        start_new_session=False,
    )
    if arguments.activation_fd is not None:
        os.close(arguments.activation_fd)
    shutdown = threading.Event()
    service: ProcessSnapshot | None = None
    tracked: dict[int, ProcessSnapshot] = {}
    record_payload: dict[str, Any] | None = None

    def persist_tracked_members(current: dict[int, ProcessSnapshot]) -> None:
        nonlocal record_payload
        if record_payload is None:
            raise ServiceError("service ownership record is unavailable")
        members = [
            _snapshot_payload(snapshot)
            for pid, snapshot in sorted(current.items())
            if pid != guard.pid
        ]
        if members != record_payload["members"]:
            record_payload = {
                **record_payload,
                "record_generation": record_payload["record_generation"] + 1,
                "members": members,
            }
            _write_record(
                record,
                record_payload,
                parent_descriptor=arguments.record_parent_fd,
            )

    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        supervisor = _snapshot(os.getpid())
        service = _stable_snapshot(process.pid)
        if (
            supervisor is None
            or supervisor.signal_token != guard.signal_token
            or service is None
            or service.pgid != guard.pgid
            or os.getsid(service.pid) != guard.pid
            or process.poll() is not None
        ):
            raise ServiceError("service identity is unavailable")
        tracked[supervisor.pid] = supervisor
        tracked[service.pid] = service
        record_payload = {
            "schema_version": 1,
            "record_generation": 1,
            "kind": arguments.kind,
            "nonce": arguments.nonce,
            "port": arguments.port,
            "supervisor": _snapshot_payload(supervisor),
            "service": _snapshot_payload(service),
            "members": [_snapshot_payload(service)],
        }
        _write_record(
            record,
            record_payload,
            parent_descriptor=arguments.record_parent_fd,
        )
        os.write(arguments.ready_fd, b"R")
        os.close(arguments.ready_fd)
        while process.poll() is None and not shutdown.wait(0.05):
            if (
                _tracked_group_eventually(
                    supervisor,
                    tracked,
                    on_change=persist_tracked_members,
                )
                is None
            ):
                raise ServiceError("service process group ownership changed")
        _stop_child(
            process,
            terminate_grace=arguments.terminate_grace,
            kill_wait=arguments.kill_wait,
            anchor=supervisor,
            tracked=tracked,
            protected_pids=frozenset({supervisor.pid}),
            on_tracked_change=persist_tracked_members,
        )
        return 0
    except BaseException as supervision_error:
        print(
            f"{type(supervision_error).__name__}: {supervision_error}",
            file=sys.stderr,
        )
        with suppress(OSError):
            os.close(arguments.ready_fd)
        _stop_child(
            process,
            terminate_grace=arguments.terminate_grace,
            kill_wait=arguments.kill_wait,
            anchor=guard,
            tracked=tracked,
            protected_pids=frozenset({guard.pid}),
            on_tracked_change=(persist_tracked_members if record_payload is not None else None),
        )
        raise


class SystemServiceOperations:
    def __init__(
        self,
        data_root: Path,
        release_root: Path,
        *,
        terminate_grace: float = 5.0,
        kill_wait: float = 5.0,
    ) -> None:
        if terminate_grace <= 0 or kill_wait <= 0:
            raise ValueError("service shutdown timeouts must be positive")
        self.data_root = data_root
        self.release_root = release_root
        self.terminate_grace = terminate_grace
        self.kill_wait = kill_wait
        self._runtime_parent_descriptor = _open_record_parent(
            self.data_root / "runtime/backend.pid"
        )

    def __del__(self) -> None:
        descriptor = getattr(self, "_runtime_parent_descriptor", -1)
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
            self._runtime_parent_descriptor = -1

    def _trusted_runtime_parent(self) -> int:
        try:
            observed = _open_record_parent(self.data_root / "runtime/backend.pid")
        except OSError as exc:
            raise ServiceError("service runtime parent changed") from exc
        try:
            expected_metadata = os.fstat(self._runtime_parent_descriptor)
            observed_metadata = os.fstat(observed)
            if (
                expected_metadata.st_dev != observed_metadata.st_dev
                or expected_metadata.st_ino != observed_metadata.st_ino
            ):
                raise ServiceError("service runtime parent changed")
        finally:
            os.close(observed)
        return os.dup(self._runtime_parent_descriptor)

    def _open_current_record(self, kind: str) -> ClaimedRecord | None:
        parent_descriptor = self._trusted_runtime_parent()
        entries: list[RecordEntry] = []
        try:
            records, claims = _scan_record_entries(
                parent_descriptor,
                f"{kind}.pid",
                kind,
                self._port(kind),
            )
            entries = [*records, *claims]
            selected = _select_record_entry(records, claims, kind)
            if selected is None:
                os.close(parent_descriptor)
                return None
            _close_record_entries(entries, except_fd=selected.descriptor)
            return ClaimedRecord(
                parent_descriptor,
                selected.descriptor,
                selected.name,
                selected.payload,
            )
        except Exception:
            _close_record_entries(entries)
            with suppress(OSError):
                os.close(parent_descriptor)
            raise

    def _existing_claim(
        self,
        kind: str,
        *,
        preferred_nonce: str | None = None,
    ) -> ClaimedRecord | None:
        parent_descriptor = self._trusted_runtime_parent()
        entries: list[RecordEntry] = []
        try:
            records, claims = _scan_record_entries(
                parent_descriptor,
                f"{kind}.pid",
                kind,
                self._port(kind),
            )
            entries = [*records, *claims]
            selected = _select_record_entry(
                [],
                claims,
                kind,
                preferred_nonce=preferred_nonce,
            )
            if selected is None:
                os.close(parent_descriptor)
                return None
            _close_record_entries(entries, except_fd=selected.descriptor)
            return ClaimedRecord(
                parent_descriptor,
                selected.descriptor,
                selected.name,
                selected.payload,
            )
        except Exception:
            _close_record_entries(entries)
            with suppress(OSError):
                os.close(parent_descriptor)
            raise

    def _claim_record(
        self,
        kind: str,
        *,
        preferred_nonce: str | None = None,
    ) -> ClaimedRecord | None:
        parent_descriptor = self._trusted_runtime_parent()
        lock_descriptor = _open_record_lock(parent_descriptor)
        entries: list[RecordEntry] = []
        try:
            records, claims = _scan_record_entries(
                parent_descriptor,
                f"{kind}.pid",
                kind,
                self._port(kind),
            )
            entries = [*records, *claims]
            selected = _select_record_entry(
                records,
                claims,
                kind,
                preferred_nonce=preferred_nonce,
            )
            if selected is None:
                os.close(parent_descriptor)
                return None
            _record_boundary("claim:before_source_check")
            reopened = _open_record_at(
                parent_descriptor,
                selected.name,
                kind,
                self._port(kind),
            )
            if reopened is None:
                raise ServiceError(f"{kind} ownership changed during claim")
            reopened_payload, reopened_descriptor, reopened_metadata = reopened
            try:
                if (
                    reopened_payload != selected.payload
                    or reopened_metadata.st_dev != selected.metadata.st_dev
                    or reopened_metadata.st_ino != selected.metadata.st_ino
                ):
                    raise ServiceError(f"{kind} ownership changed during claim")
            finally:
                os.close(reopened_descriptor)
            _close_record_entries(entries, except_fd=selected.descriptor)
            return ClaimedRecord(
                parent_descriptor,
                selected.descriptor,
                selected.name,
                selected.payload,
            )
        except Exception:
            _close_record_entries(entries)
            os.close(parent_descriptor)
            raise
        finally:
            with suppress(OSError):
                os.close(lock_descriptor)

    def reconcile(self) -> None:
        from publish_install import FirstInstallPublisher

        FirstInstallPublisher(self.data_root, self.release_root).reconcile(lock_held=True)
        for kind in ("backend", "ollama"):
            self._reconcile_orphan(kind)

    def validate(self, phase: str) -> ValidationOutcome:
        completed = subprocess.run(
            [
                str(self.release_root / ".venv/bin/python"),
                str(self.release_root / "packaging/tools/verify_install.py"),
                "--phase",
                phase,
                "--data-root",
                str(self.data_root),
                "--release-root",
                str(self.release_root),
                "--json",
            ],
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            report = json.loads(completed.stdout)
            status = str(report["status"])
            exit_code = int(report["exit_code"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return ValidationOutcome(2, "invalid")
        if completed.returncode != exit_code:
            return ValidationOutcome(2, "invalid")
        return ValidationOutcome(exit_code, status)

    def state(self, kind: str) -> str:
        port = self._port(kind)
        try:
            record = self._open_current_record(kind)
        except ServiceError:
            return "unsafe"
        if record is None:
            return "unsafe" if _port_open(port) else "absent"
        try:
            members = _verified_record_members_eventually(record.payload)
            if members == () and not _port_open(port):
                return "absent"
            verified = _verified_record_payload_eventually(record.payload, kind)
            return (
                "owned"
                if verified is not None and _owned_group_listens_eventually(verified[1], port)
                else "unsafe"
            )
        finally:
            record.close()

    def launch(self, kind: str, activation_fd: int | None = None) -> ServiceIdentity:
        if self.state(kind) != "absent":
            raise ServiceError(f"{kind} service is not safe to launch")
        command, environment = self._service_command(kind)
        nonce = uuid.uuid4().hex
        record_path = self._record_path(kind)
        runtime_parent_descriptor = self._trusted_runtime_parent()
        ready_read, ready_write = os.pipe()
        supervisor = Path(__file__).resolve(strict=True)
        python = self.release_root / ".venv/bin/python"
        arguments = [
            str(python),
            str(supervisor),
            "supervise",
            "--record",
            str(record_path),
            "--record-parent-fd",
            str(runtime_parent_descriptor),
            "--kind",
            kind,
            "--nonce",
            nonce,
            "--port",
            str(self._port(kind)),
            "--ready-fd",
            str(ready_write),
            "--terminate-grace",
            str(self.terminate_grace),
            "--kill-wait",
            str(self.kill_wait),
        ]
        pass_fds = [ready_write, runtime_parent_descriptor]
        if activation_fd is not None:
            arguments.extend(["--activation-fd", str(activation_fd)])
            pass_fds.append(activation_fd)
        arguments.extend(["--", *command])
        log_path = self.data_root / "logs" / f"{kind}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with log_path.open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    arguments,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    close_fds=True,
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                )
        except Exception:
            os.close(ready_read)
            os.close(ready_write)
            os.close(runtime_parent_descriptor)
            raise
        os.close(ready_write)
        os.close(runtime_parent_descriptor)
        try:
            readable, _, _ = select.select([ready_read], [], [], 5)
            ready = os.read(ready_read, 1) if readable else b""
        finally:
            os.close(ready_read)
        if ready != b"R" or process.poll() is not None:
            _stop_child(
                process,
                terminate_grace=self.terminate_grace,
                kill_wait=self.kill_wait,
            )
            raise ServiceError(f"{kind} service supervisor failed to start")
        deadline = time.monotonic() + 10
        port = self._port(kind)
        while time.monotonic() < deadline:
            try:
                record = self._open_current_record(kind)
            except ServiceError:
                record = None
            if record is not None:
                try:
                    verified = _verified_record_payload_eventually(record.payload, kind)
                    if verified is not None and _owned_group_listens(verified[1], port):
                        return _service_identity(kind, record.payload, verified)
                finally:
                    record.close()
            if process.poll() is not None:
                break
            time.sleep(0.05)
        try:
            self._stop_recorded_service(kind)
        except ServiceError as cleanup_error:
            raise ServiceError(
                f"{kind} service failed owned-listener verification and cleanup"
            ) from cleanup_error
        raise ServiceError(f"{kind} service failed owned-listener verification")

    def backend_healthy(self) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _http_health(8765, "/health", "status", "healthy"):
                return True
            time.sleep(0.05)
        return False

    def stop(self, kind: str) -> None:
        self._stop_recorded_service(kind)
        if kind == "backend":
            self._reconcile_tools()

    def stop_matching(self, kind: str, expected: ServiceIdentity) -> None:
        self._stop_recorded_service(kind, expected=expected)
        if kind == "backend":
            self._reconcile_tools()

    def _stop_recorded_service(
        self,
        kind: str,
        *,
        expected: ServiceIdentity | None = None,
    ) -> None:
        record = self._claim_record(kind)
        if record is None:
            if not _port_open(self._port(kind)):
                return
            raise ServiceError(f"{kind} service ownership is unverified")
        try:
            payload = record.payload
            if expected is not None and not _record_matches_identity(payload, expected):
                raise ServiceError(f"{kind} service generation changed")
            verified = _verified_record_payload_eventually(payload, kind)
            if verified is None:
                self._stop_recorded_members(kind, payload)
                self._remove_converged_record(record, kind)
                return
            current = _service_identity(kind, payload, verified)
            if expected is not None and current != expected:
                raise ServiceError(f"{kind} service generation changed")
            supervisor, _service = verified
            if not _signal_snapshot(supervisor, signal.SIGTERM):
                raise ServiceError(f"{kind} supervisor signal was rejected")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not _token_is_live(supervisor):
                    break
                time.sleep(0.05)
            latest = self._claim_record(kind, preferred_nonce=str(payload["nonce"]))
            if latest is not None:
                if latest.payload["nonce"] != payload["nonce"]:
                    latest.close()
                    raise ServiceError(f"{kind} service generation changed")
                record.close()
                record = latest
                payload = latest.payload
            self._stop_recorded_members(kind, payload)
            self._remove_converged_record(record, kind)
        finally:
            record.close()

    def _reconcile_orphan(self, kind: str) -> None:
        record = self._open_current_record(kind)
        if record is not None:
            try:
                if _verified_record_payload_eventually(record.payload, kind) is not None:
                    return
            finally:
                record.close()
        record = self._claim_record(kind)
        if record is None:
            return
        try:
            self._stop_recorded_members(kind, record.payload)
            self._remove_converged_record(record, kind)
        finally:
            record.close()

    def _stop_recorded_members(self, kind: str, payload: dict[str, Any]) -> None:
        live = _verified_record_members(payload)
        if live is None:
            raise ServiceError(f"{kind} orphan ownership is unverified")
        if live and not all(
            _signal_snapshot(snapshot, signal.SIGTERM)
            for snapshot in sorted(live, key=lambda item: item.pid, reverse=True)
        ):
            raise ServiceError(f"{kind} orphan TERM signal was rejected")
        deadline = time.monotonic() + self.terminate_grace
        while time.monotonic() < deadline:
            remaining = _verified_record_members(payload)
            if remaining is None:
                raise ServiceError(f"{kind} orphan ownership changed after TERM")
            if not remaining:
                return
            time.sleep(0.02)
        remaining = _verified_record_members(payload)
        if remaining is None:
            raise ServiceError(f"{kind} orphan ownership changed before KILL")
        if remaining and not all(
            _signal_snapshot(snapshot, signal.SIGKILL)
            for snapshot in sorted(remaining, key=lambda item: item.pid, reverse=True)
        ):
            raise ServiceError(f"{kind} orphan KILL signal was rejected")
        deadline = time.monotonic() + self.kill_wait
        while time.monotonic() < deadline:
            remaining = _verified_record_members(payload)
            if remaining is None:
                raise ServiceError(f"{kind} orphan ownership changed after KILL")
            if not remaining:
                return
            time.sleep(0.02)
        raise ServiceError(f"{kind} orphan members did not converge")

    def _remove_converged_record(
        self,
        record: ClaimedRecord,
        kind: str,
    ) -> None:
        payload = record.payload
        if _verified_record_members(payload) != ():
            raise ServiceError(f"{kind} recorded members did not converge")
        if _port_open(self._port(kind)):
            raise ServiceError(f"{kind} port remains occupied after cleanup")
        opened = _open_record_at(
            record.parent_descriptor,
            record.name,
            kind,
            self._port(kind),
        )
        if opened is None:
            raise ServiceError(f"{kind} ownership record changed before cleanup")
        current_payload, descriptor, metadata = opened
        try:
            held_metadata = os.fstat(record.descriptor)
            if (
                current_payload != payload
                or metadata.st_dev != held_metadata.st_dev
                or metadata.st_ino != held_metadata.st_ino
            ):
                raise ServiceError(f"{kind} ownership record changed before cleanup")
        finally:
            os.close(descriptor)
        os.fsync(record.parent_descriptor)

    def ownership_records_converged(self) -> bool:
        process_root = self.data_root / "runtime/processes"
        return not process_root.exists() or not any(process_root.rglob("*.json"))

    def _record_path(self, kind: str) -> Path:
        if kind not in {"backend", "ollama"}:
            raise ServiceError("unknown service kind")
        return self.data_root / "runtime" / f"{kind}.pid"

    @staticmethod
    def _port(kind: str) -> int:
        return 8765 if kind == "backend" else 11435

    def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
        environment = {
            "HOME": os.environ.get("HOME", "/"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }
        if kind == "ollama":
            dependencies = json.loads(
                (self.release_root / "packaging/dependencies.json").read_text(encoding="utf-8")
            )
            artifact = next(
                item
                for item in dependencies["artifacts"]
                if isinstance(item, dict) and item.get("id") == "ollama"
            )
            executable = (
                self.data_root / "app/tools/ollama" / str(artifact["version"]) / "bin/ollama"
            )
            environment.update(
                {
                    "OLLAMA_HOST": "127.0.0.1:11435",
                    "OLLAMA_MODELS": str(self.data_root / "models/ollama"),
                }
            )
            return [str(executable), "serve"], environment
        state = json.loads(
            (self.data_root / "runtime/install-state.json").read_text(encoding="utf-8")
        )
        ffmpeg_dir = self.data_root / "app" / str(state["ffmpeg"]["directory"])
        environment.update(
            {
                "LVT_DATA_ROOT": str(self.data_root),
                "LVT_MODEL_ROOT": str(self.data_root / "models"),
                "LVT_INSTALLED_MODE": "1",
                "LVT_FFMPEG_DIR": str(ffmpeg_dir),
                "LVT_OLLAMA_URL": "http://127.0.0.1:11435",
                "LVT_WORKER_CONCURRENCY": "1",
                "PYTHONPATH": str(self.release_root / "backend/src"),
            }
        )
        return [str(self.release_root / ".venv/bin/python"), "-m", "lvt.main"], environment

    def _reconcile_tools(self) -> None:
        process_root = self.data_root / "runtime/processes"
        completed = subprocess.run(
            [
                str(self.release_root / ".venv/bin/python"),
                str(self.release_root / "packaging/tools/reconcile_processes.py"),
                "--process-root",
                str(process_root),
                "--json",
            ],
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ServiceError("tool process ownership did not converge")


def main(argv: list[str] | None = None) -> int:
    selected = list(sys.argv[1:] if argv is None else argv)
    if selected[:1] == ["supervise"]:
        parser = argparse.ArgumentParser(description="监督单个应用服务")
        parser.add_argument("--record", required=True, type=Path)
        parser.add_argument("--record-parent-fd", required=True, type=int)
        parser.add_argument("--kind", required=True)
        parser.add_argument("--nonce", required=True)
        parser.add_argument("--port", required=True, type=int)
        parser.add_argument("--ready-fd", required=True, type=int)
        parser.add_argument("--activation-fd", type=int)
        parser.add_argument("--terminate-grace", required=True, type=float)
        parser.add_argument("--kill-wait", required=True, type=float)
        parser.add_argument("command", nargs=argparse.REMAINDER)
        arguments = parser.parse_args(selected[1:])
        if arguments.command[:1] == ["--"]:
            arguments.command = arguments.command[1:]
        try:
            return _supervise(arguments)
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 70

    parser = argparse.ArgumentParser(description="管理 Local Video Transcriber 本地服务")
    parser.add_argument("action", choices=("prestart", "start", "stop"))
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    arguments = parser.parse_args(selected)
    manager = LifecycleManager(arguments.data_root, arguments.release_root)
    try:
        if arguments.action == "prestart":
            result = manager.prestart()
        elif arguments.action == "start":
            result = manager.start()
        else:
            result = manager.stop()
    except Exception:
        result = LifecycleResult(2, "unsafe_or_corrupt")
    print(
        json.dumps(
            {"schema_version": 1, "status": result.status},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
