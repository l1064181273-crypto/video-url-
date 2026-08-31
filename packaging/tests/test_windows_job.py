from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

from windows_job import (  # noqa: E402
    WindowsJobError,
    launch_suspended_in_job,
)


class FakeWindowsJobApi:
    def __init__(self, *, fail_assign: bool = False) -> None:
        self.fail_assign = fail_assign
        self.calls: list[tuple[object, ...]] = []

    def create_kill_on_close_job(self, name: str) -> object:
        self.calls.append(("create_job", name))
        return "job-handle"

    def create_process_suspended(
        self,
        command: list[str],
        environment: dict[str, str],
        cwd: str,
    ) -> tuple[object, object, int]:
        self.calls.append(("create_process", tuple(command), environment, cwd))
        return "process-handle", "thread-handle", 731

    def assign_process_to_job(self, job: object, process: object) -> None:
        self.calls.append(("assign", job, process))
        if self.fail_assign:
            raise OSError("assignment failed")

    def resume_thread(self, thread: object) -> None:
        self.calls.append(("resume", thread))

    def terminate_process(self, process: object, exit_code: int) -> None:
        self.calls.append(("terminate_process", process, exit_code))

    def terminate_job(self, job: object, exit_code: int) -> None:
        self.calls.append(("terminate_job", job, exit_code))

    def process_in_job(self, process: object, job: object) -> bool:
        self.calls.append(("process_in_job", process, job))
        return True

    def close_handle(self, handle: object) -> None:
        self.calls.append(("close", handle))


def test_process_is_assigned_to_kill_on_close_job_before_resume() -> None:
    api = FakeWindowsJobApi()

    launched = launch_suspended_in_job(
        ["python.exe", "-m", "lvt.main"],
        {"LVT_DATA_ROOT": r"C:\Users\test\AppData\Local\LocalVideoTranscriber"},
        r"C:\LVT",
        "LocalVideoTranscriber-backend-a" + "1" * 31,
        api,
    )

    assert launched.pid == 731
    assert api.calls[:4] == [
        ("create_job", "LocalVideoTranscriber-backend-a" + "1" * 31),
        (
            "create_process",
            ("python.exe", "-m", "lvt.main"),
            {"LVT_DATA_ROOT": r"C:\Users\test\AppData\Local\LocalVideoTranscriber"},
            r"C:\LVT",
        ),
        ("assign", "job-handle", "process-handle"),
        ("resume", "thread-handle"),
    ]
    assert ("close", "thread-handle") in api.calls
    assert ("close", "process-handle") not in api.calls
    assert ("close", "job-handle") not in api.calls

    launched.close()

    assert api.calls[-2:] == [
        ("close", "process-handle"),
        ("close", "job-handle"),
    ]


def test_assignment_failure_terminates_suspended_process_and_closes_all_handles() -> None:
    api = FakeWindowsJobApi(fail_assign=True)

    with pytest.raises(WindowsJobError, match="launch failed"):
        launch_suspended_in_job(
            ["ollama.exe", "serve"],
            {"OLLAMA_HOST": "127.0.0.1:11435"},
            r"C:\LVT",
            "LocalVideoTranscriber-ollama-b" + "2" * 31,
            api,
        )

    assert ("resume", "thread-handle") not in api.calls
    assert ("terminate_process", "process-handle", 125) in api.calls
    assert api.calls[-3:] == [
        ("close", "thread-handle"),
        ("close", "process-handle"),
        ("close", "job-handle"),
    ]


def test_job_termination_targets_job_handle_not_pid() -> None:
    api = FakeWindowsJobApi()
    launched = launch_suspended_in_job(
        ["python.exe", "-m", "lvt.main"],
        {},
        r"C:\LVT",
        "LocalVideoTranscriber-backend-c" + "3" * 31,
        api,
    )

    launched.terminate(exit_code=143)
    launched.close()

    assert ("terminate_job", "job-handle", 143) in api.calls
