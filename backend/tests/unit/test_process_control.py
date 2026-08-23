import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from lvt.core.processes import (
    CancellationToken,
    ProcessCancelledError,
    ProcessExecutionError,
    ProcessTimeoutError,
    SubprocessExecutor,
)


def _python(script: str, *args: str) -> list[str]:
    return [sys.executable, "-c", script, *args]


def _wait_until(predicate: object, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_process_executor_returns_normal_output_and_reaps_process() -> None:
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)

    result = executor.run(
        _python("import sys; print('out'); print('err', file=sys.stderr)"),
        timeout=2,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert not _process_exists(result.pid)


def test_process_executor_raises_for_nonzero_exit() -> None:
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)

    with pytest.raises(ProcessExecutionError) as raised:
        executor.run(
            _python("import sys; print('failure', file=sys.stderr); raise SystemExit(7)"),
            timeout=2,
        )

    assert raised.value.returncode == 7
    assert "failure" in raised.value.stderr
    assert not _process_exists(raised.value.pid)


def test_timeout_sends_term_and_reaps_process(tmp_path: Path) -> None:
    marker = tmp_path / "term.txt"
    script = """
import signal, sys, time
marker = sys.argv[1]
def stop(_signum, _frame):
    open(marker, "w").write("term")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(0.01)
"""
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.5)

    with pytest.raises(ProcessTimeoutError) as raised:
        executor.run(_python(script, str(marker)), timeout=0.1)

    assert raised.value.termination_signal is signal.SIGTERM
    assert marker.read_text(encoding="utf-8") == "term"
    assert not _process_exists(raised.value.pid)


def test_ignored_term_is_killed_and_reaped() -> None:
    script = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.01)
"""
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.05)

    with pytest.raises(ProcessTimeoutError) as raised:
        executor.run(_python(script), timeout=0.1)

    assert raised.value.termination_signal is signal.SIGKILL
    assert not _process_exists(raised.value.pid)


def test_cancellation_terminates_process_group_and_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids.txt"
    script = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(f"{__import__('os').getpid()} {child.pid}")
while True:
    time.sleep(0.01)
"""
    token = CancellationToken()
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)
    captured: list[ProcessCancelledError] = []

    def run() -> None:
        try:
            executor.run(_python(script, str(pid_file)), timeout=10, cancellation=token)
        except ProcessCancelledError as exc:
            captured.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    _wait_until(pid_file.exists)
    parent_pid, child_pid = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]
    token.cancel()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(captured) == 1
    _wait_until(lambda: not _process_exists(parent_pid))
    _wait_until(lambda: not _process_exists(child_pid))


def test_timeout_cleans_child_after_process_group_leader_exits(tmp_path: Path) -> None:
    pid_file = tmp_path / "orphan-child.txt"
    script = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
"""
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)

    with pytest.raises(ProcessTimeoutError):
        executor.run(_python(script, str(pid_file)), timeout=0.1)

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    _wait_until(lambda: not _process_exists(child_pid))


def test_large_stdout_and_stderr_do_not_deadlock() -> None:
    size = 2 * 1024 * 1024
    script = """
import os
size = int(__import__('sys').argv[1])
os.write(1, b'x' * size)
os.write(2, b'y' * size)
"""
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)

    result = executor.run(_python(script, str(size)), timeout=5)

    assert len(result.stdout) == size
    assert len(result.stderr) == size
