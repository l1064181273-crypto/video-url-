from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ProcessCancelledError(
                "operation cancelled before process start",
                pid=-1,
                stdout="",
                stderr="",
                termination_signal=None,
            )


@dataclass(frozen=True)
class ProcessResult:
    command: tuple[str, ...]
    pid: int
    returncode: int
    stdout: str
    stderr: str
    pgid: int = -1
    group_cleanup_signal: signal.Signals | None = None


@dataclass(frozen=True)
class ProcessOwnership:
    job_id: str
    run_id: str
    kind: str

    def __post_init__(self) -> None:
        for value, name in ((self.job_id, "job_id"), (self.run_id, "run_id")):
            try:
                parsed = uuid.UUID(value)
            except (AttributeError, ValueError) as exc:
                raise ValueError(f"{name} must be a UUID") from exc
            if str(parsed) != value:
                raise ValueError(f"{name} must be a canonical UUID")
        if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.kind) is None:
            raise ValueError("kind must be a safe lowercase component")


class OwnershipSupervisor(Protocol):
    def wrap(
        self,
        command: tuple[str, ...],
        ownership: ProcessOwnership,
        *,
        control_fd: int,
        ready_fd: int,
        terminate_grace: float,
        kill_wait: float,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class ToolSupervisorLauncher:
    supervisor_path: Path
    process_root: Path
    python_executable: Path = Path(sys.executable)

    def wrap(
        self,
        command: tuple[str, ...],
        ownership: ProcessOwnership,
        *,
        control_fd: int,
        ready_fd: int,
        terminate_grace: float,
        kill_wait: float,
    ) -> tuple[str, ...]:
        if (
            not self.supervisor_path.is_absolute()
            or self.supervisor_path.is_symlink()
            or not self.supervisor_path.is_file()
            or not self.process_root.is_absolute()
        ):
            raise ValueError("tool supervisor configuration is unsafe")
        return (
            os.fspath(self.python_executable),
            os.fspath(self.supervisor_path),
            "--process-root",
            os.fspath(self.process_root),
            "--job-id",
            ownership.job_id,
            "--run-id",
            ownership.run_id,
            "--kind",
            ownership.kind,
            "--ownership-nonce",
            uuid.uuid4().hex,
            "--control-fd",
            str(control_fd),
            "--ready-fd",
            str(ready_fd),
            "--terminate-grace",
            str(terminate_grace),
            "--kill-wait",
            str(kill_wait),
            "--",
            *command,
        )


class ProcessControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        pid: int,
        stdout: str,
        stderr: str,
        termination_signal: signal.Signals | None = None,
        returncode: int | None = None,
        pgid: int | None = None,
    ) -> None:
        super().__init__(message)
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.termination_signal = termination_signal
        self.returncode = returncode
        self.pgid = pgid if pgid is not None else pid


class ProcessExecutionError(ProcessControlError):
    pass


class ProcessTimeoutError(ProcessControlError):
    pass


class ProcessCancelledError(ProcessControlError):
    pass


class ProcessGroupCleanupError(ProcessControlError):
    pass


