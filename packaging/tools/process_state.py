#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
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


class ServiceOperations(Protocol):
    def reconcile(self) -> None: ...

    def validate(self, phase: str) -> ValidationOutcome: ...

    def state(self, kind: str) -> str: ...

    def launch(self, kind: str, activation_fd: int | None = None) -> None: ...

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
        ):
            return None
        return supervisor, service
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _read_record_payload(path: Path, kind: str, port: int) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o777 != 0o600
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "schema_version",
                "kind",
                "nonce",
                "port",
                "supervisor",
                "service",
            }
            or payload.get("schema_version") != 1
            or payload.get("kind") != kind
            or payload.get("port") != port
            or not isinstance(payload.get("nonce"), str)
            or len(payload["nonce"]) != 32
            or any(character not in "0123456789abcdef" for character in payload["nonce"])
        ):
            return None
        for name in ("supervisor", "service"):
            if not _snapshot_payload_is_valid(payload.get(name)):
                return None
        return payload
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _verified_service_from_record(
    path: Path,
    kind: str,
    port: int,
) -> ProcessSnapshot | None:
    payload = _read_record_payload(path, kind, port)
    if payload is None:
        return None
    service = _snapshot(payload["service"]["pid"])
    if service is None or not _snapshot_matches(service, payload["service"]):
        return None
    return service


def _snapshot_matches_pid_payload(expected: Any) -> bool:
    if not isinstance(expected, dict) or type(expected.get("pid")) is not int:
        return False
    snapshot = _snapshot(expected["pid"])
    return snapshot is not None and _snapshot_matches(snapshot, expected)


def verify_owned_service_record(
    path: Path,
    kind: str,
    port: int,
    *,
    require_listener: bool = False,
) -> bool:
    verified = _verified_record(path, kind, port)
    if verified is None:
        return False
    return not require_listener or _owned_group_listens(verified[1], port)


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
    if live_anchor is not None:
        for snapshot in group:
            if not _token_is_live(snapshot):
                return None
            tracked[snapshot.pid] = snapshot
        return group
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


def _signal_group_members(
    group: tuple[ProcessSnapshot, ...],
    anchor_pid: int,
    requested: signal.Signals,
) -> bool:
    ordered = [snapshot for snapshot in group if snapshot.pid != anchor_pid]
    ordered.extend(snapshot for snapshot in group if snapshot.pid == anchor_pid)
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


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


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
) -> None:
    owned_anchor = anchor or _snapshot(process.pid)
    if owned_anchor is None:
        if process.poll() is not None:
            return
        raise ServiceError("service process group ownership changed before TERM")
    owned = tracked if tracked is not None else {}
    group = _extend_tracked_group(owned_anchor, owned)
    if group is None:
        raise ServiceError("service process group ownership changed before TERM")
    if group and not _signal_group_members(group, owned_anchor.pid, signal.SIGTERM):
        raise ServiceError("service process group ownership changed before TERM")
    term_deadline = time.monotonic() + terminate_grace
    while time.monotonic() < term_deadline:
        if process.poll() is not None:
            break
        remaining = _extend_tracked_group(owned_anchor, owned)
        if remaining is None:
            raise ServiceError("service process group ownership changed after TERM")
        if not remaining:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0)
            return
        time.sleep(0.02)
    remaining = _extend_tracked_group(owned_anchor, owned)
    if remaining is None:
        raise ServiceError("service process group ownership changed before KILL")
    if remaining and not _signal_group_members(remaining, owned_anchor.pid, signal.SIGKILL):
        raise ServiceError("service process group ownership changed before KILL")
    deadline = time.monotonic() + kill_wait
    while time.monotonic() < deadline:
        remaining = _extend_tracked_group(owned_anchor, owned)
        if remaining is None:
            raise ServiceError("service process group ownership changed after KILL")
        if not remaining:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0)
            return
        time.sleep(0.02)
    if _extend_tracked_group(owned_anchor, owned):
        raise ServiceError("service process group did not converge")


