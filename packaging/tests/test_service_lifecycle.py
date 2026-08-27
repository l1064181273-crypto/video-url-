from __future__ import annotations

import argparse
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


def _ownership_payload(tmp_path: Path, *, generation: int = 1) -> dict[str, object]:
    executable = tmp_path / "owned-service"
    executable.write_bytes(b"service")
    supervisor = ProcessSnapshot(
        700,
        700,
        "supervisor",
        executable,
        1,
        2,
        "a" * 64,
        (1,) * 8,
    )
    service = ProcessSnapshot(
        701,
        700,
        "service",
        executable,
        1,
        2,
        "a" * 64,
        (2,) * 8,
    )
    return {
        "schema_version": 1,
        "record_generation": generation,
        "kind": "backend",
        "nonce": "a" * 32,
        "port": 8765,
        "supervisor": process_state._snapshot_payload(supervisor),
        "service": process_state._snapshot_payload(service),
        "members": [process_state._snapshot_payload(service)],
    }


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
            "record_generation",
            "kind",
            "nonce",
            "port",
            "supervisor",
            "service",
            "members",
        }
        assert payload["record_generation"] >= 1
        assert payload["members"][0] == payload["service"]
        assert payload["port"] == 8765
        assert record.stat().st_mode & 0o777 == 0o600
    finally:
        operations.stop("backend")

    assert operations.state("backend") == "absent"
    assert record.is_file()


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
    assert record.is_file()


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
    payload["members"] = [payload["service"]]
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
        "members": [process_state._snapshot_payload(snapshot)],
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


def test_reused_session_leader_pid_rejects_untracked_descendant_without_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "service"
    executable.write_bytes(b"service")
    old_leader = ProcessSnapshot(601, 601, "old", executable, 1, 2, "a" * 64, (1,) * 8)
    reused_descendant = ProcessSnapshot(
        602,
        601,
        "new",
        executable,
        1,
        2,
        "a" * 64,
        (2,) * 8,
    )
    signals: list[tuple[int, signal.Signals]] = []

    class ExitedLeader:
        pid = 601

        @staticmethod
        def poll() -> int | None:
            return 0

    monkeypatch.setattr(
        process_state,
        "_group_snapshots",
        lambda _pgid: (reused_descendant,),
    )
    monkeypatch.setattr(
        process_state,
        "_token_is_live",
        lambda snapshot: snapshot.signal_token == reused_descendant.signal_token,
    )
    monkeypatch.setattr(process_state.os, "getsid", lambda _pid: old_leader.pid)
    monkeypatch.setattr(
        process_state,
        "_signal_snapshot",
        lambda snapshot, requested: signals.append((snapshot.pid, requested)) or True,
    )

    with pytest.raises(ServiceError, match="ownership changed before TERM"):
        process_state._stop_child(  # type: ignore[arg-type]
            ExitedLeader(),
            terminate_grace=0.01,
            kill_wait=0.1,
            anchor=old_leader,
        )

    assert signals == []


@pytest.mark.parametrize("conflict", ["reused_member", "unknown_member"])
def test_orphan_recovery_rejects_unverified_members_without_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict: str,
) -> None:
    executable = tmp_path / "service"
    executable.write_bytes(b"service")
    supervisor = ProcessSnapshot(
        700,
        700,
        "supervisor",
        executable,
        1,
        2,
        "a" * 64,
        (1,) * 8,
    )
    member = ProcessSnapshot(
        701,
        700,
        "member",
        executable,
        1,
        2,
        "a" * 64,
        (2,) * 8,
    )
    replacement = ProcessSnapshot(
        701 if conflict == "reused_member" else 702,
        700,
        "foreign",
        executable,
        1,
        2,
        "a" * 64,
        (3,) * 8,
    )
    payload = {
        "schema_version": 1,
        "kind": "backend",
        "nonce": "a" * 32,
        "port": 8765,
        "supervisor": process_state._snapshot_payload(supervisor),
        "service": process_state._snapshot_payload(member),
        "members": [process_state._snapshot_payload(member)],
    }
    live = (
        {member.pid: replacement}
        if conflict == "reused_member"
        else {member.pid: member, replacement.pid: replacement}
    )
    signals: list[int] = []
    monkeypatch.setattr(process_state, "_snapshot", lambda pid: live.get(pid))
    monkeypatch.setattr(
        process_state,
        "_group_snapshots",
        lambda _pgid: tuple(live.values()),
    )
    monkeypatch.setattr(process_state, "_token_is_live", lambda _snapshot: True)
    monkeypatch.setattr(
        process_state,
        "_signal_snapshot",
        lambda snapshot, _requested: signals.append(snapshot.pid) or True,
    )
    operations = _system_operations(tmp_path)

    with pytest.raises(ServiceError, match="ownership is unverified"):
        operations._stop_recorded_members("backend", payload)

    assert signals == []


