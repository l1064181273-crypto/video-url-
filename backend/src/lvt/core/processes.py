from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


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
    ) -> None:
        if poll_interval <= 0 or terminate_grace <= 0 or kill_wait <= 0:
            raise ValueError("process timing values must be positive")
        self.poll_interval = poll_interval
        self.terminate_grace = terminate_grace
        self.kill_wait = kill_wait

    def run(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout: float,
        cancellation: CancellationToken | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        if not command:
            raise ValueError("command cannot be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        normalized = tuple(os.fspath(argument) for argument in command)
        try:
            process = subprocess.Popen(
                normalized,
                cwd=os.fspath(cwd) if cwd is not None else None,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessExecutionError(
                f"process could not start: {exc}",
                pid=-1,
                stdout="",
                stderr=str(exc),
                returncode=None,
            ) from exc
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            pgid = process.pid
        deadline = time.monotonic() + timeout
        cleanup_complete = False
        try:
            while True:
                if cancellation is not None and cancellation.cancelled:
                    stdout, stderr, stopped_by = self._cleanup_process_group(process, pgid)
                    cleanup_complete = True
                    raise ProcessCancelledError(
                        "process cancelled",
                        pid=process.pid,
                        pgid=pgid,
                        stdout=self._decode(stdout),
                        stderr=self._decode(stderr),
                        termination_signal=stopped_by,
                        returncode=process.returncode,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr, stopped_by = self._cleanup_process_group(process, pgid)
                    cleanup_complete = True
                    raise ProcessTimeoutError(
                        "process timed out",
                        pid=process.pid,
                        pgid=pgid,
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
                pgid,
                captured=(stdout, stderr),
            )
            cleanup_complete = True
            result = ProcessResult(
                command=normalized,
                pid=process.pid,
                returncode=process.returncode,
                stdout=self._decode(stdout),
                stderr=self._decode(stderr),
                pgid=pgid,
                group_cleanup_signal=stopped_by,
            )
            if result.returncode != 0:
                raise ProcessExecutionError(
                    f"process exited with status {result.returncode}",
                    pid=result.pid,
                    pgid=pgid,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    termination_signal=stopped_by,
                    returncode=result.returncode,
                )
            return result
        except BaseException:
            if not cleanup_complete:
                with suppress(ProcessControlError):
                    self._cleanup_process_group(process, pgid)
            raise

    def _cleanup_process_group(
        self,
        process: subprocess.Popen[bytes],
        pgid: int,
        *,
        captured: tuple[bytes, bytes] | None = None,
    ) -> tuple[bytes, bytes, signal.Signals | None]:
        if not self._group_exists(pgid):
            if captured is not None:
                return captured[0], captured[1], None
            stdout, stderr = process.communicate(timeout=self.kill_wait)
            return stdout, stderr, None

        stopped_by = signal.SIGTERM
        self._signal_group(pgid, signal.SIGTERM)
        term_deadline = time.monotonic() + self.terminate_grace
        if captured is None:
            try:
                captured = process.communicate(timeout=self.terminate_grace)
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
