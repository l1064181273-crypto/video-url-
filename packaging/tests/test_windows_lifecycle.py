from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

import windows_lifecycle  # noqa: E402
from windows_lifecycle import (  # noqa: E402
    LifecycleResult,
    SystemWindowsLifecycleOperations,
    WindowsLifecycleManager,
    WindowsServiceError,
)


class FakeOperations:
    def __init__(
        self,
        *,
        valid: bool = True,
        backend: str = "absent",
        ollama: str = "absent",
        healthy: bool = True,
        launch_failure: str | None = None,
        stop_failures: set[str] | None = None,
    ) -> None:
        self.valid = valid
        self.states = {"backend": backend, "ollama": ollama}
        self.healthy = healthy
        self.launch_failure = launch_failure
        self.stop_failures = stop_failures or set()
        self.calls: list[str] = []

    def validate_dependencies(self) -> bool:
        self.calls.append("validate:dependencies")
        return self.valid

    def state(self, kind: str) -> str:
        self.calls.append(f"state:{kind}")
        return self.states[kind]

    def launch(self, kind: str) -> None:
        self.calls.append(f"launch:{kind}")
        if self.launch_failure == kind:
            raise WindowsServiceError(f"{kind} launch failed")
        self.states[kind] = "owned"

    def backend_healthy(self) -> bool:
        self.calls.append("health:backend")
        return self.healthy

    def stop(self, kind: str) -> None:
        self.calls.append(f"stop:{kind}")
        self.states[kind] = "absent"
        if kind in self.stop_failures:
            raise WindowsServiceError(f"{kind} stop failed")


def test_start_orders_ollama_before_backend_and_checks_health() -> None:
    operations = FakeOperations()
    manager = WindowsLifecycleManager(operations)

    assert manager.start(lock_held=True) == LifecycleResult(0, "started")
    assert operations.calls == [
        "validate:dependencies",
        "state:backend",
        "state:ollama",
        "launch:ollama",
        "launch:backend",
        "health:backend",
    ]


def test_start_refuses_foreign_or_corrupt_state_without_launching() -> None:
    operations = FakeOperations(backend="unsafe")
    manager = WindowsLifecycleManager(operations)

    assert manager.start(lock_held=True) == LifecycleResult(2, "unsafe_or_corrupt")
    assert not any(call.startswith("launch:") for call in operations.calls)


def test_backend_launch_failure_stops_newly_started_ollama() -> None:
    operations = FakeOperations(launch_failure="backend")
    manager = WindowsLifecycleManager(operations)

    with pytest.raises(WindowsServiceError, match="backend launch"):
        manager.start(lock_held=True)

    assert operations.calls[-1] == "stop:ollama"


def test_start_and_cleanup_errors_are_preserved_in_order() -> None:
    operations = FakeOperations(
        launch_failure="backend",
        stop_failures={"ollama"},
    )
    manager = WindowsLifecycleManager(operations)

    with pytest.raises(ExceptionGroup) as captured:
        manager.start(lock_held=True)

    assert [str(error) for error in captured.value.exceptions] == [
        "backend launch failed",
        "ollama stop failed",
    ]


def test_stop_attempts_backend_then_ollama_and_aggregates_errors() -> None:
    operations = FakeOperations(
        backend="owned",
        ollama="owned",
        stop_failures={"backend", "ollama"},
    )
    manager = WindowsLifecycleManager(operations)

    with pytest.raises(ExceptionGroup) as captured:
        manager.stop(lock_held=True)

    assert [str(error) for error in captured.value.exceptions] == [
        "backend stop failed",
        "ollama stop failed",
    ]
    assert operations.calls == [
        "state:backend",
        "stop:backend",
        "state:ollama",
        "stop:ollama",
    ]


def test_prestart_distinguishes_missing_dependencies_and_already_running() -> None:
    assert WindowsLifecycleManager(FakeOperations(valid=False)).prestart(
        lock_held=True
    ) == LifecycleResult(1, "prerequisites_missing")
    assert WindowsLifecycleManager(FakeOperations(backend="owned", ollama="owned")).prestart(
        lock_held=True
    ) == LifecycleResult(0, "already_running")


def test_windows_service_commands_use_app_owned_executables_and_sanitized_environment(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "LocalVideoTranscriber"
    release_root = data_root / "app/releases/0.1.1"
    manifest = release_root / "packaging/dependencies.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        """
        {
          "target": "windows-x64",
          "artifacts": [
            {"id": "ollama", "version": "0.13.2", "architecture": "x86_64"}
          ]
        }
        """,
        encoding="utf-8",
    )
    state = data_root / "runtime/install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ffmpeg": {
                    "version": "8.0",
                    "directory": "tools/ffmpeg/8.0/bin",
                    "sha256": {"ffmpeg": "a" * 64, "ffprobe": "b" * 64},
                },
            }
        ),
        encoding="utf-8",
    )
    operations = SystemWindowsLifecycleOperations(
        data_root,
        release_root,
        api=object(),
    )

    ollama_command, ollama_environment = operations.service_command("ollama")
    backend_command, backend_environment = operations.service_command("backend")

    assert ollama_command == [
        str(data_root / "app/tools/ollama/0.13.2/ollama.exe"),
        "serve",
    ]
    assert backend_command == [
        str(release_root / ".venv/Scripts/python.exe"),
        "-m",
        "lvt.main",
    ]
    assert ollama_environment["OLLAMA_HOST"] == "127.0.0.1:11435"
    assert backend_environment["LVT_DATA_ROOT"] == str(data_root)
    assert backend_environment["LVT_INSTALLED_MODE"] == "1"
    for environment in (ollama_environment, backend_environment):
        assert "LVT_TOKEN" not in environment
        assert "OPENAI_API_KEY" not in environment


@pytest.mark.parametrize(
    ("record_exists", "status", "expected"),
    [
        (False, "owned", "backend.record_missing"),
        (True, "listener_pid_mismatch", "backend.listener_pid_mismatch"),
        (True, "service_identity_invalid", "backend.service_identity_invalid"),
        (True, "job_unavailable", "backend.job_unavailable"),
    ],
)
def test_ownership_timeout_diagnostics_do_not_expose_process_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_exists: bool,
    status: str,
    expected: str,
) -> None:
    record = tmp_path / "runtime/backend.pid"
    if record_exists:
        record.parent.mkdir()
        record.write_text("{}\n", encoding="ascii")
    monkeypatch.setattr(
        windows_lifecycle,
        "owned_service_record_status",
        lambda *_args, **_kwargs: status,
    )
    operations = SystemWindowsLifecycleOperations(
        tmp_path,
        tmp_path,
        api=object(),
    )

    assert operations._ownership_failure_stage("backend", record) == expected
