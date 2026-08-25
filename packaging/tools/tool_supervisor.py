#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SAFE_KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
shutdown_requested = False


class SupervisorError(RuntimeError):
    pass


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


def _prepare_process_root(path: Path) -> Path:
    if not path.is_absolute() or _has_symlink_component(path) or path.is_symlink():
        raise SupervisorError("process record root is unsafe")
    parent = path.parent.resolve(strict=True)
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise SupervisorError("process record root is unsafe")
    resolved = path.resolve(strict=True)
    resolved.relative_to(parent)
    path.chmod(0o700)
    if created:
        _fsync_directory(parent)
    return resolved


def _safe_record_path(process_root: Path, run_id: str, kind: str) -> Path:
    try:
        if str(uuid.UUID(run_id)) != run_id or SAFE_KIND.fullmatch(kind) is None:
            raise ValueError
    except ValueError as exc:
        raise SupervisorError("process ownership identifiers are invalid") from exc
    run_root = process_root / run_id
    created = False
    try:
        run_root.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if run_root.is_symlink() or not run_root.is_dir():
        raise SupervisorError("process run record root is unsafe")
    run_root.resolve(strict=True).relative_to(process_root)
    run_root.chmod(0o700)
    if created:
        _fsync_directory(process_root)
    record = run_root / f"{kind}.json"
    if record.exists() or record.is_symlink():
        raise SupervisorError("active process ownership record already exists")
    return record


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _executable_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode):
        raise SupervisorError("process executable is not a regular file")
    digest = _sha256(resolved)
    after = resolved.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SupervisorError("process executable changed during verification")
    return {
        "realpath": str(resolved),
        "device": after.st_dev,
        "inode": after.st_ino,
        "sha256": digest,
    }


def _resolve_executable(command: list[str]) -> Path:
    candidate = Path(command[0])
    if candidate.is_absolute():
        return candidate
    resolved = shutil.which(command[0])
    if resolved is None:
        raise SupervisorError("tool executable is unavailable")
    return Path(resolved)


def _process_start_time(pid: int) -> str:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        value = " ".join(completed.stdout.split())
        if completed.returncode == 0 and value:
            return value
        time.sleep(0.01)
    raise SupervisorError("process start time is unavailable")


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _complete_record(path: Path, payload: dict[str, Any], state: str) -> None:
    completed = dict(payload)
    completed["lifecycle_state"] = state
    _write_record(path, completed)
    path.unlink()
    _fsync_directory(path.parent)
    try:
        path.parent.rmdir()
    except OSError:
        return
    _fsync_directory(path.parent.parent)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_members(pgid: int) -> set[int] | None:
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,state="],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    members: set[int] = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid_value, pgid_value = (int(field) for field in fields[:2])
        except ValueError:
            continue
        if pgid_value == pgid and not fields[2].startswith("Z"):
            members.add(pid_value)
    return members


def _live_group_exists(pgid: int) -> bool:
    members = _group_members(pgid)
    return _group_exists(pgid) if members is None else bool(members)


def _signal_group(pgid: int, requested: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pgid, requested)


def _poll_child(pid: int) -> int | None:
    try:
        found, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0
    if found == 0:
        return None
    return os.waitstatus_to_exitcode(status)


