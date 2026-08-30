from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

from windows_process import (  # noqa: E402
    ExecutableIdentity,
    ProcessIdentity,
    WindowsProcessError,
    capture_process_identity,
    open_verified_process,
    owned_listener_matches,
    process_identity_from_dict,
    safe_terminate,
)


class FakeWindowsProcessApi:
    def __init__(
        self,
        *,
        creation_time: int = 100,
        image_path: str = r"C:\LVT\python.exe",
        file_identity: tuple[int, int] = (7, 11),
        digest: str = "a" * 64,
        listeners: set[int] | None = None,
    ) -> None:
        self.creation_time = creation_time
        self.image_path = image_path
        self.executable_file_identity = file_identity
        self.digest = digest
        self.listeners = {8765: {321}} if listeners is None else {8765: listeners}
        self.closed: list[object] = []
        self.terminated: list[tuple[object, int]] = []
        self.waited: list[tuple[object, int]] = []

    def open_process(self, pid: int, *, terminate: bool) -> object:
        assert pid == 321
        return ("process", pid, terminate)

    def close_handle(self, handle: object) -> None:
        self.closed.append(handle)

    def process_creation_time(self, handle: object) -> int:
        return self.creation_time

    def process_image_path(self, handle: object) -> str:
        return self.image_path

    def open_executable(self, path: str) -> object:
        assert path == self.image_path
        return ("file", path)

    def file_identity(self, handle: object) -> tuple[int, int]:
        return self.executable_file_identity

    def sha256_file(self, handle: object) -> str:
        return self.digest

    def terminate_process(self, handle: object, exit_code: int) -> None:
        self.terminated.append((handle, exit_code))

    def wait_process(self, handle: object, timeout_ms: int) -> bool:
        self.waited.append((handle, timeout_ms))
        return True

    def listener_pids(self, port: int) -> set[int]:
        return set(self.listeners.get(port, set()))


def _identity() -> ProcessIdentity:
    return ProcessIdentity(
        pid=321,
        creation_time=100,
        executable=ExecutableIdentity(
            path=r"C:\LVT\python.exe",
            volume_serial=7,
            file_index=11,
            sha256="a" * 64,
        ),
    )


def test_open_verified_process_holds_process_and_executable_handles() -> None:
    api = FakeWindowsProcessApi()

    with open_verified_process(_identity(), api, terminate=True) as opened:
        assert opened.process_handle == ("process", 321, True)
        assert opened.executable_handle == ("file", r"C:\LVT\python.exe")
        assert api.closed == []

    assert api.closed == [
        ("file", r"C:\LVT\python.exe"),
        ("process", 321, True),
    ]


def test_capture_process_identity_uses_existing_process_handle() -> None:
    api = FakeWindowsProcessApi()

    identity = capture_process_identity(321, "existing-process-handle", api)

    assert identity == _identity()
    assert api.closed == [("file", r"C:\LVT\python.exe")]


def test_pid_reuse_fails_before_opening_executable_or_terminating() -> None:
    api = FakeWindowsProcessApi(creation_time=101)

    with pytest.raises(WindowsProcessError, match="creation time"):
        safe_terminate(_identity(), api)

    assert api.terminated == []
    assert api.closed == [("process", 321, True)]


def test_executable_file_replacement_fails_closed() -> None:
    api = FakeWindowsProcessApi(file_identity=(7, 12))

    with pytest.raises(WindowsProcessError, match="executable identity"):
        safe_terminate(_identity(), api)

    assert api.terminated == []
    assert api.closed == [
        ("file", r"C:\LVT\python.exe"),
        ("process", 321, True),
    ]


def test_safe_terminate_targets_verified_handle_and_waits() -> None:
    api = FakeWindowsProcessApi()

    safe_terminate(_identity(), api, timeout_ms=4321)

    assert api.terminated == [(("process", 321, True), 143)]
    assert api.waited == [(("process", 321, True), 4321)]


def test_listener_ownership_requires_exact_verified_pid() -> None:
    assert owned_listener_matches(_identity(), 8765, FakeWindowsProcessApi())
    assert not owned_listener_matches(
        _identity(),
        8765,
        FakeWindowsProcessApi(listeners={999}),
    )


def test_listener_query_failure_is_not_treated_as_owned() -> None:
    class FailedListenerApi(FakeWindowsProcessApi):
        def listener_pids(self, port: int) -> set[int]:
            raise OSError("query failed")

    with pytest.raises(WindowsProcessError, match="listener"):
        owned_listener_matches(_identity(), 8765, FailedListenerApi())


def test_process_identity_round_trips_through_strict_record_schema() -> None:
    identity = _identity()

    assert process_identity_from_dict(identity.as_dict()) == identity


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(pid=True),
        lambda payload: payload.update(creation_time=0),
        lambda payload: payload.update(extra="not allowed"),
        lambda payload: payload["executable"].update(path=r"\\server\share\python.exe"),
        lambda payload: payload["executable"].update(file_index=-1),
        lambda payload: payload["executable"].update(sha256="not-a-digest"),
    ],
)
def test_process_identity_parser_rejects_ambiguous_or_unsafe_records(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _identity().as_dict()
    mutate(payload)

    with pytest.raises(WindowsProcessError, match="identity"):
        process_identity_from_dict(payload)
