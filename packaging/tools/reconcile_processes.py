#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SAFE_KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
HEX_32 = re.compile(r"[0-9a-f]{32}")
HEX_64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    pgid: int
    start_time: str
    executable: Path
    device: int | None = None
    inode: int | None = None
    sha256: str | None = None
    ownership_nonce: str | None = None
    signal_token: tuple[int, ...] | None = None


@dataclass(frozen=True)
class ReconcileReport:
    status: str
    code: str
    cleaned: int
    unverified: int


class ProcessInspector(Protocol):
    def snapshot(self, pid: int) -> ProcessSnapshot | None: ...

    def group_exists(self, pgid: int) -> bool: ...

    def group_snapshots(self, pgid: int) -> tuple[ProcessSnapshot, ...] | None: ...


class ProcessSignaller(Protocol):
    def signal_process(
        self,
        target: ProcessSnapshot,
        requested: signal.Signals,
    ) -> bool: ...

    def signal_owned(
        self,
        supervisor: ProcessSnapshot,
        group: tuple[ProcessSnapshot, ...],
        requested: signal.Signals,
    ) -> bool: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _executable_path(pid: int) -> Path | None:
    proc_path = Path(f"/proc/{pid}/exe")
    if proc_path.exists():
        try:
            return proc_path.resolve(strict=True)
        except OSError:
            return None
    if sys.platform != "darwin":
        return None
    buffer = ctypes.create_string_buffer(4096)
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        length = libproc.proc_pidpath(pid, buffer, len(buffer))
    except (AttributeError, OSError):
        return None
    if length <= 0:
        return None
    try:
        return Path(os.fsdecode(buffer.value)).resolve(strict=True)
    except OSError:
        return None


def _darwin_audit_token(pid: int) -> tuple[int, ...] | None:
    if sys.platform != "darwin":
        return None
    token = (ctypes.c_uint * 8)()
    count = ctypes.c_uint(len(token))
    task = ctypes.c_uint()
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
        if task.value:
            with suppress(AttributeError, OSError):
                system.mach_port_deallocate(self_task, task.value)


class SystemProcessInspector:
    def snapshot(self, pid: int) -> ProcessSnapshot | None:
        if type(pid) is not int or pid <= 0:
            return None
        signal_token_before = _darwin_audit_token(pid)
        if signal_token_before is None:
            return None
        try:
            os.kill(pid, 0)
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            return None
        start_completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        command_completed = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        start_time = " ".join(start_completed.stdout.split())
        command = command_completed.stdout.strip()
        nonce = None
        try:
            command_parts = shlex.split(command)
            nonce_index = command_parts.index("--ownership-nonce")
            nonce = command_parts[nonce_index + 1]
        except (ValueError, IndexError):
            pass
        executable = _executable_path(pid)
        if (
            start_completed.returncode != 0
            or command_completed.returncode != 0
            or not start_time
            or executable is None
        ):
            return None
        try:
            metadata = executable.stat()
            if not stat.S_ISREG(metadata.st_mode):
                return None
            digest = _sha256(executable)
        except OSError:
            return None
        signal_token_after = _darwin_audit_token(pid)
        if signal_token_after != signal_token_before:
            return None
        return ProcessSnapshot(
            pid=pid,
            pgid=pgid,
            start_time=start_time,
            executable=executable,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            sha256=digest,
            ownership_nonce=nonce,
            signal_token=signal_token_after,
        )

    def group_exists(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def group_snapshots(self, pgid: int) -> tuple[ProcessSnapshot, ...] | None:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid=,state="],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return None
        snapshots: list[ProcessSnapshot] = []
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[2].startswith("Z"):
                continue
            try:
                pid, candidate_pgid = (int(field) for field in fields[:2])
            except ValueError:
                continue
            if candidate_pgid != pgid:
                continue
            snapshot = self.snapshot(pid)
            if snapshot is None or snapshot.pgid != pgid:
                return None
            snapshots.append(snapshot)
        return tuple(snapshots)


class SystemProcessSignaller:
    def __init__(self) -> None:
        self._system = (
            ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            if sys.platform == "darwin"
            else None
        )

    def _token_is_live(self, token: tuple[int, ...] | None) -> bool:
        if self._system is None or token is None or len(token) != 8:
            return False
        encoded = (ctypes.c_uint * 8)(*token)
        buffer = ctypes.create_string_buffer(4096)
        return (
            self._system.proc_pidpath_audittoken(
                ctypes.byref(encoded),
                buffer,
                len(buffer),
            )
            > 0
        )

    def _signal_token(
        self,
        token: tuple[int, ...] | None,
        requested: signal.Signals,
    ) -> bool:
        if self._system is None or token is None or len(token) != 8:
            return False
        encoded = (ctypes.c_uint * 8)(*token)
        return (
            self._system.proc_signal_with_audittoken(
                ctypes.byref(encoded),
                int(requested),
            )
            == 0
        )

    def signal_process(
        self,
        target: ProcessSnapshot,
        requested: signal.Signals,
    ) -> bool:
        if not self._token_is_live(target.signal_token):
            return False
        return self._signal_token(target.signal_token, requested)

    def signal_owned(
        self,
        supervisor: ProcessSnapshot,
        group: tuple[ProcessSnapshot, ...],
        requested: signal.Signals,
    ) -> bool:
        targets = (supervisor, *group)
        if not targets or any(not self._token_is_live(item.signal_token) for item in targets):
            return False
        if not self._signal_token(supervisor.signal_token, requested):
            return False
        for target in group:
            self._signal_token(target.signal_token, requested)
        return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _valid_executable_identity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"realpath", "device", "inode", "sha256"}
        and isinstance(value.get("realpath"), str)
        and Path(value["realpath"]).is_absolute()
        and type(value.get("device")) is int
        and type(value.get("inode")) is int
        and isinstance(value.get("sha256"), str)
        and HEX_64.fullmatch(value["sha256"]) is not None
    )


