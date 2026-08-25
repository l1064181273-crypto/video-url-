from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from lvt.core.processes import (
    CancellationToken,
    ProcessCancelledError,
    ProcessOwnership,
    ProcessTimeoutError,
    SubprocessExecutor,
    ToolSupervisorLauncher,
)

ROOT = Path(__file__).resolve().parents[3]
TOOL_SUPERVISOR = ROOT / "packaging/tools/tool_supervisor.py"
RECONCILE_PROCESSES = ROOT / "packaging/tools/reconcile_processes.py"
JOB_ID = "22222222-2222-4222-8222-222222222222"
RUN_ID = "11111111-1111-4111-8111-111111111111"


def _wait_until(predicate: object, *, timeout: float = 5.0) -> None:
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


def _launcher(process_root: Path) -> ToolSupervisorLauncher:
    process_root.parent.mkdir(parents=True, exist_ok=True)
    return ToolSupervisorLauncher(
        supervisor_path=TOOL_SUPERVISOR,
        process_root=process_root,
    )


def test_record_is_durable_before_tool_exec_and_contains_no_command_data(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    record = process_root / RUN_ID / "ffmpeg.json"
    observed = tmp_path / "observed.json"
    script = (
        "import json,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);"
        "payload=json.loads(p.read_text());"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps("
        "{'record_exists':p.exists(),'record':payload}))"
    )
    executor = SubprocessExecutor(
        poll_interval=0.01,
        terminate_grace=0.1,
        supervisor=_launcher(process_root),
    )

    result = executor.run(
        [sys.executable, "-c", script, str(record), str(observed), "SECRET_MEDIA_ARGUMENT"],
        timeout=2,
        ownership=ProcessOwnership(job_id=JOB_ID, run_id=RUN_ID, kind="ffmpeg"),
    )

    assert result.returncode == 0
    observed_payload = json.loads(observed.read_text(encoding="utf-8"))
    assert observed_payload["record_exists"] is True
    assert not record.exists()
    serialized_record = json.dumps(observed_payload["record"])
    assert "SECRET_MEDIA_ARGUMENT" not in serialized_record
    assert set(observed_payload["record"]) == {
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


def test_backend_sigkill_pipe_eof_reaps_tool_tree_and_record(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    process_root.parent.mkdir(parents=True)
    pid_file = tmp_path / "tool-pids.json"
    tool = tmp_path / "tool.py"
    tool.write_text(
        """
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "g=subprocess.Popen([sys.executable,'-c','import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(g.pid));time.sleep(60)",
        sys.argv[2],
    ]
)
open(sys.argv[1], "w").write(json.dumps({"tool": os.getpid(), "child": child.pid}))
while True:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    grandchild_file = tmp_path / "grandchild.pid"
    backend = tmp_path / "backend.py"
    backend.write_text(
        """
import sys
from pathlib import Path
from lvt.core.processes import ProcessOwnership, SubprocessExecutor, ToolSupervisorLauncher
root, supervisor, tool, pids, grandchild = map(Path, sys.argv[1:])
executor = SubprocessExecutor(
    poll_interval=0.01,
    terminate_grace=0.1,
    kill_wait=0.2,
    supervisor=ToolSupervisorLauncher(supervisor_path=supervisor, process_root=root),
)
executor.run(
    [sys.executable, str(tool), str(pids), str(grandchild)],
    timeout=60,
    ownership=ProcessOwnership(
        job_id="22222222-2222-4222-8222-222222222222",
        run_id="11111111-1111-4111-8111-111111111111",
        kind="yt-dlp",
    ),
)
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "backend/src")
    backend_process = subprocess.Popen(
        [
            sys.executable,
            str(backend),
            str(process_root),
            str(TOOL_SUPERVISOR),
            str(tool),
            str(pid_file),
            str(grandchild_file),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    record = process_root / RUN_ID / "yt-dlp.json"
    try:
        _wait_until(record.exists)
        _wait_until(pid_file.exists)
        _wait_until(grandchild_file.exists)
        payload = json.loads(record.read_text(encoding="utf-8"))
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        tracked = [
            payload["supervisor"]["pid"],
            pids["tool"],
            pids["child"],
            int(grandchild_file.read_text(encoding="utf-8")),
        ]

        os.kill(backend_process.pid, signal.SIGKILL)
        backend_process.wait(timeout=2)

        _wait_until(lambda: not record.exists())
        for pid in tracked:
            _wait_until(lambda pid=pid: not _process_exists(pid))
    finally:
        with suppress(ProcessLookupError):
            os.kill(backend_process.pid, signal.SIGKILL)
        for record_path in process_root.rglob("*.json"):
            try:
                payload = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for key in ("tool", "supervisor"):
                pid = payload.get(key, {}).get("pid")
                if isinstance(pid, int):
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)


def test_ready_pipe_failure_reaps_released_tool_and_record(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    process_root.parent.mkdir(parents=True)
    pid_file = tmp_path / "tool.pid"
    control_read, control_write = os.pipe()
    ready_read, ready_write = os.pipe()
    launcher = _launcher(process_root)
    command = launcher.wrap(
        (
            sys.executable,
            "-c",
            "import os,pathlib,sys,time;"
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
            "time.sleep(60)",
            str(pid_file),
        ),
        ProcessOwnership(job_id=JOB_ID, run_id=RUN_ID, kind="ffmpeg"),
        control_fd=control_read,
        ready_fd=ready_write,
        terminate_grace=0.1,
        kill_wait=0.2,
    )
    os.close(ready_read)
    supervisor = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(control_read, ready_write),
    )
    os.close(control_read)
    os.close(ready_write)
    try:
        assert supervisor.wait(timeout=5) == 70
        if pid_file.exists():
            _wait_until(lambda: not _process_exists(int(pid_file.read_text(encoding="utf-8"))))
        assert not list(process_root.rglob("*.json"))
    finally:
        os.close(control_write)
        with suppress(ProcessLookupError):
            os.kill(supervisor.pid, signal.SIGKILL)


def test_tool_leader_exit_still_cleans_group_descendant(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "open(sys.argv[1],'w').write(str(p.pid))"
    )
    executor = SubprocessExecutor(
        poll_interval=0.01,
        terminate_grace=0.1,
        supervisor=_launcher(process_root),
    )

    result = executor.run(
        [sys.executable, "-c", script, str(child_pid_file)],
        timeout=2,
        ownership=ProcessOwnership(job_id=JOB_ID, run_id=RUN_ID, kind="ffmpeg"),
    )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert result.returncode == 0
    _wait_until(lambda: not _process_exists(child_pid))
    assert not list(process_root.rglob("*.json"))


def test_system_reconciler_stops_only_fully_verified_live_supervisor(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    started = tmp_path / "started"
    executor = SubprocessExecutor(
        poll_interval=0.01,
        terminate_grace=0.1,
        supervisor=_launcher(process_root),
    )
    failures: list[BaseException] = []

    def run() -> None:
        try:
            executor.run(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,sys,time;"
                    "pathlib.Path(sys.argv[1]).write_text('started');"
                    "time.sleep(60)",
                    str(started),
                ],
                timeout=60,
                ownership=ProcessOwnership(job_id=JOB_ID, run_id=RUN_ID, kind="ffmpeg"),
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    record = process_root / RUN_ID / "ffmpeg.json"
    _wait_until(started.exists)
    _wait_until(record.exists)

    completed = subprocess.run(
        [
            sys.executable,
            str(RECONCILE_PROCESSES),
            "--process-root",
            str(process_root),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    thread.join(timeout=5)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {
        "schema_version": 1,
        "status": "healthy",
        "code": "PROCESS_RECORDS_CONVERGED",
        "cleaned": 1,
        "unverified": 0,
    }
    assert not thread.is_alive()
    assert failures
    assert not record.exists()


def test_supervised_timeout_preserves_error_and_cleans_record(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    pid_file = tmp_path / "tool.pid"
    executor = SubprocessExecutor(
        poll_interval=0.01,
        terminate_grace=0.05,
        kill_wait=0.2,
        supervisor=_launcher(process_root),
    )

    with pytest.raises(ProcessTimeoutError) as raised:
        executor.run(
            [
                sys.executable,
                "-c",
                "import os,pathlib,signal,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                "time.sleep(60)",
                str(pid_file),
            ],
            timeout=0.1,
            ownership=ProcessOwnership(job_id=JOB_ID, run_id=RUN_ID, kind="ffmpeg"),
        )

    assert raised.value.termination_signal in {signal.SIGTERM, signal.SIGKILL}
    _wait_until(lambda: not _process_exists(int(pid_file.read_text(encoding="utf-8"))))
    assert not list(process_root.rglob("*.json"))


def test_supervised_cancellation_preserves_error_and_cleans_record(tmp_path: Path) -> None:
    process_root = tmp_path / "runtime/processes"
    pid_file = tmp_path / "tool.pid"
    token = CancellationToken()
    executor = SubprocessExecutor(
        poll_interval=0.01,
        terminate_grace=0.1,
        supervisor=_launcher(process_root),
    )
    failures: list[BaseException] = []

    def run() -> None:
        try:
            executor.run(
                [
                    sys.executable,
                    "-c",
                    "import os,pathlib,sys,time;"
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                    "time.sleep(60)",
                    str(pid_file),
                ],
                timeout=60,
                cancellation=token,
                ownership=ProcessOwnership(job_id=JOB_ID, run_id=RUN_ID, kind="yt-dlp"),
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    _wait_until(pid_file.exists)
    token.cancel()
    thread.join(timeout=5)

    assert len(failures) == 1
    assert isinstance(failures[0], ProcessCancelledError)
    assert not thread.is_alive()
    _wait_until(lambda: not _process_exists(int(pid_file.read_text(encoding="utf-8"))))
    assert not list(process_root.rglob("*.json"))