def test_converged_cleanup_retains_claim_and_rejects_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"
    payload = _ownership_payload(tmp_path)
    record.write_text(json.dumps(payload), encoding="utf-8")
    record.chmod(0o600)
    monkeypatch.setattr(process_state, "_verified_record_members", lambda _payload: ())
    monkeypatch.setattr(process_state, "_port_open", lambda _port: False)

    claimed = operations._claim_record("backend")
    assert claimed is not None
    operations._remove_converged_record(claimed, "backend")
    claim_path = record.parent / claimed.name
    assert claim_path.is_file()

    displaced = record.parent / "displaced-claim"
    claim_path.rename(displaced)
    claim_path.write_text("{}\n", encoding="utf-8")
    claim_path.chmod(0o600)
    try:
        with pytest.raises(ServiceError, match="record changed"):
            operations._remove_converged_record(claimed, "backend")
        assert claim_path.read_text(encoding="utf-8") == "{}\n"
        assert json.loads(displaced.read_text(encoding="utf-8")) == payload
    finally:
        claimed.close()


def test_dangling_record_and_replaced_runtime_parent_are_unsafe(tmp_path: Path) -> None:
    operations = _system_operations(tmp_path)
    runtime = operations.data_root / "runtime"
    record = runtime / "backend.pid"
    record.symlink_to(tmp_path / "missing-record")

    assert operations.state("backend") == "unsafe"
    with pytest.raises(ServiceError, match="unverified"):
        operations.stop("backend")
    assert record.is_symlink()

    trusted_runtime = operations.data_root / "trusted-runtime"
    runtime.rename(trusted_runtime)
    replacement = tmp_path / "replacement-runtime"
    replacement.mkdir()
    runtime.symlink_to(replacement, target_is_directory=True)

    assert operations.state("backend") == "unsafe"
    with pytest.raises(ServiceError, match="runtime parent changed"):
        operations.stop("backend")
    assert runtime.is_symlink()


def test_backend_and_ollama_dangling_records_are_both_unsafe(tmp_path: Path) -> None:
    operations = _system_operations(tmp_path)
    runtime = operations.data_root / "runtime"
    for kind in ("backend", "ollama"):
        (runtime / f"{kind}.pid").symlink_to(tmp_path / f"missing-{kind}")

    assert operations.state("backend") == "unsafe"
    assert operations.state("ollama") == "unsafe"
    for kind in ("backend", "ollama"):
        with pytest.raises(ServiceError, match="unverified"):
            operations.stop(kind)
        assert (runtime / f"{kind}.pid").is_symlink()


def test_stop_claim_never_removes_public_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"
    payload = _ownership_payload(tmp_path)
    record.write_text(json.dumps(payload), encoding="utf-8")
    record.chmod(0o600)
    claimed = threading.Barrier(2)
    resume = threading.Barrier(2)
    signals: list[int] = []

    def synchronized_claim(name: str) -> None:
        if name == "claim:before_source_check":
            claimed.wait(timeout=5)
            resume.wait(timeout=5)

    monkeypatch.setattr(process_state, "_record_boundary", synchronized_claim)
    monkeypatch.setattr(process_state, "_verified_record_members", lambda _payload: ())
    monkeypatch.setattr(process_state, "_port_open", lambda _port: False)
    monkeypatch.setattr(
        process_state,
        "_signal_snapshot",
        lambda snapshot, _requested: signals.append(snapshot.pid) or True,
    )
    errors: list[BaseException] = []

    def stop() -> None:
        try:
            operations.stop("backend")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=stop)
    worker.start()
    claimed.wait(timeout=5)
    displaced = record.parent / "displaced-owned-record"
    record.rename(displaced)
    record.symlink_to(tmp_path / "foreign-target")
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ServiceError)
    assert signals == []
    assert record.is_symlink()
    assert json.loads(displaced.read_text(encoding="utf-8")) == payload