class SubprocessExecutor:
    def __init__(
        self,
        *,
        poll_interval: float = 0.1,
        terminate_grace: float = 2.0,
        kill_wait: float = 2.0,
        supervisor: OwnershipSupervisor | None = None,
    ) -> None:
        if poll_interval <= 0 or terminate_grace <= 0 or kill_wait <= 0:
            raise ValueError("process timing values must be positive")
        self.poll_interval = poll_interval
        self.terminate_grace = terminate_grace
        self.kill_wait = kill_wait
        self.supervisor = supervisor

    def run(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout: float,
        cancellation: CancellationToken | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        ownership: ProcessOwnership | None = None,
    ) -> ProcessResult:
        if not command:
            raise ValueError("command cannot be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if ownership is not None and self.supervisor is None:
            raise ValueError("ownership requires a configured supervisor")
        normalized = tuple(os.fspath(argument) for argument in command)
        control_read = control_write = ready_read = ready_write = -1
        launch_command = normalized
        pass_fds: tuple[int, ...] = ()
        if ownership is not None:
            assert self.supervisor is not None
            try:
                control_read, control_write = os.pipe()
                ready_read, ready_write = os.pipe()
                launch_command = self.supervisor.wrap(
                    normalized,
                    ownership,
                    control_fd=control_read,
                    ready_fd=ready_write,
                    terminate_grace=self.terminate_grace,
                    kill_wait=self.kill_wait,
                )
            except BaseException:
                for descriptor in (control_read, control_write, ready_read, ready_write):
                    if descriptor >= 0:
                        os.close(descriptor)
                raise
            pass_fds = (control_read, ready_write)
        try:
            try:
                process = subprocess.Popen(
                    launch_command,
                    cwd=os.fspath(cwd) if cwd is not None else None,
                    env=dict(env) if env is not None else None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=pass_fds,
                )
            finally:
                if control_read >= 0:
                    os.close(control_read)
                if ready_write >= 0:
                    os.close(ready_write)
        except OSError as exc:
            for descriptor in (control_write, ready_read):
                if descriptor >= 0:
                    os.close(descriptor)
            raise ProcessExecutionError(
                f"process could not start: {exc}",
                pid=-1,
                stdout="",
                stderr=str(exc),
                returncode=None,
            ) from exc
        try:
            launch_pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            launch_pgid = process.pid
        reported_pid = process.pid
        reported_pgid = launch_pgid
        if ownership is not None:
            try:
                reported_pid, reported_pgid = self._read_supervisor_ready(process, ready_read)
            except BaseException:
                with suppress(ProcessControlError):
                    self._cleanup_process_group(process, launch_pgid, supervised=True)
                if control_write >= 0:
                    os.close(control_write)
                    control_write = -1
                raise
            finally:
                os.close(ready_read)
                ready_read = -1
        deadline = time.monotonic() + timeout
        cleanup_complete = False
        try:
            while True:
                if cancellation is not None and cancellation.cancelled:
                    stdout, stderr, stopped_by = self._cleanup_process_group(
                        process,
                        launch_pgid,
                        supervised=ownership is not None,
                    )
                    cleanup_complete = True
                    raise ProcessCancelledError(
                        "process cancelled",
                        pid=reported_pid,
                        pgid=reported_pgid,
                        stdout=self._decode(stdout),
                        stderr=self._decode(stderr),
                        termination_signal=stopped_by,
                        returncode=process.returncode,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr, stopped_by = self._cleanup_process_group(
                        process,
                        launch_pgid,
                        supervised=ownership is not None,
                    )
                    cleanup_complete = True
                    raise ProcessTimeoutError(
                        "process timed out",
                        pid=reported_pid,
                        pgid=reported_pgid,
                        stdout=self._decode(stdout),
                        stderr=self._decode(stderr),
                        termination_signal=stopped_by,
                        returncode=process.returncode,
                    )
                try:
                    stdout, stderr = process.communicate(timeout=min(self.poll_interval, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue

            stdout, stderr, stopped_by = self._cleanup_process_group(
                process,
                launch_pgid,
                captured=(stdout, stderr),
                supervised=ownership is not None,
            )
            cleanup_complete = True
            result = ProcessResult(
                command=normalized,
                pid=reported_pid,
                returncode=process.returncode,
                stdout=self._decode(stdout),
                stderr=self._decode(stderr),
                pgid=reported_pgid,
                group_cleanup_signal=stopped_by,
            )
            if result.returncode != 0:
                raise ProcessExecutionError(
                    f"process exited with status {result.returncode}",
                    pid=result.pid,
                    pgid=reported_pgid,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    termination_signal=stopped_by,
                    returncode=result.returncode,
                )
            return result
        except BaseException:
            if not cleanup_complete:
                with suppress(ProcessControlError):
                    self._cleanup_process_group(
                        process,
                        launch_pgid,
                        supervised=ownership is not None,
                    )
            raise
        finally:
            for descriptor in (control_write, ready_read):
                if descriptor >= 0:
                    os.close(descriptor)

    def _read_supervisor_ready(
        self,
        process: subprocess.Popen[bytes],
        ready_fd: int,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + max(5.0, self.poll_interval)
        payload = bytearray()
        while time.monotonic() < deadline and len(payload) <= 4096:
            readable, _, _ = select.select(
                [ready_fd],
                [],
                [],
                min(self.poll_interval, max(0, deadline - time.monotonic())),
            )
            if not readable:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(ready_fd, 4096 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
            if b"\n" in chunk:
                break
        try:
            ready = json.loads(bytes(payload).decode("utf-8"))
            pid = ready["pid"]
            pgid = ready["pgid"]
            if (
                not isinstance(ready, dict)
                or set(ready) != {"pid", "pgid"}
                or type(pid) is not int
                or type(pgid) is not int
                or pid <= 0
                or pgid <= 0
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProcessExecutionError(
                "tool supervisor did not publish a valid ownership record",
                pid=process.pid,
                pgid=process.pid,
                stdout="",
                stderr="",
                returncode=process.poll(),
            ) from exc
        return pid, pgid

    def _cleanup_process_group(
        self,
        process: subprocess.Popen[bytes],
        pgid: int,
        *,
        captured: tuple[bytes, bytes] | None = None,
        supervised: bool = False,
    ) -> tuple[bytes, bytes, signal.Signals | None]:
        if not self._group_exists(pgid):
            if captured is not None:
                return captured[0], captured[1], None
            stdout, stderr = process.communicate(timeout=self.kill_wait)
            return stdout, stderr, None

        stopped_by = signal.SIGTERM
        self._signal_group(pgid, signal.SIGTERM)
        term_wait = (
            self.terminate_grace + self.kill_wait + max(0.5, self.poll_interval)
            if supervised
            else self.terminate_grace
        )
        term_deadline = time.monotonic() + term_wait
        if captured is None:
            try:
                captured = process.communicate(timeout=term_wait)
            except subprocess.TimeoutExpired:
                captured = None
        self._wait_for_group_exit(pgid, term_deadline)

        if self._group_exists(pgid):
            stopped_by = signal.SIGKILL
            self._signal_group(pgid, signal.SIGKILL)
            kill_deadline = time.monotonic() + self.kill_wait
            if captured is None:
                try:
                    captured = process.communicate(timeout=self.kill_wait)
                except subprocess.TimeoutExpired:
                    captured = None
            self._wait_for_group_exit(pgid, kill_deadline)

        if captured is None:
            captured = process.communicate(timeout=self.kill_wait)
        if self._group_exists(pgid):
            raise ProcessGroupCleanupError(
                "process group still has members after SIGKILL",
                pid=process.pid,
                pgid=pgid,
                stdout=self._decode(captured[0]),
                stderr=self._decode(captured[1]),
                termination_signal=stopped_by,
                returncode=process.returncode,
            )
        return captured[0], captured[1], stopped_by

    def _wait_for_group_exit(self, pgid: int, deadline: float) -> None:
        while self._group_exists(pgid) and time.monotonic() < deadline:
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _signal_group(pgid: int, requested_signal: signal.Signals) -> None:
        with suppress(ProcessLookupError):
            os.killpg(pgid, requested_signal)

    @staticmethod
    def _decode(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")