def _supervise(arguments: argparse.Namespace) -> int:
    record = arguments.record
    if (
        arguments.kind not in {"backend", "ollama"}
        or len(arguments.nonce) != 32
        or not all(character in "0123456789abcdef" for character in arguments.nonce)
        or arguments.port not in {8765, 11435}
        or not arguments.command
    ):
        raise ServiceError("service supervisor arguments are invalid")
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
        start_new_session=True,
    )
    if arguments.activation_fd is not None:
        os.close(arguments.activation_fd)
    shutdown = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        supervisor = _snapshot(os.getpid())
        service = _snapshot(process.pid)
        if supervisor is None or service is None or process.poll() is not None:
            raise ServiceError("service identity is unavailable")
        _write_record(
            record,
            {
                "schema_version": 1,
                "kind": arguments.kind,
                "nonce": arguments.nonce,
                "port": arguments.port,
                "supervisor": _snapshot_payload(supervisor),
                "service": _snapshot_payload(service),
            },
        )
        os.write(arguments.ready_fd, b"R")
        os.close(arguments.ready_fd)
        tracked = {service.pid: service}
        while process.poll() is None and not shutdown.wait(0.05):
            if _extend_tracked_group(service, tracked) is None:
                raise ServiceError("service process group ownership changed")
        _stop_child(
            process,
            terminate_grace=arguments.terminate_grace,
            kill_wait=arguments.kill_wait,
            anchor=service,
            tracked=tracked,
        )
        record.unlink(missing_ok=True)
        _fsync_directory(record.parent)
        return 0
    except BaseException:
        with suppress(OSError):
            os.close(arguments.ready_fd)
        _stop_child(
            process,
            terminate_grace=arguments.terminate_grace,
            kill_wait=arguments.kill_wait,
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
        record_path = self._record_path(kind)
        port = self._port(kind)
        if not record_path.exists():
            return "unsafe" if _port_open(port) else "absent"
        verified = _verified_record(record_path, kind, port)
        return (
            "owned"
            if verified is not None and _owned_group_listens(verified[1], port)
            else "unsafe"
        )

    def launch(self, kind: str, activation_fd: int | None = None) -> None:
        if self.state(kind) != "absent":
            raise ServiceError(f"{kind} service is not safe to launch")
        command, environment = self._service_command(kind)
        nonce = uuid.uuid4().hex
        record_path = self._record_path(kind)
        record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ready_read, ready_write = os.pipe()
        supervisor = Path(__file__).resolve(strict=True)
        python = self.release_root / ".venv/bin/python"
        arguments = [
            str(python),
            str(supervisor),
            "supervise",
            "--record",
            str(record_path),
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
        pass_fds = [ready_write]
        if activation_fd is not None:
            arguments.extend(["--activation-fd", str(activation_fd)])
            pass_fds.append(activation_fd)
        arguments.extend(["--", *command])
        log_path = self.data_root / "logs" / f"{kind}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        os.close(ready_write)
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
            verified = _verified_record(record_path, kind, port)
            if verified is not None and _owned_group_listens(verified[1], port):
                return
            if process.poll() is not None or _port_open(port):
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

    def _stop_recorded_service(self, kind: str) -> None:
        record_path = self._record_path(kind)
        verified = _verified_record(record_path, kind, self._port(kind))
        if verified is None:
            if not record_path.exists() and not _port_open(self._port(kind)):
                return
            orphan = _verified_service_from_record(record_path, kind, self._port(kind))
            if orphan is not None:
                self._stop_orphan_service(orphan)
                self._remove_converged_record(record_path, kind, orphan.pgid)
                return
            raise ServiceError(f"{kind} service ownership is unverified")
        supervisor, _service = verified
        if not _signal_snapshot(supervisor, signal.SIGTERM):
            raise ServiceError(f"{kind} supervisor signal was rejected")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not record_path.exists():
                if _port_open(self._port(kind)):
                    raise ServiceError(f"{kind} foreign listener remains after shutdown")
                return
            time.sleep(0.05)
        verified = _verified_record(record_path, kind, self._port(kind))
        if verified is None:
            raise ServiceError(f"{kind} service changed during shutdown")
        for snapshot in verified:
            if not _signal_snapshot(snapshot, signal.SIGKILL):
                raise ServiceError(f"{kind} kill signal was rejected")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _port_open(self._port(kind)):
                record_path.unlink(missing_ok=True)
                _fsync_directory(record_path.parent)
                return
            time.sleep(0.05)
        raise ServiceError(f"{kind} service did not stop")

    def _reconcile_orphan(self, kind: str) -> None:
        record_path = self._record_path(kind)
        if not record_path.exists():
            return
        if _verified_record(record_path, kind, self._port(kind)) is not None:
            return
        orphan = _verified_service_from_record(record_path, kind, self._port(kind))
        if orphan is not None:
            self._stop_orphan_service(orphan)
            self._remove_converged_record(record_path, kind, orphan.pgid)
            return
        payload = _read_record_payload(record_path, kind, self._port(kind))
        if payload is not None and not _port_open(self._port(kind)):
            supervisor_live = _snapshot_matches_pid_payload(payload["supervisor"])
            service_live = _snapshot_matches_pid_payload(payload["service"])
            if not supervisor_live and not service_live:
                record_path.unlink()
                _fsync_directory(record_path.parent)
                return
        raise ServiceError(f"{kind} orphan ownership is unverified")

    def _stop_orphan_service(self, service: ProcessSnapshot) -> None:
        class RecordedProcess:
            pid = service.pid

            @staticmethod
            def poll() -> int | None:
                return None if _token_is_live(service) else 0

            @staticmethod
            def wait(timeout: float) -> int:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if not _token_is_live(service):
                        return 0
                    time.sleep(0.02)
                raise subprocess.TimeoutExpired(str(service.pid), timeout)

        _stop_child(
            RecordedProcess(),  # type: ignore[arg-type]
            terminate_grace=self.terminate_grace,
            kill_wait=self.kill_wait,
            anchor=service,
        )

    def _remove_converged_record(self, record_path: Path, kind: str, pgid: int) -> None:
        if _group_snapshots(pgid) not in {None, ()}:
            raise ServiceError(f"{kind} process group did not converge")
        if _port_open(self._port(kind)):
            raise ServiceError(f"{kind} port remains occupied after cleanup")
        record_path.unlink(missing_ok=True)
        _fsync_directory(record_path.parent)

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
        except Exception:
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