def test_record_writer_preserves_source_replacement_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"
    current = _ownership_payload(tmp_path)
    record.write_text(json.dumps(current), encoding="utf-8")
    record.chmod(0o600)
    replacement = record.parent / "foreign-record"
    replacement.write_text("foreign\n", encoding="utf-8")
    replacement.chmod(0o600)
    displaced = record.parent / "displaced-owned-record"
    before_claim = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def synchronized_write(name: str) -> None:
        if name == "write:before_source_check":
            before_claim.wait(timeout=5)
            resume.wait(timeout=5)

    monkeypatch.setattr(process_state, "_record_boundary", synchronized_write)

    def write() -> None:
        try:
            process_state._write_record(
                record,
                _ownership_payload(tmp_path, generation=2),
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=write)
    worker.start()
    before_claim.wait(timeout=5)
    record.rename(displaced)
    replacement.rename(record)
    foreign_before = record.stat()
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ServiceError)
    foreign_after = record.stat()
    assert (foreign_after.st_dev, foreign_after.st_ino) == (
        foreign_before.st_dev,
        foreign_before.st_ino,
    )
    assert record.read_text(encoding="utf-8") == "foreign\n"
    assert json.loads(displaced.read_text(encoding="utf-8")) == current


def test_record_writer_never_overwrites_unverified_public_entry(tmp_path: Path) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"
    record.write_text("foreign-record\n", encoding="utf-8")
    record.chmod(0o600)
    before = record.stat()

    with pytest.raises(ServiceError, match="unverified"):
        process_state._write_record(record, _ownership_payload(tmp_path))

    after = record.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert record.read_text(encoding="utf-8") == "foreign-record\n"


def test_public_record_generation_must_exceed_retained_same_nonce(
    tmp_path: Path,
) -> None:
    operations = _system_operations(tmp_path)
    runtime = operations.data_root / "runtime"
    record = runtime / "backend.pid"
    record.write_text(json.dumps(_ownership_payload(tmp_path, generation=1)), encoding="utf-8")
    record.chmod(0o600)
    claim = runtime / ".backend.pid.claim-00000000000000000005-retained"
    claim.write_text(json.dumps(_ownership_payload(tmp_path, generation=5)), encoding="utf-8")
    claim.chmod(0o600)

    with pytest.raises(ServiceError, match="generation"):
        operations._open_current_record("backend")

    record.write_text(json.dumps(_ownership_payload(tmp_path, generation=2)), encoding="utf-8")
    with pytest.raises(ServiceError, match="generation"):
        operations._open_current_record("backend")


def test_duplicate_or_corrupt_retained_generation_blocks_public_record(
    tmp_path: Path,
) -> None:
    operations = _system_operations(tmp_path)
    runtime = operations.data_root / "runtime"
    record = runtime / "backend.pid"
    record.write_text(json.dumps(_ownership_payload(tmp_path, generation=6)), encoding="utf-8")
    record.chmod(0o600)
    for suffix in ("left", "right"):
        claim = runtime / f".backend.pid.claim-00000000000000000005-{suffix}"
        claim.write_text(json.dumps(_ownership_payload(tmp_path, generation=5)), encoding="utf-8")
        claim.chmod(0o600)

    with pytest.raises(ServiceError, match="duplicate"):
        operations._open_current_record("backend")

    for claim in runtime.glob(".backend.pid.claim-*"):
        claim.unlink()
    corrupt = runtime / ".backend.pid.claim-00000000000000000005-corrupt"
    corrupt.write_text("{", encoding="utf-8")
    corrupt.chmod(0o600)
    with pytest.raises(ServiceError, match="unverified"):
        operations._open_current_record("backend")


@pytest.mark.parametrize("kind", ["backend", "ollama"])
@pytest.mark.parametrize("entry_kind", ["fifo", "socket", "directory"])
def test_special_ownership_record_is_bounded_unsafe(
    tmp_path: Path,
    kind: str,
    entry_kind: str,
) -> None:
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime" / f"{kind}.pid"
    held_socket: socket.socket | None = None
    short_link: Path | None = None
    if entry_kind == "fifo":
        os.mkfifo(record, 0o600)
    elif entry_kind == "socket":
        held_socket = socket.socket(socket.AF_UNIX)
        short_link = Path("/tmp") / f"lvt-record-{os.getpid()}-{kind}"
        short_link.unlink(missing_ok=True)
        short_link.symlink_to(record.parent, target_is_directory=True)
        held_socket.bind(str(short_link / record.name))
    elif entry_kind == "directory":
        record.mkdir()
    else:
        raise AssertionError("unknown special entry kind")

    result: list[str] = []
    worker = threading.Thread(target=lambda: result.append(operations.state(kind)), daemon=True)
    worker.start()
    worker.join(timeout=1)
    try:
        assert not worker.is_alive()
        assert result == ["unsafe"]
    finally:
        if held_socket is not None:
            held_socket.close()
        if short_link is not None:
            short_link.unlink(missing_ok=True)