def _wait_child(pid: int, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _poll_child(pid)
        if status is not None:
            return status
        time.sleep(0.01)
    return _poll_child(pid)


def _cleanup_tool_group(
    pid: int,
    pgid: int,
    guard_pid: int,
    guard_write: int,
    status: int | None,
    *,
    control_fd: int,
    ready_fd: int,
    gate_write: int,
    terminate_grace: float,
    kill_wait: float,
) -> tuple[int, str]:
    guard_pid, guard_write = _replace_dead_guard(
        pgid,
        guard_pid,
        guard_write,
        control_fd=control_fd,
        ready_fd=ready_fd,
        gate_write=gate_write,
    )
    while status is not None and guard_pid <= 0 and _live_group_exists(pgid):
        guard_pid, guard_write = _replace_guard(
            pgid,
            control_fd=control_fd,
            ready_fd=ready_fd,
            gate_write=gate_write,
        )
        if guard_pid <= 0:
            time.sleep(0.01)
    if _live_group_exists(pgid):
        _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + terminate_grace
        while time.monotonic() < deadline:
            members = _group_members(pgid)
            if members is not None and not members.difference({guard_pid}):
                break
            time.sleep(0.01)
    while _live_group_exists(pgid):
        guard_pid, guard_write = _replace_dead_guard(
            pgid,
            guard_pid,
            guard_write,
            control_fd=control_fd,
            ready_fd=ready_fd,
            gate_write=gate_write,
        )
        while status is not None and guard_pid <= 0 and _live_group_exists(pgid):
            guard_pid, guard_write = _replace_guard(
                pgid,
                control_fd=control_fd,
                ready_fd=ready_fd,
                gate_write=gate_write,
            )
            if guard_pid <= 0:
                time.sleep(0.01)
        if (status is None or guard_pid > 0) and _live_group_exists(pgid):
            _signal_group(pgid, signal.SIGKILL)
            deadline = time.monotonic() + kill_wait
            while _live_group_exists(pgid) and time.monotonic() < deadline:
                time.sleep(0.01)
    if guard_write >= 0:
        with suppress(OSError):
            os.close(guard_write)
    if guard_pid > 0:
        _wait_child(guard_pid, kill_wait)
    if status is None:
        status = _wait_child(pid, kill_wait)
    if status is None:
        return 70, "cleanup_failed"
    return status, "completed"


def _handle_shutdown(_signum: int, _frame: object) -> None:
    global shutdown_requested
    shutdown_requested = True


def _fork_tool(command: list[str], control_fd: int, ready_fd: int) -> tuple[int, int, int]:
    gate_read, gate_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(gate_write)
            os.close(control_fd)
            os.close(ready_fd)
            os.setpgid(0, 0)
            launch_token = os.read(gate_read, 1)
            os.close(gate_read)
            if launch_token != b"G":
                os._exit(125)
            os.execvpe(command[0], command, os.environ.copy())
        except BaseException:
            os._exit(127)
    os.close(gate_read)
    with suppress(ProcessLookupError):
        os.setpgid(pid, pid)
    return pid, pid, gate_write


def _fork_group_guard(
    pgid: int,
    *,
    control_fd: int,
    ready_fd: int,
    gate_write: int,
) -> tuple[int, int]:
    close_fds: list[int] = []
    for descriptor in (control_fd, ready_fd, gate_write):
        if descriptor < 0:
            continue
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        close_fds.append(descriptor)
    guard_read, guard_write = os.pipe()
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(guard_write)
            os.close(ready_read)
            for descriptor in close_fds:
                with suppress(OSError):
                    os.close(descriptor)
            os.setpgid(0, pgid)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            os.write(ready_write, b"R")
            os.close(ready_write)
            while os.read(guard_read, 1):
                pass
            os.close(guard_read)
            os.killpg(pgid, signal.SIGKILL)
        except BaseException:
            pass
        os._exit(0)
    os.close(guard_read)
    os.close(ready_write)
    try:
        os.setpgid(pid, pgid)
        if os.read(ready_read, 1) != b"R":
            raise SupervisorError("process group guard failed to initialize")
    except (OSError, SupervisorError):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.close(guard_write)
        _wait_child(pid, 1)
        raise
    finally:
        os.close(ready_read)
    return pid, guard_write


def _replace_guard(
    pgid: int,
    *,
    control_fd: int,
    ready_fd: int,
    gate_write: int,
) -> tuple[int, int]:
    if not _group_exists(pgid):
        return -1, -1
    try:
        return _fork_group_guard(
            pgid,
            control_fd=control_fd,
            ready_fd=ready_fd,
            gate_write=gate_write,
        )
    except OSError:
        return -1, -1


def _replace_dead_guard(
    pgid: int,
    guard_pid: int,
    guard_write: int,
    *,
    control_fd: int,
    ready_fd: int,
    gate_write: int,
) -> tuple[int, int]:
    if guard_pid > 0 and _poll_child(guard_pid) is None:
        return guard_pid, guard_write
    if guard_write >= 0:
        with suppress(OSError):
            os.close(guard_write)
    return _replace_guard(
        pgid,
        control_fd=control_fd,
        ready_fd=ready_fd,
        gate_write=gate_write,
    )


def supervise(arguments: argparse.Namespace) -> int:
    process_root = _prepare_process_root(arguments.process_root)
    record_path = _safe_record_path(process_root, arguments.run_id, arguments.kind)
    try:
        if str(uuid.UUID(arguments.job_id)) != arguments.job_id:
            raise ValueError
        if re.fullmatch(r"[0-9a-f]{32}", arguments.ownership_nonce) is None:
            raise ValueError
    except ValueError as exc:
        raise SupervisorError("process ownership metadata is invalid") from exc
    if not arguments.command:
        raise SupervisorError("tool command is empty")

    tool_executable = _resolve_executable(arguments.command)
    tool_identity = _executable_identity(tool_executable)
    supervisor_identity = _executable_identity(Path(sys.executable))
    child_pid = child_pgid = gate_write = -1
    guard_pid = guard_write = -1
    record: dict[str, Any] | None = None
    try:
        child_pid, child_pgid, gate_write = _fork_tool(
            arguments.command,
            arguments.control_fd,
            arguments.ready_fd,
        )
        guard_pid, guard_write = _fork_group_guard(
            child_pgid,
            control_fd=arguments.control_fd,
            ready_fd=arguments.ready_fd,
            gate_write=gate_write,
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "job_id": arguments.job_id,
            "run_id": arguments.run_id,
            "kind": arguments.kind,
            "ownership_nonce": arguments.ownership_nonce,
            "created_at": datetime.now(UTC).isoformat(),
            "lifecycle_state": "running",
            "supervisor": {
                "pid": os.getpid(),
                "pgid": os.getpgrp(),
                "start_time": _process_start_time(os.getpid()),
                "executable": supervisor_identity,
            },
            "tool": {
                "pid": child_pid,
                "pgid": child_pgid,
                "start_time": _process_start_time(child_pid),
                "executable": tool_identity,
            },
        }
        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
        _write_record(record_path, record)
        readable, _, _ = select.select([arguments.control_fd], [], [], 0)
        if shutdown_requested or (readable and not os.read(arguments.control_fd, 1)):
            raise SupervisorError("backend exited before tool launch")
        os.write(gate_write, b"G")
        os.close(gate_write)
        gate_write = -1
        os.write(
            arguments.ready_fd,
            (json.dumps({"pid": child_pid, "pgid": child_pgid}) + "\n").encode("ascii"),
        )
        os.close(arguments.ready_fd)

        child_status: int | None = None
        backend_gone = False
        while child_status is None and not shutdown_requested and not backend_gone:
            if _poll_child(guard_pid) is not None:
                with suppress(OSError):
                    os.close(guard_write)
                guard_pid = guard_write = -1
                break
            child_status = _poll_child(child_pid)
            if child_status is not None:
                break
            readable, _, _ = select.select([arguments.control_fd], [], [], 0.05)
            if readable and not os.read(arguments.control_fd, 1):
                backend_gone = True

        returncode, state = _cleanup_tool_group(
            child_pid,
            child_pgid,
            guard_pid,
            guard_write,
            child_status,
            control_fd=arguments.control_fd,
            ready_fd=arguments.ready_fd,
            gate_write=gate_write,
            terminate_grace=arguments.terminate_grace,
            kill_wait=arguments.kill_wait,
        )
        if state == "completed":
            _complete_record(record_path, record, state)
        else:
            failed = dict(record)
            failed["lifecycle_state"] = state
            _write_record(record_path, failed)
        return returncode
    except BaseException:
        if gate_write >= 0:
            with suppress(OSError):
                os.close(gate_write)
            gate_write = -1
        state = "cleanup_failed"
        if child_pid > 0:
            _, state = _cleanup_tool_group(
                child_pid,
                child_pgid,
                guard_pid,
                guard_write,
                _poll_child(child_pid),
                control_fd=arguments.control_fd,
                ready_fd=arguments.ready_fd,
                gate_write=gate_write,
                terminate_grace=arguments.terminate_grace,
                kill_wait=arguments.kill_wait,
            )
            guard_write = -1
        if record is not None and record_path.exists():
            if state == "completed":
                _complete_record(record_path, record, state)
            else:
                failed = dict(record)
                failed["lifecycle_state"] = state
                _write_record(record_path, failed)
        raise
    finally:
        for descriptor in (arguments.control_fd, arguments.ready_fd):
            with suppress(OSError):
                os.close(descriptor)


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervise one Local Video Transcriber tool")
    parser.add_argument("--process-root", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--ownership-nonce", required=True)
    parser.add_argument("--control-fd", required=True, type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--terminate-grace", required=True, type=float)
    parser.add_argument("--kill-wait", required=True, type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    if arguments.terminate_grace <= 0 or arguments.kill_wait <= 0:
        parser.error("process timing values must be positive")
    return arguments


def main(argv: list[str] | None = None) -> int:
    try:
        return supervise(_parse_arguments(argv))
    except Exception:
        print("TOOL_SUPERVISOR_FAILED", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
