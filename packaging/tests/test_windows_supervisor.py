from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

from windows_process import ExecutableIdentity, ProcessIdentity  # noqa: E402
from windows_supervisor import supervise_service  # noqa: E402


def _identity(pid: int, name: str) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        creation_time=10_000 + pid,
        executable=ExecutableIdentity(
            path=rf"C:\LVT\{name}.exe",
            volume_serial=9,
            file_index=20_000 + pid,
            sha256=(f"{pid:064x}")[-64:],
        ),
    )


class FakeSupervisorApi:
    def __init__(self) -> None:
        self.identities = {
            700: _identity(700, "supervisor"),
            731: _identity(731, "python"),
            732: _identity(732, "python-runtime"),
        }
        self.calls: list[tuple[object, ...]] = []
        self.wait_count = 0

    def create_kill_on_close_job(self, name: str) -> object:
        self.calls.append(("create_job", name))
        return "job"

    def create_process_suspended(
        self,
        command: list[str],
        environment: dict[str, str],
        cwd: str,
    ) -> tuple[object, object, int]:
        self.calls.append(("create_process", tuple(command), environment, cwd))
        return ("process", 731), "thread", 731

    def assign_process_to_job(self, job: object, process: object) -> None:
        self.calls.append(("assign", job, process))

    def resume_thread(self, thread: object) -> None:
        self.calls.append(("resume", thread))

    def terminate_process(self, process: object, exit_code: int) -> None:
        self.calls.append(("terminate_process", process, exit_code))

    def terminate_job(self, job: object, exit_code: int) -> None:
        self.calls.append(("terminate_job", job, exit_code))

    def close_handle(self, handle: object) -> None:
        self.calls.append(("close", handle))

    def open_process(self, pid: int, *, terminate: bool) -> object:
        self.calls.append(("open_process", pid, terminate))
        return ("process", pid)

    def process_creation_time(self, handle: object) -> int:
        return self.identities[handle[1]].creation_time  # type: ignore[index]

    def process_image_path(self, handle: object) -> str:
        return self.identities[handle[1]].executable.path  # type: ignore[index]

    def open_executable(self, path: str) -> object:
        return ("file", path)

    def file_identity(self, handle: object) -> tuple[int, int]:
        identity = next(
            item.executable
            for item in self.identities.values()
            if item.executable.path == handle[1]  # type: ignore[index]
        )
        return identity.volume_serial, identity.file_index

    def sha256_file(self, handle: object) -> str:
        return next(
            item.executable.sha256
            for item in self.identities.values()
            if item.executable.path == handle[1]  # type: ignore[index]
        )

    def wait_process(self, handle: object, timeout_ms: int) -> bool:
        self.calls.append(("wait", handle, timeout_ms))
        self.wait_count += 1
        return self.wait_count >= 2

    def process_exit_code(self, handle: object) -> int:
        self.calls.append(("exit_code", handle))
        return 0

    def listener_pids(self, port: int) -> set[int]:
        return {732}

    def open_job(self, name: str) -> object:
        return ("job", name)

    def process_in_job(self, process: object, job: object) -> bool:
        self.calls.append(("process_in_job", process, job))
        return process == ("process", 732) and job == "job"


def test_supervisor_holds_job_until_service_exit_and_retires_record(tmp_path: Path) -> None:
    api = FakeSupervisorApi()
    record_path = tmp_path / "backend.pid"

    result = supervise_service(
        record_path=record_path,
        kind="backend",
        port=8765,
        nonce="d" * 32,
        generation=8,
        command=[r"C:\LVT\python.exe", "-m", "lvt.main"],
        environment={"LVT_DATA_ROOT": r"C:\LVT\data"},
        cwd=r"C:\LVT",
        api=api,
        current_pid=700,
    )

    assert result == 0
    assert not record_path.exists()
    retained = tmp_path / "history" / f"backend-8-{'d' * 32}.json"
    payload = json.loads(retained.read_text(encoding="utf-8"))
    assert payload["supervisor"] == _identity(700, "supervisor").as_dict()
    assert payload["service"] == _identity(732, "python-runtime").as_dict()
    assert ("process_in_job", ("process", 732), "job") in api.calls
    assign = next(index for index, call in enumerate(api.calls) if call[0] == "assign")
    resume = next(index for index, call in enumerate(api.calls) if call[0] == "resume")
    wait = next(index for index, call in enumerate(api.calls) if call[0] == "wait")
    close_job = max(index for index, call in enumerate(api.calls) if call == ("close", "job"))
    assert assign < resume < wait < close_job


def test_supervisor_publication_failure_terminates_job_and_preserves_foreign_record(
    tmp_path: Path,
) -> None:
    api = FakeSupervisorApi()
    record_path = tmp_path / "backend.pid"
    record_path.write_bytes(b"foreign")

    with pytest.raises(Exception, match="occupied"):
        supervise_service(
            record_path=record_path,
            kind="backend",
            port=8765,
            nonce="e" * 32,
            generation=9,
            command=[r"C:\LVT\python.exe", "-m", "lvt.main"],
            environment={"LVT_DATA_ROOT": r"C:\LVT\data"},
            cwd=r"C:\LVT",
            api=api,
            current_pid=700,
        )

    assert record_path.read_bytes() == b"foreign"
    assert ("terminate_job", "job", 125) in api.calls


def test_supervisor_listener_timeout_terminates_job_without_publishing_record(
    tmp_path: Path,
) -> None:
    api = FakeSupervisorApi()
    record_path = tmp_path / "backend.pid"

    with pytest.raises(Exception, match="listener did not become owned"):
        supervise_service(
            record_path=record_path,
            kind="backend",
            port=8765,
            nonce="f" * 32,
            generation=10,
            command=[r"C:\LVT\python.exe", "-m", "lvt.main"],
            environment={"LVT_DATA_ROOT": r"C:\LVT\data"},
            cwd=r"C:\LVT",
            api=api,
            current_pid=700,
            readiness_timeout=0,
        )

    assert not record_path.exists()
    assert ("terminate_job", "job", 125) in api.calls
