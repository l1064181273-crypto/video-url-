from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECONCILE = ROOT / "packaging/tools/reconcile_processes.py"
SUPERVISOR = ROOT / "packaging/tools/tool_supervisor.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("lvt_reconcile_processes", RECONCILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_supervisor() -> Any:
    spec = importlib.util.spec_from_file_location("lvt_tool_supervisor", SUPERVISOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _identity(path: Path) -> dict[str, object]:
    metadata = path.stat()
    return {
        "realpath": str(path.resolve()),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _record(tmp_path: Path) -> tuple[Path, dict[str, Any], Path, Path]:
    supervisor = tmp_path / "supervisor"
    tool = tmp_path / "tool"
    supervisor.write_bytes(b"supervisor")
    tool.write_bytes(b"tool")
    run_id = "11111111-1111-4111-8111-111111111111"
    record = {
        "schema_version": 1,
        "job_id": "22222222-2222-4222-8222-222222222222",
        "run_id": run_id,
        "kind": "ffmpeg",
        "ownership_nonce": "a" * 32,
        "created_at": "2026-01-01T00:00:00+00:00",
        "lifecycle_state": "running",
        "supervisor": {
            "pid": 41001,
            "pgid": 41001,
            "start_time": "supervisor-start",
            "executable": _identity(supervisor),
        },
        "tool": {
            "pid": 41002,
            "pgid": 41002,
            "start_time": "tool-start",
            "executable": _identity(tool),
        },
    }
    record_path = tmp_path / "processes" / run_id / "ffmpeg.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    record_path.chmod(0o600)
    return record_path, record, supervisor, tool


class _Inspector:
    def __init__(self, module: Any, record: dict[str, Any], supervisor: Path, tool: Path) -> None:
        self.module = module
        self.snapshots = {
            41001: module.ProcessSnapshot(
                pid=41001,
                pgid=41001,
                start_time="supervisor-start",
                executable=supervisor.resolve(),
                ownership_nonce="a" * 32,
            ),
            41002: module.ProcessSnapshot(
                pid=41002,
                pgid=41002,
                start_time="tool-start",
                executable=tool.resolve(),
            ),
        }
        self.groups = {41002}
        self.record = record

    def snapshot(self, pid: int) -> Any:
        return self.snapshots.get(pid)

    def group_exists(self, pgid: int) -> bool:
        return pgid in self.groups


class _Signaller:
    def __init__(self, inspector: _Inspector, *, stop_on_term: bool = True) -> None:
        self.inspector = inspector
        self.stop_on_term = stop_on_term
        self.calls: list[tuple[str, int, signal.Signals]] = []

    def signal_process(self, pid: int, requested: signal.Signals) -> None:
        self.calls.append(("pid", pid, requested))
        if requested is signal.SIGTERM and self.stop_on_term:
            self.inspector.snapshots.clear()
            self.inspector.groups.clear()
        elif requested is signal.SIGKILL:
            self.inspector.snapshots.pop(pid, None)

    def signal_group(self, pgid: int, requested: signal.Signals) -> None:
        self.calls.append(("pgid", pgid, requested))
        if requested is signal.SIGKILL:
            self.inspector.groups.discard(pgid)
            self.inspector.snapshots.pop(41002, None)


def test_complete_ownership_is_stopped_and_record_removed(tmp_path: Path) -> None:
    module = _load_module()
    record_path, record, supervisor, tool = _record(tmp_path)
    inspector = _Inspector(module, record, supervisor, tool)
    signaller = _Signaller(inspector)

    report = module.reconcile_process_records(
        record_path.parents[1],
        inspector=inspector,
        signaller=signaller,
        terminate_grace=0.01,
        kill_wait=0.01,
        poll_interval=0.001,
    )

    assert report.status == "healthy"
    assert report.cleaned == 1
    assert signaller.calls == [("pid", 41001, signal.SIGTERM)]
    assert not record_path.exists()


@pytest.mark.parametrize(
    ("mutation", "target"),
    [
        ("supervisor_start", "supervisor"),
        ("supervisor_pgid", "supervisor"),
        ("tool_start", "tool"),
        ("tool_pgid", "tool"),
        ("tool_inode", "tool"),
        ("tool_hash", "tool"),
        ("nonce", "record"),
        ("missing_tool", "inspector"),
    ],
)
def test_incomplete_or_reused_ownership_quarantines_with_zero_signal(
    tmp_path: Path,
    mutation: str,
    target: str,
) -> None:
    module = _load_module()
    record_path, record, supervisor, tool = _record(tmp_path)
    inspector = _Inspector(module, record, supervisor, tool)
    if mutation == "supervisor_start":
        inspector.snapshots[41001] = replace(
            inspector.snapshots[41001],
            start_time="reused-supervisor",
        )
    elif mutation == "supervisor_pgid":
        inspector.snapshots[41001] = replace(inspector.snapshots[41001], pgid=999)
    elif mutation == "tool_start":
        inspector.snapshots[41002] = replace(inspector.snapshots[41002], start_time="reused-tool")
    elif mutation == "tool_pgid":
        inspector.snapshots[41002] = replace(inspector.snapshots[41002], pgid=999)
    elif mutation == "tool_inode":
        record["tool"]["executable"]["inode"] += 1
    elif mutation == "tool_hash":
        record["tool"]["executable"]["sha256"] = "b" * 64
    elif mutation == "nonce":
        record["ownership_nonce"] = "b" * 32
    elif mutation == "missing_tool":
        inspector.snapshots.pop(41002)
    else:
        raise AssertionError(target)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    signaller = _Signaller(inspector)

    report = module.reconcile_process_records(
        record_path.parents[1],
        inspector=inspector,
        signaller=signaller,
        terminate_grace=0.01,
        kill_wait=0.01,
        poll_interval=0.001,
    )

    assert report.status == "unsafe"
    assert report.code == "PROCESS_OWNERSHIP_UNVERIFIED"
    assert signaller.calls == []
    assert not record_path.exists()
    assert len(list((record_path.parents[1] / "quarantine").iterdir())) == 1


def test_pid_reuse_after_term_prevents_killpg(tmp_path: Path) -> None:
    module = _load_module()
    record_path, record, supervisor, tool = _record(tmp_path)
    inspector = _Inspector(module, record, supervisor, tool)

    class ReusingSignaller(_Signaller):
        def signal_process(self, pid: int, requested: signal.Signals) -> None:
            super().signal_process(pid, requested)
            if requested is signal.SIGTERM:
                self.inspector.snapshots[41001] = replace(
                    self.inspector.snapshots[41001],
                    start_time="reused-after-term",
                )

    signaller = ReusingSignaller(inspector, stop_on_term=False)

    report = module.reconcile_process_records(
        record_path.parents[1],
        inspector=inspector,
        signaller=signaller,
        terminate_grace=0.001,
        kill_wait=0.001,
        poll_interval=0.001,
    )

    assert report.status == "unsafe"
    assert signaller.calls == [("pid", 41001, signal.SIGTERM)]
    assert all(call[0] != "pgid" for call in signaller.calls)


def test_tool_group_kill_occurs_only_after_second_full_verification(tmp_path: Path) -> None:
    module = _load_module()
    record_path, record, supervisor, tool = _record(tmp_path)
    inspector = _Inspector(module, record, supervisor, tool)
    signaller = _Signaller(inspector, stop_on_term=False)

    report = module.reconcile_process_records(
        record_path.parents[1],
        inspector=inspector,
        signaller=signaller,
        terminate_grace=0.001,
        kill_wait=0.001,
        poll_interval=0.001,
    )

    assert report.status == "healthy"
    assert signaller.calls == [
        ("pid", 41001, signal.SIGTERM),
        ("pgid", 41002, signal.SIGKILL),
        ("pid", 41001, signal.SIGKILL),
    ]


def test_completed_record_is_removed_without_signal(tmp_path: Path) -> None:
    module = _load_module()
    record_path, record, supervisor, tool = _record(tmp_path)
    record["lifecycle_state"] = "completed"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    inspector = _Inspector(module, record, supervisor, tool)
    signaller = _Signaller(inspector)

    report = module.reconcile_process_records(
        record_path.parents[1],
        inspector=inspector,
        signaller=signaller,
    )

    assert report.status == "healthy"
    assert signaller.calls == []
    assert not record_path.exists()


def test_record_publish_fsyncs_file_before_rename_and_directory_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_supervisor()
    record = tmp_path / "run" / "ffmpeg.json"
    record.parent.mkdir()
    events: list[str] = []
    real_fsync = module.os.fsync
    real_replace = module.os.replace

    def observed_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("dir-fsync" if stat.S_ISDIR(mode) else "file-fsync")
        real_fsync(descriptor)

    def observed_replace(source: Path, destination: Path) -> None:
        events.append("rename")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "fsync", observed_fsync)
    monkeypatch.setattr(module.os, "replace", observed_replace)

    module._write_record(record, {"schema_version": 1})

    assert events == ["file-fsync", "rename", "dir-fsync"]
    assert record.stat().st_mode & 0o777 == 0o600


def test_launch_gate_eof_before_record_publish_never_execs_tool(tmp_path: Path) -> None:
    module = _load_supervisor()
    marker = tmp_path / "tool-executed"
    control_read, control_write = os.pipe()
    ready_read, ready_write = os.pipe()
    pid, _pgid, gate_write = module._fork_tool(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('bad')",
            str(marker),
        ],
        control_read,
        ready_write,
    )
    os.close(gate_write)
    _, status = os.waitpid(pid, 0)
    for descriptor in (control_read, control_write, ready_read, ready_write):
        os.close(descriptor)

    assert os.waitstatus_to_exitcode(status) != 0
    assert not marker.exists()


def test_symlinked_run_record_is_unsafe_with_zero_signal(tmp_path: Path) -> None:
    module = _load_module()
    process_root = tmp_path / "processes"
    process_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (process_root / "11111111-1111-4111-8111-111111111111").symlink_to(
        outside,
        target_is_directory=True,
    )

    class EmptyInspector:
        def snapshot(self, _pid: int) -> None:
            return None

        def group_exists(self, _pgid: int) -> bool:
            return False

    class NoSignal:
        def signal_process(self, _pid: int, _requested: signal.Signals) -> None:
            raise AssertionError("unverified record sent a process signal")

        def signal_group(self, _pgid: int, _requested: signal.Signals) -> None:
            raise AssertionError("unverified record sent a group signal")

    report = module.reconcile_process_records(
        process_root,
        inspector=EmptyInspector(),
        signaller=NoSignal(),
    )

    assert report.status == "unsafe"
    assert report.unverified == 1
    assert not (process_root / "11111111-1111-4111-8111-111111111111").exists()
    assert len(list((process_root / "quarantine").iterdir())) == 1
