from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
from contextlib import suppress
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging" / "tools"))

import process_state  # noqa: E402
import verify_install  # noqa: E402
from process_state import (  # noqa: E402
    LifecycleManager,
    LifecycleResult,
    ProcessSnapshot,
    ServiceError,
    SystemServiceOperations,
    ValidationOutcome,
)


class FakeOperations:
    def __init__(
        self,
        *,
        prerequisite: ValidationOutcome | None = None,
        runtime: ValidationOutcome | None = None,
        backend_state: str = "absent",
        ollama_state: str = "absent",
        health: bool = True,
        stop_failures: set[str] | None = None,
    ) -> None:
        self.prerequisite = prerequisite or ValidationOutcome(0, "healthy")
        self.runtime = runtime or ValidationOutcome(0, "healthy")
        self._backend_state = backend_state
        self._ollama_state = ollama_state
        self.health = health
        self.stop_failures = stop_failures or set()
        self.calls: list[str] = []
        self.launch_barrier: threading.Barrier | None = None
        self.backend_launches = 0

    def reconcile(self) -> None:
        self.calls.append("reconcile")

    def validate(self, phase: str) -> ValidationOutcome:
        self.calls.append(f"validate:{phase}")
        return self.prerequisite if phase == "installed-prerequisites" else self.runtime

    def state(self, kind: str) -> str:
        self.calls.append(f"state:{kind}")
        return self._backend_state if kind == "backend" else self._ollama_state

    def launch(self, kind: str, activation_fd: int | None = None) -> None:
        self.calls.append(f"launch:{kind}")
        if kind == "backend":
            self.backend_launches += 1
            self._backend_state = "owned"
            if self.launch_barrier is not None:
                self.launch_barrier.wait(timeout=5)
        else:
            self._ollama_state = "owned"

    def backend_healthy(self) -> bool:
        self.calls.append("health:backend")
        return self.health

    def stop(self, kind: str) -> None:
        self.calls.append(f"stop:{kind}")
        if kind in self.stop_failures:
            raise ServiceError(f"{kind} cleanup failed")
        if kind == "backend":
            self._backend_state = "absent"
        else:
            self._ollama_state = "absent"

    def ownership_records_converged(self) -> bool:
        self.calls.append("ownership:converged")
        return True


def _manager(tmp_path: Path, operations: FakeOperations) -> LifecycleManager:
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.0"
    release.mkdir(parents=True)
    return LifecycleManager(data_root, release, operations=operations)


