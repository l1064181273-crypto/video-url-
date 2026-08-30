import os
import signal
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from lvt.core.processes import (
    CancellationToken,
    ProcessCancelledError,
    ProcessExecutionError,
    ProcessOwnership,
    ProcessTimeoutError,
    SubprocessExecutor,
    ToolSupervisorLauncher,
)

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX process-group and signal semantics",
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


def _kill_group(pgid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _saved_pgid(result: Any) -> int:
    return int(getattr(result, "pgid", result.pid))


def _closed_pipe_leader_command(
    tmp_path: Path,
    *,
    exit_code: int,
    ignore_term: bool = False,
    spawn_grandchild: bool = False,
) -> tuple[list[str], Path]:
    pid_file = tmp_path / "descendants.txt"
    child_script = tmp_path / "child.py"
    grandchild_block = (
        """
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pid_text = f"{os.getpid()} {grandchild.pid}"
"""
        if spawn_grandchild
        else "pid_text = str(os.getpid())"
    )
    ignore_block = "signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_term else ""
    child_script.write_text(
        f"""
import os, signal, subprocess, sys, time
{ignore_block}
{grandchild_block}
open(sys.argv[1], "w").write(pid_text)
while True:
    time.sleep(0.01)
""",
        encoding="utf-8",
    )
    leader_script = """
import pathlib, subprocess, sys, time
subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
path = pathlib.Path(sys.argv[2])
while not path.exists():
    time.sleep(0.01)
raise SystemExit(int(sys.argv[3]))
"""
    return (
        _python(
            leader_script,
            str(child_script),
            str(pid_file),
            str(exit_code),
        ),
        pid_file,
    )


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


@POSIX_ONLY
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


@POSIX_ONLY
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


@POSIX_ONLY
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


@POSIX_ONLY
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


@POSIX_ONLY
def test_successful_leader_cleans_closed_pipe_child_before_return(
    tmp_path: Path,
) -> None:
    command, pid_file = _closed_pipe_leader_command(tmp_path, exit_code=0)
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)
    result = None
    try:
        result = executor.run(command, timeout=2)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        assert result.returncode == 0
        assert result.group_cleanup_signal is signal.SIGTERM
        assert not _process_exists(child_pid)
    finally:
        if result is not None:
            _kill_group(_saved_pgid(result))


@POSIX_ONLY
def test_nonzero_leader_cleans_closed_pipe_child_before_error(
    tmp_path: Path,
) -> None:
    command, pid_file = _closed_pipe_leader_command(tmp_path, exit_code=7)
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)
    captured: ProcessExecutionError | None = None
    try:
        with pytest.raises(ProcessExecutionError) as raised:
            executor.run(command, timeout=2)
        captured = raised.value
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        assert captured.returncode == 7
        assert captured.termination_signal is signal.SIGTERM
        assert not _process_exists(child_pid)
    finally:
        if captured is not None:
            _kill_group(_saved_pgid(captured))


@POSIX_ONLY
def test_successful_leader_kills_closed_pipe_child_that_ignores_term(
    tmp_path: Path,
) -> None:
    command, pid_file = _closed_pipe_leader_command(
        tmp_path,
        exit_code=0,
        ignore_term=True,
    )
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.05)
    result = None
    try:
        result = executor.run(command, timeout=2)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        assert result.group_cleanup_signal is signal.SIGKILL
        assert not _process_exists(child_pid)
    finally:
        if result is not None:
            _kill_group(_saved_pgid(result))


@POSIX_ONLY
def test_successful_leader_cleans_closed_pipe_child_and_grandchild(
    tmp_path: Path,
) -> None:
    command, pid_file = _closed_pipe_leader_command(
        tmp_path,
        exit_code=0,
        spawn_grandchild=True,
    )
    executor = SubprocessExecutor(poll_interval=0.01, terminate_grace=0.1)
    result = None
    try:
        result = executor.run(command, timeout=2)
        child_pid, grandchild_pid = [
            int(value) for value in pid_file.read_text(encoding="utf-8").split()
        ]
        assert result.group_cleanup_signal is signal.SIGTERM
        assert not _process_exists(child_pid)
        assert not _process_exists(grandchild_pid)
    finally:
        if result is not None:
            _kill_group(_saved_pgid(result))


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


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Job Objects")
def test_windows_supervisor_timeout_kills_child_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "windows-job-pids.txt"
    script = """
import os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}", encoding="ascii")
while True:
    time.sleep(0.01)
"""
    repository_root = Path(__file__).resolve().parents[3]
    executor = SubprocessExecutor(
        poll_interval=0.01,
        terminate_grace=0.5,
        supervisor=ToolSupervisorLauncher(
            supervisor_path=repository_root / "packaging" / "tools" / "windows_tool_supervisor.py",
            process_root=tmp_path,
        ),
    )

    with pytest.raises(ProcessTimeoutError):
        executor.run(
            _python(script, str(pid_file)),
            timeout=0.25,
            ownership=ProcessOwnership(
                job_id="11111111-1111-4111-8111-111111111111",
                run_id="22222222-2222-4222-8222-222222222222",
                kind="test-tool",
            ),
        )

    parent_pid, child_pid = [int(value) for value in pid_file.read_text("ascii").split()]
    _wait_until(lambda: not _process_exists(parent_pid))
    _wait_until(lambda: not _process_exists(child_pid))