def _valid_process_identity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"pid", "pgid", "start_time", "executable"}
        and type(value.get("pid")) is int
        and value["pid"] > 0
        and type(value.get("pgid")) is int
        and value["pgid"] > 0
        and isinstance(value.get("start_time"), str)
        and bool(value["start_time"])
        and _valid_executable_identity(value.get("executable"))
    )


def _valid_record(record: Any, path: Path, process_root: Path) -> bool:
    if (
        not isinstance(record, dict)
        or set(record)
        != {
            "schema_version",
            "job_id",
            "run_id",
            "kind",
            "ownership_nonce",
            "created_at",
            "lifecycle_state",
            "supervisor",
            "tool",
        }
        or record.get("schema_version") != 1
        or record.get("lifecycle_state") not in {"running", "completed", "cleanup_failed"}
        or not isinstance(record.get("created_at"), str)
        or HEX_32.fullmatch(record.get("ownership_nonce", "")) is None
        or SAFE_KIND.fullmatch(record.get("kind", "")) is None
        or not _valid_process_identity(record.get("supervisor"))
        or not _valid_process_identity(record.get("tool"))
    ):
        return False
    try:
        if str(uuid.UUID(record["job_id"])) != record["job_id"]:
            return False
        if str(uuid.UUID(record["run_id"])) != record["run_id"]:
            return False
        if path.name != f"{record['kind']}.json" or path.parent.name != record["run_id"]:
            return False
        path.parent.resolve(strict=True).relative_to(process_root)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _identity_matches(expected: dict[str, Any], actual: ProcessSnapshot | None) -> bool:
    if actual is None:
        return False
    executable = expected["executable"]
    try:
        resolved = actual.executable.resolve(strict=True)
        metadata = resolved.stat()
        digest = actual.sha256 if actual.sha256 is not None else _sha256(resolved)
        device = actual.device if actual.device is not None else metadata.st_dev
        inode = actual.inode if actual.inode is not None else metadata.st_ino
    except OSError:
        return False
    return bool(
        actual.pid == expected["pid"]
        and actual.pgid == expected["pgid"]
        and actual.start_time == expected["start_time"]
        and str(resolved) == executable["realpath"]
        and device == executable["device"]
        and inode == executable["inode"]
        and digest == executable["sha256"]
    )


def _ownership_snapshots(
    record: dict[str, Any],
    inspector: ProcessInspector,
) -> tuple[ProcessSnapshot, ProcessSnapshot] | None:
    supervisor = inspector.snapshot(record["supervisor"]["pid"])
    tool = inspector.snapshot(record["tool"]["pid"])
    ownership_nonce = record.get("ownership_nonce")
    if (
        isinstance(ownership_nonce, str)
        and _identity_matches(
            record["supervisor"],
            supervisor,
        )
        and supervisor is not None
        and supervisor.ownership_nonce == ownership_nonce
        and _identity_matches(
            record["tool"],
            tool,
        )
        and supervisor.signal_token is not None
        and tool is not None
        and tool.signal_token is not None
    ):
        return supervisor, tool
    return None