@pytest.mark.parametrize(("kind", "port"), [("backend", 8765), ("ollama", 11435)])
def test_device_record_open_is_nonblocking_and_rejected(kind: str, port: int) -> None:
    parent = os.open(
        "/dev",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(process_state._open_record_at(parent, "null", kind, port)),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)
    try:
        assert not worker.is_alive()
        assert result == [None]
    finally:
        os.close(parent)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_retained_record_claims_allow_a_new_service_generation(tmp_path: Path) -> None:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            pytest.skip("backend test port is occupied")
    operations = _system_operations(tmp_path)
    record = operations.data_root / "runtime/backend.pid"

    first = operations.launch("backend")
    operations.stop("backend")
    second = operations.launch("backend")
    try:
        assert second.nonce != first.nonce
        assert operations.state("backend") == "owned"
    finally:
        operations.stop("backend")

    generations = list((operations.data_root / "runtime").glob(".backend.pid.generation-*"))
    assert record.is_file()
    assert len(generations) >= 1


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

        assert record.is_file()
        with pytest.raises(ProcessLookupError):
            os.kill(service_pid, 0)
    finally:
        for snapshot in (service_snapshot, supervisor_snapshot):
            if snapshot is not None and process_state._token_is_live(snapshot):
                process_state._signal_snapshot(snapshot, signal.SIGKILL)
                with suppress(ChildProcessError):
                    os.waitpid(snapshot.pid, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_reconcile_reaps_recorded_descendant_after_leader_and_supervisor_exit(
    tmp_path: Path,
) -> None:
    base = _system_operations(tmp_path)
    descendant_path = tmp_path / "descendant.pid"
    release_path = tmp_path / "release-leader"

    class DetachedDescendantOperations(FixtureSystemOperations):
        def _service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
            assert kind == "backend"
            script = (
                "import http.server,json,os,signal,sys,threading;"
                "descendant=os.fork();"
                "\nif descendant==0:"
                "\n signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));signal.pause()"
                "\nelse:"
                "\n H=type('H',(http.server.BaseHTTPRequestHandler,),{"
                "'do_GET':lambda s:(s.send_response(200),s.send_header('Content-Type',"
                "'application/json'),s.end_headers(),s.wfile.write("
                "json.dumps({'status':'healthy'}).encode())),"
                "'log_message':lambda *a:None});"
                "server=http.server.HTTPServer(('127.0.0.1',8765),H);"
                "threading.Thread(target=server.serve_forever,daemon=True).start();"
                "\n while not os.path.exists(sys.argv[2]):"
                "\n  threading.Event().wait(0.01)"
            )
            return [
                sys.executable,
                "-c",
                script,
                str(descendant_path),
                str(release_path),
            ], {
                "HOME": os.environ.get("HOME", "/"),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            }

    operations = DetachedDescendantOperations(
        base.data_root,
        base.release_root,
        terminate_grace=5,
        kill_wait=1,
    )
    record = operations.data_root / "runtime/backend.pid"
    snapshots: list[ProcessSnapshot] = []
    operations.launch("backend")
    try:
        descendant_pid = int(descendant_path.read_text(encoding="ascii"))
        for _ in range(500):
            payload = process_state._read_record_payload(record, "backend", 8765)
            assert payload is not None
            members = payload.get("members", [])
            if any(member.get("pid") == descendant_pid for member in members):
                break
            threading.Event().wait(0.01)
        else:
            pytest.fail("descendant identity was not persisted")

        supervisor_snapshot = process_state._snapshot(payload["supervisor"]["pid"])
        leader_snapshot = process_state._snapshot(payload["service"]["pid"])
        descendant_snapshot = process_state._snapshot(descendant_pid)
        snapshots = [
            snapshot
            for snapshot in (supervisor_snapshot, leader_snapshot, descendant_snapshot)
            if snapshot is not None
        ]
        assert supervisor_snapshot is not None
        assert leader_snapshot is not None
        assert descendant_snapshot is not None

        release_path.write_text("exit\n", encoding="ascii")
        for _ in range(500):
            if not process_state._token_is_live(leader_snapshot):
                break
            threading.Event().wait(0.01)
        assert not process_state._token_is_live(leader_snapshot)
        assert process_state._token_is_live(descendant_snapshot)
        assert record.exists()

        assert process_state._signal_snapshot(supervisor_snapshot, signal.SIGKILL)
        os.waitpid(supervisor_snapshot.pid, 0)
        assert process_state._token_is_live(descendant_snapshot)
        assert record.exists()

        operations.reconcile()

        assert not process_state._token_is_live(descendant_snapshot)
        assert record.is_file()
    finally:
        if record.exists():
            cleanup_payload = json.loads(record.read_text(encoding="utf-8"))
            cleanup_entries = [
                cleanup_payload.get("supervisor"),
                cleanup_payload.get("service"),
                *cleanup_payload.get("members", []),
            ]
            for entry in cleanup_entries:
                if isinstance(entry, dict):
                    snapshot = process_state._snapshot(entry.get("pid", -1))
                    if snapshot is not None and snapshot not in snapshots:
                        snapshots.append(snapshot)
        for snapshot in snapshots:
            if process_state._token_is_live(snapshot):
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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
def test_supervisor_adopts_descendants_when_leader_exits_before_first_group_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "runtime/backend.pid"
    record.parent.mkdir(parents=True)
    record_parent_fd = os.open(
        record.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    descendants_path = tmp_path / "descendants.json"
    ready_read, ready_write = os.pipe()
    activation_read, activation_write = os.pipe()
    released = False
    original_extend = process_state._extend_tracked_group
    script = (
        "import json,os,signal,sys;"
        "control=int(os.environ['LVT_PRECOMMIT_ACTIVATION_FD']);"
        "ready_r,ready_w=os.pipe();child=os.fork();"
        "\nif child==0:"
        "\n os.close(ready_r);signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "grand=os.fork();"
        "\n if grand==0:"
        "\n  signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.pause()"
        "\n else:"
        "\n  open(sys.argv[1],'w').write(json.dumps("
        "{'child':os.getpid(),'grandchild':grand}));"
        "os.write(ready_w,b'R');os.close(ready_w);signal.pause()"
        "\nelse:"
        "\n os.close(ready_w);os.read(ready_r,1);os.close(ready_r);"
        "os.read(control,1);os.close(control)"
    )

    def release_before_first_scan(
        anchor: ProcessSnapshot,
        tracked: dict[int, ProcessSnapshot],
    ) -> tuple[ProcessSnapshot, ...] | None:
        nonlocal released
        if not released:
            released = True
            service = next(snapshot for pid, snapshot in tracked.items() if pid != anchor.pid)
            os.write(activation_write, b"X")
            found, status = os.waitpid(service.pid, 0)
            assert found == service.pid
            assert os.waitstatus_to_exitcode(status) == 0
        return original_extend(anchor, tracked)

    monkeypatch.setattr(process_state, "_extend_tracked_group", release_before_first_scan)
    arguments = argparse.Namespace(
        record=record,
        record_parent_fd=record_parent_fd,
        kind="backend",
        nonce="a" * 32,
        port=8765,
        ready_fd=ready_write,
        activation_fd=activation_read,
        terminate_grace=0.01,
        kill_wait=1.0,
        command=[sys.executable, "-c", script, str(descendants_path)],
    )
    supervisor_pid = os.fork()
    if supervisor_pid == 0:
        os.close(ready_read)
        try:
            os.setsid()
            result = process_state._supervise(arguments)
        except BaseException:
            result = 70
        finally:
            os._exit(result)

    os.close(ready_write)
    os.close(record_parent_fd)
    os.close(activation_read)
    os.close(activation_write)
    supervisor_snapshot = process_state._snapshot(supervisor_pid)
    descendants: dict[str, int] = {}
    try:
        assert supervisor_snapshot is not None
        assert os.read(ready_read, 1) == b"R"
        descendants = json.loads(descendants_path.read_text(encoding="utf-8"))
        found, status = os.waitpid(supervisor_pid, 0)
        assert found == supervisor_pid
        assert os.waitstatus_to_exitcode(status) == 0
        assert all(process_state._snapshot(pid) is None for pid in descendants.values())
        assert record.is_file()
    finally:
        with suppress(OSError):
            os.close(ready_read)
        if supervisor_snapshot is not None and process_state._token_is_live(supervisor_snapshot):
            process_state._signal_snapshot(supervisor_snapshot, signal.SIGKILL)
            with suppress(ChildProcessError):
                os.waitpid(supervisor_pid, 0)
        for pid in descendants.values():
            snapshot = process_state._snapshot(pid)
            if snapshot is not None and process_state._token_is_live(snapshot):
                assert process_state._signal_snapshot(snapshot, signal.SIGKILL)
