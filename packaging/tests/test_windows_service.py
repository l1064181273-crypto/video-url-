from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

from windows_process import ExecutableIdentity, ProcessIdentity  # noqa: E402
from windows_service import (  # noqa: E402
    WindowsServiceError,
    WindowsServiceRecord,
    owned_service_record_status,
    publish_service_record,
    retire_service_record,
    service_record_from_dict,
    stop_verified_service,
    verify_owned_service_record,
)


def _identity(pid: int, label: str) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        creation_time=1000 + pid,
        executable=ExecutableIdentity(
            path=rf"C:\LVT\{label}.exe",
            volume_serial=7,
            file_index=20 + pid,
            sha256=(f"{pid:064x}")[-64:],
        ),
    )


def _record() -> WindowsServiceRecord:
    return WindowsServiceRecord(
        kind="backend",
        port=8765,
        nonce="a" * 32,
        generation=4,
        job_name="LocalVideoTranscriber-backend-" + "a" * 32,
        supervisor=_identity(301, "supervisor"),
        service=_identity(302, "python"),
    )


class FakeServiceApi:
    def __init__(
        self,
        *,
        creation_overrides: dict[int, int] | None = None,
        listeners: set[int] | None = None,
        supervisor_exits: bool = True,
    ) -> None:
        self.identities = {
            301: _identity(301, "supervisor"),
            302: _identity(302, "python"),
        }
        self.creation_overrides = creation_overrides or {}
        self.listeners = {302} if listeners is None else listeners
        self.supervisor_exits = supervisor_exits
        self.calls: list[tuple[object, ...]] = []
        self.terminated_pids: set[int] = set()

    def open_process(self, pid: int, *, terminate: bool) -> object:
        handle = ("process", pid, terminate)
        self.calls.append(("open_process", pid, terminate))
        return handle

    def close_handle(self, handle: object) -> None:
        self.calls.append(("close", handle))

    def process_creation_time(self, handle: object) -> int:
        pid = handle[1]  # type: ignore[index]
        return self.creation_overrides.get(pid, self.identities[pid].creation_time)

    def process_image_path(self, handle: object) -> str:
        pid = handle[1]  # type: ignore[index]
        return self.identities[pid].executable.path

    def open_executable(self, path: str) -> object:
        return ("file", path)

    def file_identity(self, handle: object) -> tuple[int, int]:
        path = handle[1]  # type: ignore[index]
        identity = next(
            item.executable for item in self.identities.values() if item.executable.path == path
        )
        return identity.volume_serial, identity.file_index

    def sha256_file(self, handle: object) -> str:
        path = handle[1]  # type: ignore[index]
        return next(
            item.executable.sha256
            for item in self.identities.values()
            if item.executable.path == path
        )

    def terminate_process(self, handle: object, exit_code: int) -> None:
        self.calls.append(("terminate_process", handle, exit_code))
        self.terminated_pids.add(handle[1])  # type: ignore[index]

    def wait_process(self, handle: object, timeout_ms: int) -> bool:
        self.calls.append(("wait", handle, timeout_ms))
        pid = handle[1]  # type: ignore[index]
        return pid == 302 or self.supervisor_exits or pid in self.terminated_pids

    def listener_pids(self, port: int) -> set[int]:
        self.calls.append(("listeners", port))
        return set(self.listeners)

    def open_job(self, name: str) -> object:
        self.calls.append(("open_job", name))
        return ("job", name)

    def terminate_job(self, job: object, exit_code: int) -> None:
        self.calls.append(("terminate_job", job, exit_code))


def test_service_record_round_trips_strict_schema() -> None:
    record = _record()

    assert service_record_from_dict(record.as_dict()) == record


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema_version=2),
        lambda payload: payload.update(kind="unknown"),
        lambda payload: payload.update(port=11435),
        lambda payload: payload.update(generation=True),
        lambda payload: payload.update(nonce="../escape"),
        lambda payload: payload.update(job_name="Global\\foreign"),
        lambda payload: payload.update(extra="not allowed"),
    ],
)
def test_service_record_rejects_ambiguous_or_cross_kind_metadata(mutate: object) -> None:
    payload = _record().as_dict()
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(WindowsServiceError, match="record"):
        service_record_from_dict(payload)


