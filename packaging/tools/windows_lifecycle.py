from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lifecycle_lock import LifecycleLock
from runtime_layout import path_is_link_like
from windows_service import (
    SERVICE_PORTS,
    NativeWindowsServiceApi,
    WindowsServiceApi,
    WindowsServiceError,
    _read_service_record,
    owned_service_record_status,
    retire_service_record,
    stop_verified_service,
    verify_owned_service_record,
)

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


@dataclass(frozen=True)
class LifecycleResult:
    exit_code: int
    status: str


class WindowsLifecycleOperations(Protocol):
    def validate_dependencies(self) -> bool: ...

    def state(self, kind: str) -> str: ...

    def launch(self, kind: str) -> None: ...

    def backend_healthy(self) -> bool: ...

    def stop(self, kind: str) -> None: ...


class SupervisorProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


SupervisorLauncher = Callable[
    [list[str], dict[str, str], Path, Path],
    SupervisorProcess,
]


class WindowsLifecycleManager:
    def __init__(self, operations: WindowsLifecycleOperations) -> None:
        self.operations = operations

    def prestart(self, *, lock_held: bool = False) -> LifecycleResult:
        if not lock_held:
            raise WindowsServiceError("Windows lifecycle lock is required")
        if not self.operations.validate_dependencies():
            return LifecycleResult(1, "prerequisites_missing")
        backend = self.operations.state("backend")
        ollama = self.operations.state("ollama")
        if backend == "unsafe" or ollama == "unsafe":
            return LifecycleResult(2, "unsafe_or_corrupt")
        if backend == "owned" and ollama == "owned":
            return LifecycleResult(0, "already_running")
        if backend == "owned" and ollama != "owned":
            return LifecycleResult(2, "unsafe_or_corrupt")
        return LifecycleResult(0, "ready_to_start")

    def start(self, *, lock_held: bool = False) -> LifecycleResult:
        if not lock_held:
            raise WindowsServiceError("Windows lifecycle lock is required")
        if not self.operations.validate_dependencies():
            return LifecycleResult(1, "prerequisites_missing")
        states = {
            "backend": self.operations.state("backend"),
            "ollama": self.operations.state("ollama"),
        }
        if "unsafe" in states.values() or (
            states["backend"] == "owned" and states["ollama"] != "owned"
        ):
            return LifecycleResult(2, "unsafe_or_corrupt")
        if all(state == "owned" for state in states.values()):
            return LifecycleResult(0, "already_running")

        started: list[str] = []
        try:
            for kind in ("ollama", "backend"):
                if states[kind] == "owned":
                    continue
                self.operations.launch(kind)
                started.append(kind)
            if not self.operations.backend_healthy():
                raise WindowsServiceError("backend health check failed")
            return LifecycleResult(0, "started")
        except Exception as start_error:
            cleanup_errors: list[Exception] = []
            for kind in reversed(started):
                try:
                    self.operations.stop(kind)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExceptionGroup(
                    "Windows service start and cleanup failed",
                    [start_error, *cleanup_errors],
                ) from start_error
            raise

    def stop(self, *, lock_held: bool = False) -> LifecycleResult:
        if not lock_held:
            raise WindowsServiceError("Windows lifecycle lock is required")
        errors: list[Exception] = []
        for kind in ("backend", "ollama"):
            state = self.operations.state(kind)
            if state == "absent":
                continue
            if state != "owned":
                errors.append(WindowsServiceError(f"{kind} service ownership is unsafe"))
                continue
            try:
                self.operations.stop(kind)
            except Exception as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Windows service stop failed", errors)
        return LifecycleResult(0, "stopped")


def _launch_supervisor(
    command: list[str],
    environment: dict[str, str],
    cwd: Path,
    log_path: Path,
) -> SupervisorProcess:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as log:
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )


class SystemWindowsLifecycleOperations:
    def __init__(
        self,
        data_root: Path,
        release_root: Path,
        *,
        api: WindowsServiceApi | object | None = None,
        launcher: SupervisorLauncher | None = None,
        backend_environment_overrides: dict[str, str] | None = None,
    ) -> None:
        self.data_root = data_root
        self.release_root = release_root
        self.api = NativeWindowsServiceApi() if api is None else api
        self.launcher = launcher or _launch_supervisor
        self.backend_environment_overrides = backend_environment_overrides or {}
        self.diagnostic_stage = "idle"

    def _record_path(self, kind: str) -> Path:
        if kind not in SERVICE_PORTS:
            raise WindowsServiceError("unknown service kind")
        return self.data_root / "runtime" / f"{kind}.pid"

    def _base_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in (
            "APPDATA",
            "COMSPEC",
            "LOCALAPPDATA",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
        environment["PATH"] = (
            str(Path(system_root) / "System32") if system_root else r"C:\Windows\System32"
        )
        environment["PYTHONUTF8"] = "1"
        return environment

    def _dependencies(self) -> dict[str, object]:
        path = self.release_root / "packaging" / "dependencies.json"
        if path_is_link_like(path) or not path.is_file():
            raise WindowsServiceError("dependency manifest is unavailable")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("target") != "windows-x64"
                or not isinstance(payload.get("artifacts"), list)
            ):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise WindowsServiceError("dependency manifest is invalid") from exc
        return payload

    def service_command(self, kind: str) -> tuple[list[str], dict[str, str]]:
        environment = self._base_environment()
        if kind == "ollama":
            dependencies = self._dependencies()
            try:
                artifact = next(
                    item
                    for item in dependencies["artifacts"]  # type: ignore[union-attr]
                    if isinstance(item, dict) and item.get("id") == "ollama"
                )
                version = artifact["version"]
                if not isinstance(version, str) or not version:
                    raise ValueError
            except (KeyError, StopIteration, TypeError, ValueError) as exc:
                raise WindowsServiceError("Ollama dependency metadata is invalid") from exc
            executable = self.data_root / "app" / "tools" / "ollama" / version / "ollama.exe"
            environment["PATH"] = f"{executable.parent};{environment['PATH']}"
            environment["OLLAMA_HOST"] = "127.0.0.1:11435"
            environment["OLLAMA_MODELS"] = str(self.data_root / "models" / "ollama")
            return [str(executable), "serve"], environment
        if kind != "backend":
            raise WindowsServiceError("unknown service kind")
        python = self.release_root / ".venv" / "Scripts" / "python.exe"
        state_path = self.data_root / "runtime" / "install-state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            ffmpeg_directory = state["ffmpeg"]["directory"]
            if not isinstance(ffmpeg_directory, str) or not ffmpeg_directory:
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WindowsServiceError("FFmpeg install state is invalid") from exc
        environment.update(
            {
                "LVT_DATA_ROOT": str(self.data_root),
                "LVT_MODEL_ROOT": str(self.data_root / "models"),
                "LVT_INSTALLED_MODE": "1",
                "LVT_FFMPEG_DIR": str(self.data_root / "app" / ffmpeg_directory),
                "LVT_OLLAMA_URL": "http://127.0.0.1:11435",
                "LVT_WORKER_CONCURRENCY": "1",
                "PYTHONPATH": str(self.release_root / "backend" / "src"),
            }
        )
        environment.update(self.backend_environment_overrides)
        environment["PATH"] = f"{python.parent};{environment['PATH']}"
        return [str(python), "-m", "lvt.main"], environment

    def validate_dependencies(self) -> bool:
        python = self.release_root / ".venv" / "Scripts" / "python.exe"
        validator = self.release_root / "packaging" / "tools" / "verify_install.py"
        completed = subprocess.run(
            [
                str(python),
                str(validator),
                "--phase",
                "dependencies",
                "--target",
                "windows-x64",
                "--data-root",
                str(self.data_root),
                "--release-root",
                str(self.release_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            close_fds=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return (
            completed.returncode == 0
            and isinstance(payload, dict)
            and payload.get("exit_code") == 0
            and payload.get("status") == "healthy"
        )

    def state(self, kind: str) -> str:
        port = SERVICE_PORTS[kind]
        record_path = self._record_path(kind)
        if record_path.exists() or path_is_link_like(record_path):
            return (
                "owned"
                if verify_owned_service_record(
                    record_path,
                    kind,
                    port,
                    api=self.api,  # type: ignore[arg-type]
                )
                else "unsafe"
            )
        try:
            listeners = self.api.listener_pids(port)  # type: ignore[union-attr]
        except OSError:
            return "unsafe"
        return "absent" if not listeners else "unsafe"

    def _next_generation(self, kind: str) -> int:
        maximum = 0
        history = self.data_root / "runtime" / "history"
        if history.exists():
            if path_is_link_like(history) or not history.is_dir():
                raise WindowsServiceError("service history is unsafe")
            for path in history.glob(f"{kind}-*.json"):
                record = _read_service_record(path)
                maximum = max(maximum, record.generation)
        return maximum + 1

    def _ownership_failure_stage(self, kind: str, record_path: Path) -> str:
        if not record_path.is_file():
            return f"{kind}.record_missing"
        status = owned_service_record_status(
            record_path,
            kind,
            SERVICE_PORTS[kind],
            api=self.api,  # type: ignore[arg-type]
        )
        return f"{kind}.{status}"

    def launch(self, kind: str) -> None:
        self.diagnostic_stage = f"{kind}.preflight"
        if self.state(kind) != "absent":
            raise WindowsServiceError(f"{kind} service is not safe to launch")
        record_path = self._record_path(kind)
        record_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        nonce = secrets.token_hex(16)
        generation = self._next_generation(kind)
        service_command, environment = self.service_command(kind)
        python = self.release_root / ".venv" / "Scripts" / "python.exe"
        supervisor = self.release_root / "packaging" / "tools" / "windows_supervisor.py"
        command = [
            str(python),
            str(supervisor),
            "--record",
            str(record_path),
            "--kind",
            kind,
            "--port",
            str(SERVICE_PORTS[kind]),
            "--nonce",
            nonce,
            "--generation",
            str(generation),
            "--cwd",
            str(self.release_root),
            "--",
            *service_command,
        ]
        self.diagnostic_stage = f"{kind}.supervisor_launch"
        process = self.launcher(
            command,
            environment,
            self.release_root,
            self.data_root / "logs" / f"{kind}.log",
        )
        self.diagnostic_stage = f"{kind}.ownership_wait"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if verify_owned_service_record(
                record_path,
                kind,
                SERVICE_PORTS[kind],
                api=self.api,  # type: ignore[arg-type]
            ):
                self.diagnostic_stage = f"{kind}.owned"
                return
            exit_code = process.poll()
            if exit_code is not None:
                self.diagnostic_stage = f"{kind}.supervisor_exit_{exit_code}"
                break
            time.sleep(0.05)
        if process.poll() is None:
            self.diagnostic_stage = self._ownership_failure_stage(kind, record_path)
            process.terminate()
            process.wait(timeout=5)
        raise WindowsServiceError(f"{kind} supervisor failed to establish ownership")

    def backend_healthy(self) -> bool:
        self.diagnostic_stage = "backend.health_wait"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=0.5)
            try:
                connection.request("GET", "/health", headers={"Accept": "application/json"})
                response = connection.getresponse()
                body = response.read(65_537)
                if response.status == 200 and len(body) <= 65_536:
                    payload = json.loads(body)
                    if isinstance(payload, dict) and payload.get("status") == "healthy":
                        self.diagnostic_stage = "backend.healthy"
                        return True
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            finally:
                connection.close()
            time.sleep(0.05)
        self.diagnostic_stage = "backend.health_timeout"
        return False

    def stop(self, kind: str) -> None:
        record_path = self._record_path(kind)
        if not record_path.exists() and not path_is_link_like(record_path):
            listeners = self.api.listener_pids(SERVICE_PORTS[kind])  # type: ignore[union-attr]
            if listeners:
                raise WindowsServiceError(f"{kind} listener is not owned")
            return
        record = _read_service_record(record_path)
        stop_verified_service(record, self.api)  # type: ignore[arg-type]
        deadline = time.monotonic() + 5
        while record_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if record_path.exists():
            retire_service_record(record_path, record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Local Video Transcriber on Windows")
    parser.add_argument("action", choices=("prestart", "start", "stop"))
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    result = LifecycleResult(2, "unsafe_or_corrupt")
    lock_operation = "start" if arguments.action == "prestart" else arguments.action
    lock = LifecycleLock(arguments.data_root / "app", operation=lock_operation)
    try:
        lock.acquire_bootstrap_then_flock()
        manager = WindowsLifecycleManager(
            SystemWindowsLifecycleOperations(arguments.data_root, arguments.release_root)
        )
        if arguments.action == "prestart":
            result = manager.prestart(lock_held=True)
        elif arguments.action == "start":
            result = manager.start(lock_held=True)
        else:
            result = manager.stop(lock_held=True)
    except Exception:
        result = LifecycleResult(2, "unsafe_or_corrupt")
    finally:
        lock.close()
    print(json.dumps({"schema_version": 1, "status": result.status}, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