def _wait_for_exit(
    record: dict[str, Any],
    inspector: ProcessInspector,
    timeout: float,
    poll_interval: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if inspector.snapshot(record["supervisor"]["pid"]) is None and not inspector.group_exists(
            record["tool"]["pgid"]
        ):
            return True
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
    return inspector.snapshot(record["supervisor"]["pid"]) is None and not inspector.group_exists(
        record["tool"]["pgid"]
    )


def _remove_record(path: Path, process_root: Path) -> None:
    path.unlink(missing_ok=True)
    if not path.parent.exists():
        _fsync_directory(process_root)
        return
    _fsync_directory(path.parent)
    try:
        path.parent.rmdir()
    except OSError:
        return
    _fsync_directory(process_root)


def _quarantine_record(path: Path, process_root: Path) -> None:
    quarantine = process_root / "quarantine"
    quarantine.mkdir(mode=0o700, exist_ok=True)
    if quarantine.is_symlink() or not quarantine.is_dir():
        raise RuntimeError("process quarantine is unsafe")
    destination = quarantine / f"{path.parent.name}-{path.stem}-{uuid.uuid4().hex}.json"
    os.replace(path, destination)
    _fsync_directory(path.parent)
    _fsync_directory(quarantine)


def _reconcile_record(
    path: Path,
    process_root: Path,
    inspector: ProcessInspector,
    signaller: ProcessSignaller,
    *,
    terminate_grace: float,
    kill_wait: float,
    poll_interval: float,
) -> bool:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o777 != 0o600
        ):
            raise ValueError
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        _quarantine_record(path, process_root)
        return False
    if not _valid_record(record, path, process_root):
        _quarantine_record(path, process_root)
        return False
    if record["lifecycle_state"] == "completed":
        _remove_record(path, process_root)
        return True
    ownership = _ownership_snapshots(record, inspector)
    if ownership is None:
        _quarantine_record(path, process_root)
        return False

    if not signaller.signal_process(ownership[0], signal.SIGTERM):
        _quarantine_record(path, process_root)
        return False
    if _wait_for_exit(record, inspector, terminate_grace, poll_interval):
        _remove_record(path, process_root)
        return True

    ownership = _ownership_snapshots(record, inspector)
    if ownership is None:
        _quarantine_record(path, process_root)
        return False
    group = inspector.group_snapshots(record["tool"]["pgid"])
    final_ownership = _ownership_snapshots(record, inspector)
    if (
        group is None
        or not group
        or final_ownership is None
        or not any(
            snapshot.signal_token == final_ownership[1].signal_token
            and _identity_matches(record["tool"], snapshot)
            for snapshot in group
        )
        or not signaller.signal_owned(final_ownership[0], group, signal.SIGKILL)
    ):
        _quarantine_record(path, process_root)
        return False
    if not _wait_for_exit(record, inspector, kill_wait, poll_interval):
        _quarantine_record(path, process_root)
        return False
    _remove_record(path, process_root)
    return True


def reconcile_process_records(
    process_root: Path,
    *,
    inspector: ProcessInspector | None = None,
    signaller: ProcessSignaller | None = None,
    terminate_grace: float = 2.0,
    kill_wait: float = 2.0,
    poll_interval: float = 0.05,
) -> ReconcileReport:
    if terminate_grace <= 0 or kill_wait <= 0 or poll_interval <= 0:
        raise ValueError("process reconciliation timing values must be positive")
    if (
        not process_root.is_absolute()
        or process_root.is_symlink()
        or _has_symlink_component(process_root)
    ):
        raise ValueError("process record root is unsafe")
    if not process_root.exists():
        return ReconcileReport("healthy", "PROCESS_RECORDS_CONVERGED", 0, 0)
    resolved_root = process_root.resolve(strict=True)
    selected_inspector = inspector or SystemProcessInspector()
    selected_signaller = signaller or SystemProcessSignaller()
    cleaned = 0
    unverified = 0
    records: list[Path] = []
    for run_root in sorted(resolved_root.iterdir()):
        if run_root.name == "quarantine":
            if run_root.is_symlink() or not run_root.is_dir():
                raise ValueError("process quarantine is unsafe")
            continue
        if run_root.is_symlink() or not run_root.is_dir():
            _quarantine_record(run_root, resolved_root)
            unverified += 1
            continue
        entries = sorted(run_root.iterdir())
        if not entries:
            _quarantine_record(run_root, resolved_root)
            unverified += 1
            continue
        for path in entries:
            if path.suffix != ".json" or path.is_dir():
                _quarantine_record(path, resolved_root)
                unverified += 1
            else:
                records.append(path)
    for path in records:
        if _reconcile_record(
            path,
            resolved_root,
            selected_inspector,
            selected_signaller,
            terminate_grace=terminate_grace,
            kill_wait=kill_wait,
            poll_interval=poll_interval,
        ):
            cleaned += 1
        else:
            unverified += 1
    if unverified:
        return ReconcileReport(
            "unsafe",
            "PROCESS_OWNERSHIP_UNVERIFIED",
            cleaned,
            unverified,
        )
    return ReconcileReport("healthy", "PROCESS_RECORDS_CONVERGED", cleaned, 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile owned media tool processes")
    parser.add_argument("--process-root", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        report = reconcile_process_records(arguments.process_root)
    except Exception:
        report = ReconcileReport("unsafe", "PROCESS_OWNERSHIP_UNVERIFIED", 0, 1)
    payload = {
        "schema_version": 1,
        "status": report.status,
        "code": report.code,
        "cleaned": report.cleaned,
        "unverified": report.unverified,
    }
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"[{report.status.upper()}] {report.code}")
    return 0 if report.status == "healthy" else 2


if __name__ == "__main__":
    raise SystemExit(main())