def test_stop_verifies_both_identities_and_listener_before_terminating_job() -> None:
    api = FakeServiceApi()

    stop_verified_service(_record(), api)

    terminate_index = next(
        index for index, call in enumerate(api.calls) if call[0] == "terminate_job"
    )
    assert ("listeners", 8765) in api.calls[:terminate_index]
    assert ("open_process", 301, True) in api.calls[:terminate_index]
    assert ("open_process", 302, False) in api.calls[:terminate_index]
    assert not any(call[0] == "terminate_process" for call in api.calls)


def test_stop_fails_closed_on_pid_reuse_without_opening_job() -> None:
    api = FakeServiceApi(creation_overrides={302: 9999})

    with pytest.raises(WindowsServiceError, match="identity"):
        stop_verified_service(_record(), api)

    assert not any(call[0] == "open_job" for call in api.calls)
    assert not any(call[0].startswith("terminate") for call in api.calls)


def test_stop_fails_closed_on_foreign_listener() -> None:
    api = FakeServiceApi(listeners={999})

    with pytest.raises(WindowsServiceError, match="listener"):
        stop_verified_service(_record(), api)

    assert not any(call[0] == "open_job" for call in api.calls)
    assert not any(call[0].startswith("terminate") for call in api.calls)


def test_stop_uses_verified_supervisor_handle_if_supervisor_does_not_exit() -> None:
    api = FakeServiceApi(supervisor_exits=False)

    stop_verified_service(_record(), api)

    assert (
        "terminate_process",
        ("process", 301, True),
        143,
    ) in api.calls


def test_owned_service_record_requires_job_processes_and_exact_listener(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "backend.pid"
    record_path.write_text(
        json.dumps(_record().as_dict()),
        encoding="utf-8",
    )

    assert verify_owned_service_record(
        record_path,
        "backend",
        8765,
        api=FakeServiceApi(),
    )
    assert not verify_owned_service_record(
        record_path,
        "backend",
        8765,
        api=FakeServiceApi(listeners={999}),
    )
    assert (
        owned_service_record_status(
            record_path,
            "backend",
            8765,
            api=FakeServiceApi(listeners={999}),
        )
        == "listener_pid_mismatch"
    )


def test_owned_service_record_rejects_malformed_file(tmp_path: Path) -> None:
    record_path = tmp_path / "backend.pid"
    record_path.write_text("{broken", encoding="utf-8")

    assert not verify_owned_service_record(
        record_path,
        "backend",
        8765,
        api=FakeServiceApi(),
    )
    assert (
        owned_service_record_status(
            record_path,
            "backend",
            8765,
            api=FakeServiceApi(),
        )
        == "record_invalid"
    )


def test_service_record_publication_is_exclusive_and_leaves_no_staging(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "backend.pid"

    publish_service_record(record_path, _record())

    assert service_record_from_dict(json.loads(record_path.read_text(encoding="utf-8"))) == (
        _record()
    )
    assert not list(tmp_path.glob(".backend.pid.staged-*"))


def test_service_record_publication_never_overwrites_foreign_destination(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "backend.pid"
    record_path.write_bytes(b"foreign")
    before = (record_path.stat().st_ino, record_path.read_bytes())

    with pytest.raises(WindowsServiceError, match="occupied"):
        publish_service_record(record_path, _record())

    assert (record_path.stat().st_ino, record_path.read_bytes()) == before
    assert not list(tmp_path.glob(".backend.pid.staged-*"))


def test_service_record_retirement_preserves_record_in_history(tmp_path: Path) -> None:
    record_path = tmp_path / "backend.pid"
    record = _record()
    publish_service_record(record_path, record)

    retained = retire_service_record(record_path, record)

    assert not record_path.exists()
    assert retained.name == f"backend-{record.generation}-{record.nonce}.json"
    assert service_record_from_dict(json.loads(retained.read_text(encoding="utf-8"))) == record


def test_service_record_retirement_rejects_replaced_record(tmp_path: Path) -> None:
    record_path = tmp_path / "backend.pid"
    expected = _record()
    foreign = WindowsServiceRecord(
        **{
            **expected.__dict__,
            "nonce": "b" * 32,
            "job_name": "LocalVideoTranscriber-backend-" + "b" * 32,
        }
    )
    publish_service_record(record_path, foreign)

    with pytest.raises(WindowsServiceError, match="changed"):
        retire_service_record(record_path, expected)

    assert service_record_from_dict(json.loads(record_path.read_text(encoding="utf-8"))) == foreign