def test_owned_group_signal_uses_audit_tokens_and_never_killpg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "service"
    executable.write_bytes(b"service")
    anchor = ProcessSnapshot(101, 101, "start", executable, 1, 2, "a" * 64, (1,) * 8)
    child = ProcessSnapshot(102, 101, "start", executable, 1, 2, "a" * 64, (2,) * 8)
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(process_state, "_group_snapshots", lambda _pgid: (anchor, child))
    monkeypatch.setattr(process_state, "_token_is_live", lambda _snapshot: True)
    monkeypatch.setattr(
        process_state,
        "_signal_snapshot",
        lambda snapshot, requested: signals.append((snapshot.pid, requested)) or True,
    )
    monkeypatch.setattr(
        process_state.os,
        "killpg",
        lambda _pgid, requested: (
            None
            if requested == 0
            else pytest.fail("non-zero killpg bypassed audit-token signaling")
        ),
    )

    assert process_state._signal_owned_group(anchor, signal.SIGTERM)
    assert signals == [(102, signal.SIGTERM), (101, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("validation", "backend_state", "expected"),
    [
        (ValidationOutcome(0, "healthy"), "absent", LifecycleResult(0, "ready_to_start")),
        (ValidationOutcome(0, "healthy"), "owned", LifecycleResult(10, "already_running")),
        (
            ValidationOutcome(1, "warning"),
            "absent",
            LifecycleResult(1, "missing_prerequisite"),
        ),
        (
            ValidationOutcome(2, "failed"),
            "absent",
            LifecycleResult(2, "unsafe_or_corrupt"),
        ),
    ],
)
def test_prestart_has_four_strict_exit_status_results(
    tmp_path: Path,
    validation: ValidationOutcome,
    backend_state: str,
    expected: LifecycleResult,
) -> None:
    operations = FakeOperations(prerequisite=validation, backend_state=backend_state)
    manager = _manager(tmp_path, operations)

    assert manager.prestart(lock_held=True) == expected
    assert not any(call.startswith("launch:") for call in operations.calls)


def test_status_exit_mismatch_is_internal_failure_with_zero_launch(tmp_path: Path) -> None:
    operations = FakeOperations(prerequisite=ValidationOutcome(0, "warning"))
    manager = _manager(tmp_path, operations)

    assert manager.prestart(lock_held=True) == LifecycleResult(2, "unsafe_or_corrupt")
    assert not any(call.startswith("launch:") for call in operations.calls)


def test_start_orders_health_before_runtime_full(tmp_path: Path) -> None:
    operations = FakeOperations()
    manager = _manager(tmp_path, operations)

    result = manager.start(lock_held=True)

    assert result == LifecycleResult(0, "started")
    assert operations.calls == [
        "reconcile",
        "validate:installed-prerequisites",
        "state:backend",
        "state:ollama",
        "launch:ollama",
        "launch:backend",
        "health:backend",
        "validate:runtime-full",
    ]


@pytest.mark.parametrize(
    ("health", "runtime"),
    [
        (False, ValidationOutcome(0, "healthy")),
        (True, ValidationOutcome(1, "warning")),
        (True, ValidationOutcome(2, "failed")),
    ],
)
def test_start_failure_cleans_only_services_created_by_this_call(
    tmp_path: Path,
    health: bool,
    runtime: ValidationOutcome,
) -> None:
    operations = FakeOperations(health=health, runtime=runtime)
    manager = _manager(tmp_path, operations)

    with pytest.raises(ServiceError):
        manager.start(lock_held=True)

    assert operations.calls[-2:] == ["stop:backend", "stop:ollama"]


def test_start_failure_attempts_all_created_service_cleanup_before_raising(
    tmp_path: Path,
) -> None:
    operations = FakeOperations(health=False, stop_failures={"backend"})
    manager = _manager(tmp_path, operations)

    with pytest.raises(ExceptionGroup, match="start and cleanup failed") as captured:
        manager.start(lock_held=True)

    assert operations.calls[-2:] == ["stop:backend", "stop:ollama"]
    assert len(captured.value.exceptions) == 2


def test_already_running_only_runs_runtime_full(tmp_path: Path) -> None:
    operations = FakeOperations(backend_state="owned", ollama_state="owned")
    manager = _manager(tmp_path, operations)

    result = manager.start(lock_held=True)

    assert result == LifecycleResult(0, "already_running")
    assert not any(call.startswith("launch:") for call in operations.calls)
    assert operations.calls[-1] == "validate:runtime-full"


def test_stop_converges_backend_tools_records_then_owned_ollama(tmp_path: Path) -> None:
    operations = FakeOperations(backend_state="owned", ollama_state="owned")
    manager = _manager(tmp_path, operations)

    result = manager.stop(lock_held=True)

    assert result == LifecycleResult(0, "stopped")
    assert operations.calls == [
        "reconcile",
        "state:backend",
        "stop:backend",
        "ownership:converged",
        "state:ollama",
        "stop:ollama",
        "ownership:converged",
    ]


def test_unowned_service_state_fails_closed_without_signal(tmp_path: Path) -> None:
    operations = FakeOperations(backend_state="unsafe", ollama_state="owned")
    manager = _manager(tmp_path, operations)

    with pytest.raises(ServiceError):
        manager.stop(lock_held=True)

    assert not any(call.startswith("stop:") for call in operations.calls)


def test_two_concurrent_start_calls_launch_one_backend(tmp_path: Path) -> None:
    operations = FakeOperations()
    manager = _manager(tmp_path, operations)
    entered = threading.Barrier(2)
    release = threading.Barrier(2)
    original_launch = operations.launch

    def launch(kind: str, activation_fd: int | None = None) -> None:
        original_launch(kind, activation_fd)
        if kind == "backend":
            entered.wait(timeout=5)
            release.wait(timeout=5)

    operations.launch = launch  # type: ignore[method-assign]
    outcomes: list[LifecycleResult] = []

    def run() -> None:
        outcomes.append(manager.start())

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    entered.wait(timeout=5)
    second.start()
    release.wait(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert operations.backend_launches == 1
    assert sorted(result.status for result in outcomes) == ["already_running", "started"]


class FixtureSystemOperations(SystemServiceOperations):
    def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
        assert kind == "backend"
        script = (
            "import http.server,json;"
            "H=type('H',(http.server.BaseHTTPRequestHandler,),{"
            "'do_GET':lambda s:(s.send_response(200),s.send_header('Content-Type',"
            "'application/json'),s.end_headers(),s.wfile.write("
            "json.dumps({'status':'healthy'}).encode())),"
            "'log_message':lambda *a:None});"
            "http.server.HTTPServer(('127.0.0.1',8765),H).serve_forever()"
        )
        return [sys.executable, "-c", script], {
            "HOME": os.environ.get("HOME", "/"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }

    def _reconcile_tools(self) -> None:
        return


class IgnoringFixtureSystemOperations(FixtureSystemOperations):
    def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
        command, environment = super()._service_command(kind)
        command[2] = "import signal;" + command[2]
        command[2] = "import signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);" + command[
            2
        ].removeprefix("import signal;")
        return command, environment


class FullRuntimeFixtureOperations(FixtureSystemOperations):
    def reconcile(self) -> None:
        for kind in ("backend", "ollama"):
            self._reconcile_orphan(kind)

    def validate(self, phase: str) -> ValidationOutcome:
        if phase == "installed-prerequisites":
            return ValidationOutcome(0, "healthy")
        probes = verify_install.LocalProbes(self.data_root)
        healthy = probes.ollama_port_state() == "owned" and self.backend_healthy()
        return ValidationOutcome(0, "healthy") if healthy else ValidationOutcome(2, "failed")

    def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
        port = 8765 if kind == "backend" else 11435
        script = (
            "import http.server,json;"
            "H=type('H',(http.server.BaseHTTPRequestHandler,),{"
            "'do_GET':lambda s:(s.send_response(200),s.send_header('Content-Type',"
            "'application/json'),s.end_headers(),s.wfile.write("
            "json.dumps({'status':'healthy'}).encode())),"
            "'log_message':lambda *a:None});"
            f"http.server.HTTPServer(('127.0.0.1',{port}),H).serve_forever()"
        )
        return [sys.executable, "-c", script], {
            "HOME": os.environ.get("HOME", "/"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        }


def _system_operations(tmp_path: Path) -> FixtureSystemOperations:
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.0"
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    for relative in ("runtime", "logs"):
        (data_root / relative).mkdir(parents=True)
    return FixtureSystemOperations(data_root, release, terminate_grace=0.1, kill_wait=1)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_real_supervisor_record_is_generation_bound_and_stop_reaps_service(
    tmp_path: Path,
) -> None:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            pytest.skip("backend test port is occupied")
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"

    operations.launch("backend")
    try:
        assert operations.state("backend") == "owned"
        assert operations.backend_healthy()
        payload = json.loads(record.read_text(encoding="utf-8"))
        assert set(payload) == {
            "schema_version",
            "kind",
            "nonce",
            "port",
            "supervisor",
            "service",
        }
        assert payload["port"] == 8765
        assert record.stat().st_mode & 0o777 == 0o600
    finally:
        operations.stop("backend")

    assert operations.state("backend") == "absent"
    assert not record.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_real_supervisor_escalates_ignoring_service_to_kill(tmp_path: Path) -> None:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            pytest.skip("backend test port is occupied")
    base = _system_operations(tmp_path)
    operations = IgnoringFixtureSystemOperations(
        base.data_root,
        base.release_root,
        terminate_grace=0.1,
        kill_wait=1,
    )
    record = operations.data_root / "runtime/backend.pid"

    operations.launch("backend")
    payload = json.loads(record.read_text(encoding="utf-8"))
    service_pid = payload["service"]["pid"]
    operations.stop("backend")

    with pytest.raises(ProcessLookupError):
        os.kill(service_pid, 0)
    assert not record.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
@pytest.mark.parametrize("tamper", ["start_time", "executable", "pid"])
def test_stale_or_reused_service_record_fails_closed_without_signal(
    tmp_path: Path,
    tamper: str,
) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"
    payload = {
        "schema_version": 1,
        "kind": "backend",
        "nonce": "a" * 32,
        "port": 8765,
        "supervisor": {
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "start_time": "stale",
            "executable": {
                "realpath": "/usr/bin/false",
                "device": 1,
                "inode": 1,
                "sha256": "0" * 64,
            },
        },
        "service": {
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "start_time": "stale",
            "executable": {
                "realpath": "/usr/bin/false",
                "device": 1,
                "inode": 1,
                "sha256": "0" * 64,
            },
        },
    }
    if tamper == "pid":
        payload["supervisor"]["pid"] = 999_999
    elif tamper == "executable":
        payload["supervisor"]["executable"]["sha256"] = "f" * 64
    record.write_text(json.dumps(payload), encoding="utf-8")
    record.chmod(0o600)

    assert operations.state("backend") == "unsafe"
    with pytest.raises(ServiceError, match="unverified"):
        operations.stop("backend")
    assert record.exists()


def test_unowned_backend_port_conflict_is_unsafe_and_11434_is_not_consulted(
    tmp_path: Path,
) -> None:
    operations = _system_operations(tmp_path)
    backend = socket.socket()
    backend.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        backend.bind(("127.0.0.1", 8765))
        backend.listen()
    except OSError:
        backend.close()
        pytest.skip("backend test port is occupied")
    user_ollama = socket.socket()
    user_ollama.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        with suppress(OSError):
            user_ollama.bind(("127.0.0.1", 11434))
            user_ollama.listen()
        assert operations.state("backend") == "unsafe"
    finally:
        backend.close()
        user_ollama.close()


def test_snapshot_payload_persists_audit_generation_and_rejects_reuse(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "service"
    executable.write_bytes(b"service")
    original = ProcessSnapshot(
        321,
        321,
        "same-second",
        executable,
        1,
        2,
        "a" * 64,
        (1, 2, 3, 4, 5, 321, 7, 10),
    )
    reused = ProcessSnapshot(
        321,
        321,
        "same-second",
        executable,
        1,
        2,
        "a" * 64,
        (1, 2, 3, 4, 5, 321, 7, 11),
    )

    payload = process_state._snapshot_payload(original)

    assert payload["audit_token"] == list(original.signal_token)
    assert process_state._snapshot_matches(original, payload)
    assert not process_state._snapshot_matches(reused, payload)


@pytest.mark.parametrize("mutation", ["extra_executable_field", "invalid_audit_word"])
def test_ownership_record_parser_rejects_invalid_nested_snapshot_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    snapshot = process_state._snapshot(os.getpid())
    assert snapshot is not None
    payload = {
        "schema_version": 1,
        "kind": "backend",
        "nonce": "a" * 32,
        "port": 8765,
        "supervisor": process_state._snapshot_payload(snapshot),
        "service": process_state._snapshot_payload(snapshot),
    }
    if mutation == "extra_executable_field":
        payload["service"]["executable"]["unexpected"] = True
    else:
        payload["service"]["audit_token"][0] = 0x1_0000_0000
    record = tmp_path / "backend.pid"
    record.write_text(json.dumps(payload), encoding="utf-8")
    record.chmod(0o600)

    assert process_state._read_record_payload(record, "backend", 8765) is None


def test_stop_child_kills_remaining_descendant_after_leader_exits_on_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "service"
    executable.write_bytes(b"service")
    leader = ProcessSnapshot(501, 501, "start", executable, 1, 2, "a" * 64, (1,) * 8)
    child = ProcessSnapshot(502, 501, "start", executable, 1, 2, "a" * 64, (2,) * 8)
    signals: list[tuple[int, signal.Signals]] = []
    groups = iter(((leader, child), (child,), (child,), ()))

    class ExitedLeader:
        pid = 501

        @staticmethod
        def poll() -> int | None:
            return 0

        @staticmethod
        def wait(timeout: float) -> int:
            return 0

    monkeypatch.setattr(process_state, "_group_snapshots", lambda _pgid: next(groups, ()))
    monkeypatch.setattr(process_state, "_token_is_live", lambda _snapshot: True)
    monkeypatch.setattr(
        process_state,
        "_signal_snapshot",
        lambda snapshot, requested: signals.append((snapshot.pid, requested)) or True,
    )

    process_state._stop_child(  # type: ignore[arg-type]
        ExitedLeader(),
        terminate_grace=0.01,
        kill_wait=0.1,
        anchor=leader,
    )

    assert (502, signal.SIGTERM) in signals
    assert (502, signal.SIGKILL) in signals


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_lifecycle_start_record_is_accepted_by_runtime_full_ownership_validation(
    tmp_path: Path,
) -> None:
    base = _system_operations(tmp_path)
    operations = FullRuntimeFixtureOperations(
        base.data_root,
        base.release_root,
        terminate_grace=0.1,
        kill_wait=1,
    )
    manager = LifecycleManager(base.data_root, base.release_root, operations=operations)

    result = manager.start(lock_held=True)
    try:
        assert result == LifecycleResult(0, "started")
        assert verify_install.LocalProbes(base.data_root).ollama_port_state() == "owned"
        assert operations.validate("runtime-full") == ValidationOutcome(0, "healthy")
    finally:
        manager.stop(lock_held=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_supervisor_sigkill_is_reconciled_without_orphaning_service(
    tmp_path: Path,
) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"
    operations.launch("backend")
    payload = json.loads(record.read_text(encoding="utf-8"))
    supervisor_pid = payload["supervisor"]["pid"]
    service_pid = payload["service"]["pid"]
    supervisor_snapshot = process_state._snapshot(supervisor_pid)
    service_snapshot = process_state._snapshot(service_pid)
    try:
        assert supervisor_snapshot is not None
        assert service_snapshot is not None
        assert process_state._signal_snapshot(supervisor_snapshot, signal.SIGKILL)
        os.waitpid(supervisor_pid, 0)

        operations.reconcile()

        assert not record.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(service_pid, 0)
    finally:
        for snapshot in (service_snapshot, supervisor_snapshot):
            if snapshot is not None and process_state._token_is_live(snapshot):
                process_state._signal_snapshot(snapshot, signal.SIGKILL)
                with suppress(ChildProcessError):
                    os.waitpid(snapshot.pid, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_foreign_11435_listener_winning_launch_race_is_rejected(
    tmp_path: Path,
) -> None:
    base = _system_operations(tmp_path)

    class NonListeningOllama(FixtureSystemOperations):
        def __init__(self) -> None:
            super().__init__(
                base.data_root,
                base.release_root,
                terminate_grace=0.1,
                kill_wait=0.2,
            )
            self.first_state = True

        def state(self, kind: str) -> str:
            if kind == "ollama" and self.first_state:
                self.first_state = False
                return "absent"
            return super().state(kind)

        def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
            assert kind == "ollama"
            return [
                sys.executable,
                "-c",
                "import threading; threading.Event().wait()",
            ], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

    foreign = socket.socket()
    foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        foreign.bind(("127.0.0.1", 11435))
        foreign.listen()
    except OSError:
        foreign.close()
        pytest.skip("Ollama test port is occupied")
    operations = NonListeningOllama()
    try:
        with pytest.raises(ServiceError, match="owned-listener"):
            operations.launch("ollama")
    finally:
        foreign.close()
        record = operations.data_root / "runtime/ollama.pid"
        if record.exists():
            payload = json.loads(record.read_text(encoding="utf-8"))
            for name in ("service", "supervisor"):
                snapshot = process_state._snapshot(payload[name]["pid"])
                if snapshot is not None and process_state._token_is_live(snapshot):
                    process_state._signal_snapshot(snapshot, signal.SIGKILL)
                    with suppress(ChildProcessError):
                        os.waitpid(snapshot.pid, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_real_stop_kills_child_and_grandchild_after_leader_exits_on_term(
    tmp_path: Path,
) -> None:
    base = _system_operations(tmp_path)
    pid_file = tmp_path / "descendants.json"

    class DescendantFixtureOperations(FixtureSystemOperations):
        def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
            assert kind == "backend"
            script = (
                "import http.server,json,os,signal,sys;"
                "child=os.fork();"
                "\nif child==0:"
                "\n signal.signal(signal.SIGTERM,signal.SIG_IGN); grand=os.fork();"
                "\n if grand==0:"
                "\n  signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "\n  H=type('H',(http.server.BaseHTTPRequestHandler,),{"
                "'do_GET':lambda s:(s.send_response(200),s.send_header('Content-Type',"
                "'application/json'),s.end_headers(),s.wfile.write("
                "json.dumps({'status':'healthy'}).encode())),"
                "'log_message':lambda *a:None});"
                "\n  http.server.HTTPServer(('127.0.0.1',8765),H).serve_forever()"
                "\n else:"
                "\n  open(sys.argv[1],'w').write(json.dumps("
                "{'child':os.getpid(),'grandchild':grand}));"
                "\n  os.waitpid(grand,0)"
                "\nelse:"
                "\n os.waitpid(child,0)"
            )
            return [sys.executable, "-c", script, str(pid_file)], {
                "HOME": os.environ.get("HOME", "/"),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }

    operations = DescendantFixtureOperations(
        base.data_root,
        base.release_root,
        terminate_grace=0.1,
        kill_wait=1,
    )
    snapshots: list[ProcessSnapshot] = []
    operations.launch("backend")
    try:
        descendants = json.loads(pid_file.read_text(encoding="utf-8"))
        snapshots = [
            snapshot
            for pid in descendants.values()
            if (snapshot := process_state._snapshot(pid)) is not None
        ]
        assert len(snapshots) == 2

        operations.stop("backend")

        assert all(process_state._snapshot(snapshot.pid) is None for snapshot in snapshots)
    finally:
        for snapshot in snapshots:
            if process_state._token_is_live(snapshot):
                process_state._signal_snapshot(snapshot, signal.SIGKILL)
