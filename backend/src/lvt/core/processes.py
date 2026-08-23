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
    ) -> None:
        super().__init__(message)
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.termination_signal = termination_signal
        self.returncode = returncode


class ProcessExecutionError(ProcessControlError):
    pass


class ProcessTimeoutError(ProcessControlError):
    pass


class ProcessCancelledError(ProcessControlError):
    pass


class SubprocessExecutor:
    def __init__(
        self,
        *,
        poll_interval: float = 0.1,
        terminate_grace: float = 2.0,
    ) -> None:
        if poll_interval <= 0 or terminate_grace <= 0:
            raise ValueError("process timing values must be positive")
        self.poll_interval = poll_interval
        self.terminate_grace = terminate_grace

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
        deadline = time.monotonic() + timeout
        while True:
            if cancellation is not None and cancellation.cancelled:
                stdout, stderr, stopped_by = self._terminate_process_group(process)
                raise ProcessCancelledError(
                    "process cancelled",
                    pid=process.pid,
                    stdout=self._decode(stdout),
                    stderr=self._decode(stderr),
                    termination_signal=stopped_by,
                    returncode=process.returncode,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr, stopped_by = self._terminate_process_group(process)
                raise ProcessTimeoutError(
                    "process timed out",
                    pid=process.pid,
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

        result = ProcessResult(
            command=normalized,
            pid=process.pid,
            returncode=process.returncode,
            stdout=self._decode(stdout),
            stderr=self._decode(stderr),
        )
        if result.returncode != 0:
            raise ProcessExecutionError(
                f"process exited with status {result.returncode}",
                pid=result.pid,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return result

    def _terminate_process_group(
        self, process: subprocess.Popen[bytes]
    ) -> tuple[bytes, bytes, signal.Signals | None]:
        stopped_by = signal.SIGTERM
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=self.terminate_grace)
        except subprocess.TimeoutExpired:
            stopped_by = signal.SIGKILL
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return stdout, stderr, stopped_by

    @staticmethod
    def _decode(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")
