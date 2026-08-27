from __future__ import annotations

import json
import os
import select
import signal
import sys
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging" / "tools"))
sys.path.insert(0, str(ROOT / "backend" / "src"))

import process_state  # noqa: E402
import publish_install  # noqa: E402
from lvt.api.app import create_app  # noqa: E402
from lvt.core.jobs import JobStatus  # noqa: E402
from lvt.core.processes import CancellationToken  # noqa: E402
from lvt.db.repository import JobRepository  # noqa: E402
from publish_install import (  # noqa: E402
    ActivationHandle,
    FirstInstallPublisher,
    PublishError,
    tree_identity,
)
from transaction_journal import TransactionJournal  # noqa: E402


class FakeServices:
    def __init__(self, *, runtime_ok: bool = True, health_ok: bool = True) -> None:
        self.runtime_ok = runtime_ok
        self.health_ok = health_ok
        self.calls: list[str] = []
        self.activate_call_count = 0

    def validate_candidate(self, phase: str) -> bool:
        self.calls.append(f"validate:{phase}")
        return True

    def start_precommit(self) -> object:
        self.calls.append("start:precommit")
        return object()

    def runtime_full(self) -> bool:
        self.calls.append("validate:runtime-full")
        return self.runtime_ok

    def activate(self, handle: object) -> None:
        self.calls.append("activate")
        self.activate_call_count += 1

    def healthy(self) -> bool:
        self.calls.append("health:normal")
        return self.health_ok

    def stop_candidate(self) -> None:
        self.calls.append("stop:candidate")

    def copy_token(self, token_path: Path) -> None:
        self.calls.append("copy:token")
        assert token_path.name == "api-token"


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.0"
    extension = release / "extension"
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        '{"manifest_version":3,"version":"0.1.0"}\n',
        encoding="utf-8",
    )
    (extension / "sidepanel.html").write_text("<main></main>\n", encoding="utf-8")
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    token = data_root / "config/api-token"
    token.parent.mkdir(parents=True)
    token.write_text("secret-never-rendered\n", encoding="ascii")
    token.chmod(0o600)
    (data_root / "runtime").mkdir()
    return data_root, release


def _publisher(
    tmp_path: Path,
    services: FakeServices,
    *,
    failpoint: Any = None,
) -> FirstInstallPublisher:
    data_root, release = _layout(tmp_path)
    return FirstInstallPublisher(
        data_root,
        release,
        services=services,
        failpoint=failpoint,
    )


def test_first_publish_has_strict_phase_switch_commit_activate_order(tmp_path: Path) -> None:
    services = FakeServices()
    publisher = _publisher(tmp_path, services)

    publisher.publish(lock_held=True)

    assert services.calls == [
        "validate:staging-core",
        "validate:dependencies",
        "start:precommit",
        "validate:runtime-full",
        "activate",
        "health:normal",
        "copy:token",
    ]
    assert publisher.current.is_symlink()
    assert publisher.current.resolve(strict=True) == publisher.release_root
    assert (publisher.extension / "manifest.json").is_file()
    assert publisher.journal.verify_critical("ACTIVATED")
    assert not publisher.current_previous.exists()
    assert not publisher.extension_previous.exists()


def test_runtime_failure_before_commit_restores_first_install_absence(tmp_path: Path) -> None:
    services = FakeServices(runtime_ok=False)
    publisher = _publisher(tmp_path, services)

    with pytest.raises(PublishError, match="runtime"):
        publisher.publish(lock_held=True)

    assert services.activate_call_count == 0
    assert services.calls[-1] == "stop:candidate"
    assert not publisher.current.exists() and not publisher.current.is_symlink()
    assert not publisher.extension.exists()


class SimulatedCrash(BaseException):
    pass


class NeverClaimPipeline:
    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return JobStatus.DOWNLOADING

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
        progress_callback: Any,
    ) -> None:
        raise AssertionError("precommit worker claimed before activation")


class ForkedPrecommitServices(FakeServices):
    def __init__(
        self,
        database: Path,
        backend_pid_path: Path,
        done_fd: int,
    ) -> None:
        super().__init__()
        self.database = database
        self.backend_pid_path = backend_pid_path
        self.done_fd = done_fd
        self.runtime_checked = False

    def start_precommit(self) -> object:
        activation_read, activation_write = os.pipe()
        ready_read, ready_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(activation_write)
                os.close(ready_read)
                app = create_app(
                    db_path=self.database,
                    api_token="test-only-token",
                    pipeline_builder=lambda _repository: NeverClaimPipeline(),
                    worker_poll_interval=60,
                    precommit_activation_fd=activation_read,
                )
                with TestClient(app):
                    os.write(ready_write, b"R")
                    os.close(ready_write)
                    if not app.state.activation_barrier.wait_closed(timeout=20):
                        os._exit(72)
                os.write(self.done_fd, b"D")
            finally:
                os._exit(0)
        os.close(activation_read)
        os.close(ready_write)
        self.backend_pid_path.write_text(str(pid), encoding="ascii")
        assert os.read(ready_read, 1) == b"R"
        os.close(ready_read)
        return ActivationHandle(activation_write)

    def runtime_full(self) -> bool:
        self.runtime_checked = True
        return True

    def activate(self, handle: object) -> None:
        assert isinstance(handle, ActivationHandle)
        os.write(handle.write_fd, b"A")
        os.close(handle.write_fd)
        handle.write_fd = -1
        handle.activated = True


@pytest.mark.parametrize("component", ["current", "extension"])
@pytest.mark.parametrize(
    "boundary",
    [
        "before_intent",
        "after_intent",
        "before_next_prepare",
        "after_next_prepare",
        "before_next_parent_fsync",
        "after_next_parent_fsync",
        "before_old_rename",
        "after_old_rename",
        "before_old_parent_fsync",
        "after_old_parent_fsync",
        "before_live_rename",
        "after_live_rename",
        "before_live_parent_fsync",
        "after_live_parent_fsync",
    ],
)
def test_switch_failpoints_recover_without_false_corruption(
    tmp_path: Path,
    component: str,
    boundary: str,
) -> None:
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if not fired and name == f"{component}:{boundary}":
            fired = True
            raise SimulatedCrash

    services = FakeServices()
    publisher = _publisher(tmp_path, services, failpoint=failpoint)
    with pytest.raises(SimulatedCrash):
        publisher.publish(lock_held=True)

    recovered = FirstInstallPublisher(
        publisher.data_root,
        publisher.release_root,
        services=FakeServices(),
    )
    recovered.reconcile(lock_held=True)
    recovered.reconcile(lock_held=True)

    assert not recovered.current.exists() and not recovered.current.is_symlink()
    assert not recovered.extension.exists()


@pytest.mark.parametrize(
    "boundary",
    ["before_next_file_fsync", "after_next_file_fsync"],
)
def test_extension_file_fsync_failpoints_recover_to_absence(
    tmp_path: Path,
    boundary: str,
) -> None:
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if not fired and name == f"extension:{boundary}":
            fired = True
            raise SimulatedCrash

    services = FakeServices()
    publisher = _publisher(tmp_path, services, failpoint=failpoint)
    with pytest.raises(SimulatedCrash):
        publisher.publish(lock_held=True)

    recovered = FirstInstallPublisher(
        publisher.data_root,
        publisher.release_root,
        services=FakeServices(),
    )
    recovered.reconcile(lock_held=True)
    assert not recovered.current.exists() and not recovered.current.is_symlink()
    assert not recovered.extension.exists()


@pytest.mark.parametrize(
    "journal_boundary",
    [
        "slot-a:before_temp_write",
        "slot-a:after_temp_write",
        "slot-a:before_file_fsync",
        "slot-a:after_file_fsync",
        "slot-a:before_slot_rename",
        "slot-a:after_slot_rename",
        "slot-a:before_directory_fsync",
        "slot-a:after_directory_fsync",
        "slot-b:before_temp_write",
        "slot-b:after_temp_write",
        "slot-b:before_file_fsync",
        "slot-b:after_file_fsync",
        "slot-b:before_slot_rename",
        "slot-b:after_slot_rename",
        "slot-b:before_directory_fsync",
        "slot-b:after_directory_fsync",
    ],
)
def test_commit_failpoints_never_activate_before_double_reopen(
    tmp_path: Path,
    journal_boundary: str,
) -> None:
    services = FakeServices()
    publisher = _publisher(tmp_path, services)
    original = publisher.journal
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if (
            not fired
            and "validate:runtime-full" in services.calls
            and "activate" not in services.calls
            and name == journal_boundary
        ):
            fired = True
            raise SimulatedCrash

    publisher.journal = TransactionJournal(original.root, failpoint=failpoint)
    with pytest.raises(SimulatedCrash):
        publisher.publish(lock_held=True)

    assert services.activate_call_count == 0
    direction = TransactionJournal(original.root).committed_direction()
    recovered_services = FakeServices()
    recovered = FirstInstallPublisher(
        publisher.data_root,
        publisher.release_root,
        services=recovered_services,
    )
    recovered.reconcile(lock_held=True)
    if direction == "committed":
        assert recovered_services.activate_call_count == 1
        assert recovered.current.resolve(strict=True) == recovered.release_root
        assert recovered.journal.verify_critical("ACTIVATED")
    else:
        assert recovered_services.activate_call_count == 0
        assert not recovered.current.exists() and not recovered.current.is_symlink()
        assert not recovered.extension.exists()


@pytest.mark.parametrize("damaged", ["slot-a.json", "slot-b.json"])
@pytest.mark.parametrize("damage", ["delete", "truncate"])
def test_single_committed_copy_is_repaired_before_exactly_one_activate(
    tmp_path: Path,
    damaged: str,
    damage: str,
) -> None:
    services = FakeServices()
    publisher = _publisher(tmp_path, services)
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    publisher.journal.write_critical({**payload, "state": "COMMITTED", "decision": "committed"})
    publisher.converge(committed=True)
    target = publisher.journal.root / damaged
    if damage == "delete":
        target.unlink()
    else:
        target.write_bytes(b"{")

    publisher.reconcile(lock_held=True)

    assert services.activate_call_count == 1
    assert publisher.journal.verify_critical("ACTIVATED")
    assert publisher.current.resolve(strict=True) == publisher.release_root
    assert tree_identity(publisher.extension) == payload["identities"]["extension"]["new"]


def test_conflicting_committed_slots_fail_closed_without_activate(tmp_path: Path) -> None:
    services = FakeServices()
    publisher = _publisher(tmp_path, services)
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    publisher.journal.write_critical({**payload, "state": "COMMITTED", "decision": "committed"})
    conflict = {
        **payload,
        "transaction_id": str(uuid.uuid4()),
        "decision_id": str(uuid.uuid4()),
        "state": "COMMITTED",
        "decision": "committed",
    }
    publisher.journal._write_slot(publisher.journal.root / "slot-b.json", 100, conflict)

    with pytest.raises(PublishError, match="conflicting"):
        publisher.reconcile(lock_held=True)
    assert services.activate_call_count == 0


@pytest.mark.parametrize(
    ("live", "next_value", "previous", "committed", "expected"),
    [
        ("old", "new", "absent", False, "old"),
        ("absent", "new", "old", False, "old"),
        ("new", "absent", "old", False, "old"),
        ("new", "absent", "absent", False, "absent"),
        ("old", "absent", "absent", False, "old"),
        ("absent", "absent", "old", False, "old"),
        ("old", "new", "absent", True, "new"),
        ("absent", "new", "old", True, "new"),
        ("new", "absent", "old", True, "new"),
        ("new", "absent", "absent", True, "new"),
        ("old", "absent", "absent", True, "new"),
        ("absent", "absent", "old", True, "new"),
        ("old", "new", "old", False, "old"),
        ("old", "new", "old", True, "new"),
    ],
)
def test_live_next_previous_matrix_converges_idempotently(
    tmp_path: Path,
    live: str,
    next_value: str,
    previous: str,
    committed: bool,
    expected: str,
) -> None:
    services = FakeServices()
    publisher = _publisher(tmp_path, services)
    old_release = publisher.data_root / "app/releases/old"
    old_release.mkdir()
    first_install_absence = (
        live == "new" and next_value == "absent" and previous == "absent" and not committed
    )
    payload = publisher.prepare_payload(
        old_current_target=None if first_install_absence else "releases/old",
        old_extension_identity=(
            {"kind": "absent"} if first_install_absence else {"kind": "tree", "sha256": "3" * 64}
        ),
    )
    old_extension = tmp_path / "old-extension"
    old_extension.mkdir()
    (old_extension / "manifest.json").write_text('{"version":"old"}\n', encoding="utf-8")
    if not first_install_absence:
        payload["identities"]["extension"]["old"] = tree_identity(old_extension)

    def put(path: Path, value: str, component: str) -> None:
        if value == "absent":
            return
        if component == "current":
            target = "releases/old" if value == "old" else "releases/0.1.0"
            path.symlink_to(target)
        else:
            source = old_extension if value == "old" else publisher.release_root / "extension"
            publisher.copy_tree(source, path)

    put(publisher.current, live, "current")
    put(publisher.current_next, next_value, "current")
    put(publisher.current_previous, previous, "current")
    put(publisher.extension, live, "extension")
    put(publisher.extension_next, next_value, "extension")
    put(publisher.extension_previous, previous, "extension")

    publisher.converge_payload(payload, committed=committed)
    first_snapshot = publisher.filesystem_snapshot()
    publisher.converge_payload(payload, committed=committed)

    assert publisher.filesystem_snapshot() == first_snapshot
    if expected == "absent":
        assert not publisher.current.exists() and not publisher.current.is_symlink()
        assert not publisher.extension.exists()
    elif expected == "old":
        assert os.readlink(publisher.current) == "releases/old"
        assert tree_identity(publisher.extension) == payload["identities"]["extension"]["old"]
    else:
        assert os.readlink(publisher.current) == "releases/0.1.0"
        assert tree_identity(publisher.extension) == payload["identities"]["extension"]["new"]


def test_journal_and_output_never_contain_token(tmp_path: Path) -> None:
    services = FakeServices()
    publisher = _publisher(tmp_path, services)
    secret = (publisher.data_root / "config/api-token").read_text(encoding="ascii").strip()

    publisher.publish(lock_held=True)

    transcript = "\n".join(
        path.read_text(encoding="utf-8") for path in publisher.journal.root.glob("slot-*.json")
    )
    assert secret not in transcript
    assert "api-token" not in transcript


def test_tree_identity_distinguishes_empty_file_from_empty_directory(
    tmp_path: Path,
) -> None:
    file_tree = tmp_path / "file-tree"
    directory_tree = tmp_path / "directory-tree"
    file_tree.mkdir()
    directory_tree.mkdir()
    (file_tree / "entry").touch(mode=0o755)
    (directory_tree / "entry").mkdir(mode=0o755)

    assert tree_identity(file_tree) != tree_identity(directory_tree)


def test_same_version_new_transaction_ignores_completed_previous_transaction(
    tmp_path: Path,
) -> None:
    first = _publisher(tmp_path, FakeServices())
    first.publish(lock_held=True)
    first_transaction = first.journal.read_latest()
    assert first_transaction is not None

    second_services = FakeServices()
    second = FirstInstallPublisher(
        first.data_root,
        first.release_root,
        services=second_services,
    )
    second.publish(lock_held=True)

    latest = second.journal.read_latest()
    assert latest is not None
    assert latest.payload["transaction_id"] != first_transaction.payload["transaction_id"]
    assert latest.payload["state"] == "ACTIVATED"
    assert latest.payload["substate"]["cleanup"] == "complete"
    assert second.current.resolve(strict=True) == second.release_root


@pytest.mark.parametrize(
    "boundary",
    [
        "cleanup:before_intent",
        "cleanup:after_intent",
        "cleanup:current:before_remove",
        "cleanup:current:after_remove",
        "cleanup:current:before_parent_fsync",
        "cleanup:current:after_parent_fsync",
        "cleanup:extension:before_remove",
        "cleanup:extension:after_remove",
        "cleanup:extension:before_parent_fsync",
        "cleanup:extension:after_parent_fsync",
        "cleanup:before_complete",
        "cleanup:after_complete",
    ],
)
def test_cleanup_crash_boundaries_preserve_direction_and_recover(
    tmp_path: Path,
    boundary: str,
) -> None:
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if not fired and name == boundary:
            fired = True
            raise SimulatedCrash

    services = FakeServices()
    publisher = _publisher(tmp_path, services, failpoint=failpoint)
    with pytest.raises(SimulatedCrash):
        publisher.publish(lock_held=True)

    journal = TransactionJournal(publisher.journal.root)
    assert journal.committed_direction() == "committed"
    recovered = FirstInstallPublisher(
        publisher.data_root,
        publisher.release_root,
        services=FakeServices(),
    )
    recovered.reconcile(lock_held=True)
    recovered.reconcile(lock_held=True)

    latest = recovered.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ACTIVATED"
    assert latest.payload["substate"]["cleanup"] == "complete"
    assert not recovered.current_previous.exists()
    assert not recovered.extension_previous.exists()


@pytest.mark.parametrize(
    ("component", "action"),
    [
        ("current", "remove_live"),
        ("current", "remove_next"),
        ("current", "remove_previous"),
        ("current", "rename_next_to_live"),
        ("current", "rename_previous_to_live"),
        ("extension", "rebuild_live"),
    ],
)
@pytest.mark.parametrize(
    "boundary",
    [
        "before_intent",
        "after_intent",
        "before_effect",
        "after_effect",
        "before_file_fsync",
        "after_file_fsync",
        "before_parent_fsync",
        "after_parent_fsync",
    ],
)
def test_recovery_actions_are_durable_and_idempotent_at_every_boundary(
    tmp_path: Path,
    component: str,
    action: str,
    boundary: str,
) -> None:
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if not fired and name == f"recovery:{component}:{action}:{boundary}":
            fired = True
            raise SimulatedCrash

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    old_release = publisher.data_root / "app/releases/old"
    old_release.mkdir()
    old_extension = tmp_path / "old-extension"
    old_extension.mkdir()
    (old_extension / "manifest.json").write_text('{"version":"old"}\n', encoding="utf-8")
    payload = publisher.prepare_payload(
        old_current_target="releases/old",
        old_extension_identity=tree_identity(old_extension),
    )
    publisher.journal.write_progress(payload)
    if component == "current":
        selected = {
            "remove_live": publisher.current,
            "remove_next": publisher.current_next,
            "remove_previous": publisher.current_previous,
            "rename_next_to_live": publisher.current_next,
            "rename_previous_to_live": publisher.current_previous,
        }[action]
        target = "releases/old" if "previous" in action else "releases/0.1.0"
        selected.symlink_to(target)

    with pytest.raises(SimulatedCrash):
        publisher._durable_recovery_action(payload, component, action)

    recovered = FirstInstallPublisher(
        publisher.data_root,
        publisher.release_root,
        services=FakeServices(),
    )
    latest = recovered.journal.read_latest()
    assert latest is not None
    if latest.payload["recovery"]["phase"] == "idle":
        recovered._durable_recovery_action(latest.payload, component, action)
    else:
        recovered._resume_recovery(latest.payload)

    final = recovered.journal.read_latest()
    assert final is not None
    assert final.payload["recovery"] == {
        "component": "none",
        "action": "none",
        "phase": "idle",
    }


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
@pytest.mark.parametrize(
    ("component", "action"),
    [
        ("current", "remove_live"),
        ("current", "remove_next"),
        ("current", "remove_previous"),
        ("current", "rename_next_to_live"),
        ("current", "rename_previous_to_live"),
        ("extension", "rebuild_live"),
    ],
)
@pytest.mark.parametrize(
    "boundary",
    [
        "before_intent",
        "after_intent",
        "before_effect",
        "after_effect",
        "before_file_fsync",
        "after_file_fsync",
        "before_parent_fsync",
        "after_parent_fsync",
    ],
)
def test_critical_recovery_actions_survive_sigkill_at_every_boundary(
    tmp_path: Path,
    component: str,
    action: str,
    boundary: str,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    old_release = publisher.data_root / "app/releases/old"
    old_release.mkdir()
    old_extension = tmp_path / "old-extension"
    old_extension.mkdir()
    (old_extension / "manifest.json").write_text(
        '{"version":"old"}\n',
        encoding="utf-8",
    )
    prepared = publisher.prepare_payload(
        old_current_target="releases/old",
        old_extension_identity=tree_identity(old_extension),
    )
    publisher.journal.write_progress(prepared)
    committed = {
        **prepared,
        "state": "COMMITTED",
        "decision": "committed",
    }
    publisher.journal.write_critical(committed)
    if component == "current":
        selected = {
            "remove_live": publisher.current,
            "remove_next": publisher.current_next,
            "remove_previous": publisher.current_previous,
            "rename_next_to_live": publisher.current_next,
            "rename_previous_to_live": publisher.current_previous,
        }[action]
        target = "releases/old" if "previous" in action else "releases/0.1.0"
        selected.symlink_to(target)

    marker_read, marker_write = os.pipe()
    gate_read, gate_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(marker_read)
            os.close(gate_write)

            def failpoint(name: str) -> None:
                if name == f"recovery:{component}:{action}:{boundary}":
                    os.write(marker_write, b"B")
                    os.read(gate_read, 1)

            child = FirstInstallPublisher(
                publisher.data_root,
                publisher.release_root,
                services=FakeServices(),
                failpoint=failpoint,
            )
            child._durable_recovery_action(committed, component, action)
        finally:
            os._exit(0)

    os.close(marker_write)
    os.close(gate_read)
    child_snapshot = process_state._snapshot(child_pid)
    try:
        assert child_snapshot is not None
        readable, _, _ = select.select([marker_read], [], [], 20)
        assert readable and os.read(marker_read, 1) == b"B"
        assert process_state._signal_snapshot(child_snapshot, signal.SIGKILL)
        found, status = os.waitpid(child_pid, 0)
        assert found == child_pid
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL

        recovered = FirstInstallPublisher(
            publisher.data_root,
            publisher.release_root,
            services=FakeServices(),
        )
        latest = recovered.journal.read_latest()
        assert latest is not None
        if latest.payload["recovery"]["phase"] == "idle":
            recovered._durable_recovery_action(latest.payload, component, action)
        else:
            recovered._resume_recovery(latest.payload)
        final = recovered.journal.read_latest()
        assert final is not None
        assert final.payload["recovery"] == {
            "component": "none",
            "action": "none",
            "phase": "idle",
        }
        assert recovered.journal.verify_critical("COMMITTED")
    finally:
        os.close(marker_read)
        os.close(gate_write)
        if child_snapshot is not None and process_state._token_is_live(child_snapshot):
            process_state._signal_snapshot(child_snapshot, signal.SIGKILL)
            with suppress(ChildProcessError):
                os.waitpid(child_pid, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
@pytest.mark.parametrize(
    "journal_boundary",
    [
        f"{slot}:{boundary}"
        for slot in ("slot-a", "slot-b")
        for boundary in (
            "before_temp_write",
            "after_temp_write",
            "before_file_fsync",
            "after_file_fsync",
            "before_slot_rename",
            "after_slot_rename",
            "before_directory_fsync",
            "after_directory_fsync",
        )
    ],
)
def test_real_publisher_sigkill_before_commit_never_releases_worker_barrier(
    tmp_path: Path,
    journal_boundary: str,
) -> None:
    data_root, release = _layout(tmp_path)
    database = data_root / "db/lvt.sqlite3"
    database.parent.mkdir()
    repository = JobRepository(database)
    repository.initialize()
    job_id = str(repository.create("https://example.test/precommit-kill")["uuid"])
    marker_read, marker_write = os.pipe()
    gate_read, gate_write = os.pipe()
    done_read, done_write = os.pipe()
    backend_pid_path = tmp_path / "backend.pid"
    publisher_pid = os.fork()
    if publisher_pid == 0:
        try:
            os.close(marker_read)
            os.close(gate_write)
            os.close(done_read)
            services = ForkedPrecommitServices(database, backend_pid_path, done_write)
            fired = False

            def failpoint(name: str) -> None:
                nonlocal fired
                if not fired and services.runtime_checked and name == journal_boundary:
                    fired = True
                    os.write(marker_write, b"B")
                    os.read(gate_read, 1)

            publisher = FirstInstallPublisher(
                data_root,
                release,
                services=services,
            )
            publisher.journal = TransactionJournal(
                publisher.journal.root,
                failpoint=failpoint,
            )
            publisher.publish(lock_held=True)
        finally:
            os._exit(0)

    os.close(marker_write)
    os.close(gate_read)
    os.close(done_write)
    publisher_snapshot = process_state._snapshot(publisher_pid)
    try:
        assert publisher_snapshot is not None
        assert os.read(marker_read, 1) == b"B"
        assert process_state._signal_snapshot(publisher_snapshot, signal.SIGKILL)
        found, status = os.waitpid(publisher_pid, 0)
        assert found == publisher_pid
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
        readable, _, _ = select.select([done_read], [], [], 20)
        assert readable and os.read(done_read, 1) == b"D"

        job = JobRepository(database).get(job_id)
        assert job is not None
        assert job["execution_count_total"] == 0
        assert all(
            event["status"] == JobStatus.QUEUED.value
            for event in JobRepository(database).list_events(job_id)
        )

        recovered_services = FakeServices()
        recovered = FirstInstallPublisher(
            data_root,
            release,
            services=recovered_services,
        )
        recovered.reconcile(lock_held=True)
        assert recovered.journal.read_latest() is not None
    finally:
        os.close(marker_read)
        os.close(gate_write)
        os.close(done_read)
        if publisher_snapshot is not None and process_state._token_is_live(publisher_snapshot):
            process_state._signal_snapshot(publisher_snapshot, signal.SIGKILL)
        if backend_pid_path.exists():
            backend_pid = int(backend_pid_path.read_text(encoding="ascii"))
            backend_snapshot = process_state._snapshot(backend_pid)
            if backend_snapshot is not None and process_state._token_is_live(backend_snapshot):
                process_state._signal_snapshot(backend_snapshot, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS audit tokens are required")
@pytest.mark.parametrize(
    "copy_boundary",
    [
        "extension-candidate:before_create",
        "extension-copy:after_first_file",
        "extension-copy:after_middle_file",
        "extension-copy:before_complete",
        "extension-candidate:before_tombstone_claim",
        "extension-candidate:after_tombstone_rename",
        "extension-candidate:before_tombstone_parent_fsync",
        "extension-candidate:after_tombstone_parent_fsync",
        "extension-candidate:after_tombstone_claim",
        "extension-candidate:before_deletion_claim",
        "extension-candidate:after_deletion_rename",
        "extension-candidate:before_deletion_parent_fsync",
        "extension-candidate:after_deletion_parent_fsync",
        "extension-candidate:after_deletion_claim",
        "extension-candidate:before_retained_quarantine",
        "extension-candidate:after_retained_quarantine",
    ],
)
def test_extension_staging_sigkill_never_leaves_partial_next(
    tmp_path: Path,
    copy_boundary: str,
) -> None:
    data_root, release = _layout(tmp_path)
    for index in range(6):
        (release / "extension" / f"asset-{index}.js").write_text(
            f"export const value = {index};\n",
            encoding="utf-8",
        )
    marker_read, marker_write = os.pipe()
    gate_read, gate_write = os.pipe()
    publisher_pid = os.fork()
    if publisher_pid == 0:
        try:
            os.close(marker_read)
            os.close(gate_write)

            def failpoint(name: str) -> None:
                if name == copy_boundary:
                    os.write(marker_write, b"B")
                    os.read(gate_read, 1)

            FirstInstallPublisher(
                data_root,
                release,
                services=FakeServices(),
                failpoint=failpoint,
            ).publish(lock_held=True)
        finally:
            os._exit(0)

    os.close(marker_write)
    os.close(gate_read)
    publisher_snapshot = process_state._snapshot(publisher_pid)
    try:
        assert publisher_snapshot is not None
        readable, _, _ = select.select([marker_read], [], [], 10)
        assert readable and os.read(marker_read, 1) == b"B"
        assert process_state._signal_snapshot(publisher_snapshot, signal.SIGKILL)
        found, status = os.waitpid(publisher_pid, 0)
        assert found == publisher_pid
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL

        recovered = FirstInstallPublisher(
            data_root,
            release,
            services=FakeServices(),
        )
        recovered.reconcile(lock_held=True)
        recovered.reconcile(lock_held=True)

        assert not recovered.current.exists() and not recovered.current.is_symlink()
        assert not recovered.extension.exists()
        assert not recovered.extension_next.exists()
        assert not list(data_root.glob("extension.next.candidate-*"))
        assert not list(data_root.glob("extension.next.candidate-*.owner"))
        assert not list(data_root.glob(".extension.next.bootstrap-*"))
        assert not list(data_root.glob(".extension.next.tombstone-*"))
        retained = list(data_root.glob(".extension.next.deleting-*"))
        assert len(retained) == 1
        assert recovered._extension_candidate_is_owned(
            recovered.journal.read_latest().payload,  # type: ignore[union-attr]
            candidate=retained[0],
        )
    finally:
        os.close(marker_read)
        os.close(gate_write)
        if publisher_snapshot is not None and process_state._token_is_live(publisher_snapshot):
            assert process_state._signal_snapshot(publisher_snapshot, signal.SIGKILL)
            with suppress(ChildProcessError):
                os.waitpid(publisher_pid, 0)


@pytest.mark.parametrize("candidate_kind", ["foreign_name", "missing_owner"])
def test_unknown_extension_staging_candidate_fails_closed(
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    foreign = (
        publisher.data_root / "extension.next.candidate-foreign"
        if candidate_kind == "foreign_name"
        else publisher._extension_candidate_paths(payload)[0]
    )
    foreign.mkdir()
    (foreign / "untrusted").write_text("external\n", encoding="utf-8")

    with pytest.raises(PublishError, match="extension staging candidate"):
        publisher.reconcile(lock_held=True)

    assert foreign.is_dir()
    assert (foreign / "untrusted").read_text(encoding="utf-8") == "external\n"


def test_extension_candidate_claim_race_preserves_foreign_directory(
    tmp_path: Path,
) -> None:
    namespace_checked = threading.Barrier(2)
    release_claim = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "extension-candidate:before_create":
            namespace_checked.wait(timeout=5)
            release_claim.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    candidate = publisher._extension_candidate_paths(payload)[0]

    def stage() -> None:
        try:
            publisher._stage_extension_next(payload)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=stage)
    worker.start()
    namespace_checked.wait(timeout=5)
    candidate.mkdir()
    foreign = candidate / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")
    release_claim.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    assert foreign.read_text(encoding="utf-8") == "external\n"
    assert not (candidate / ".owner.json").exists()


@pytest.mark.parametrize("field", ["transaction_nonce", "device", "inode"])
def test_extension_candidate_recovery_rejects_mismatched_owner_identity(
    tmp_path: Path,
    field: str,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    candidate = publisher._extension_candidate_paths(payload)[0]
    candidate.mkdir(mode=0o700)
    publisher._write_extension_candidate_owner(candidate, payload)
    foreign = candidate / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")
    owner = candidate / ".owner.json"
    marker = json.loads(owner.read_text(encoding="ascii"))
    marker[field] = "wrong" if field == "transaction_nonce" else marker[field] + 1
    owner.write_text(json.dumps(marker), encoding="ascii")
    owner.chmod(0o600)

    with pytest.raises(PublishError, match="ownership is unverified"):
        publisher.reconcile(lock_held=True)

    assert foreign.read_text(encoding="utf-8") == "external\n"


def test_extension_candidate_removal_claim_preserves_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    candidate = publisher._extension_candidate_paths(payload)[0]
    displaced = publisher.data_root / "displaced-owned-candidate"
    replacement = publisher.data_root / "replacement"
    candidate.mkdir(mode=0o700)
    publisher._write_extension_candidate_owner(candidate, payload)
    (candidate / "owned").write_text("owned\n", encoding="utf-8")
    replacement.mkdir()
    foreign = replacement / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")
    original_rename = publish_install._rename_directory_exclusive

    def replace_before_claim(source: Path, destination: Path) -> None:
        if source == candidate:
            candidate.rename(displaced)
            replacement.rename(candidate)
        original_rename(source, destination)

    monkeypatch.setattr(
        publish_install,
        "_rename_directory_exclusive",
        replace_before_claim,
    )

    with pytest.raises(PublishError, match="ownership changed during tombstone claim"):
        publisher._remove_owned_extension_candidate(payload)

    retained_foreign = publisher._extension_tombstone_path(payload)
    assert retained_foreign.is_dir()
    assert (retained_foreign / "untrusted").read_text(encoding="utf-8") == "external\n"
    assert displaced.is_dir()
    assert (displaced / "owned").read_text(encoding="utf-8") == "owned\n"


def test_retained_quarantine_never_deletes_claimed_contents(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    candidate = publisher._extension_candidate_paths(payload)[0]
    candidate.mkdir(mode=0o700)
    publisher._write_extension_candidate_owner(candidate, payload)
    nested = candidate / "payload/nested"
    nested.mkdir(parents=True)
    content = nested / "asset.js"
    content.write_text("external-inode-must-survive\n", encoding="utf-8")
    before = content.stat()

    publisher._remove_owned_extension_candidate(payload)

    retained = publisher._extension_deletion_path(payload)
    preserved = retained / "payload/nested/asset.js"
    after = preserved.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert preserved.read_text(encoding="utf-8") == "external-inode-must-survive\n"
    assert (retained / ".owner.json").is_file()


def test_post_claim_name_swap_preserves_foreign_inode_and_content(tmp_path: Path) -> None:
    claimed = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "extension-candidate:after_deletion_claim":
            claimed.wait(timeout=5)
            resume.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    candidate = publisher._extension_candidate_paths(payload)[0]
    deletion = publisher._extension_deletion_path(payload)
    displaced = publisher.data_root / "displaced-retained-quarantine"
    replacement = publisher.data_root / "foreign-replacement"
    candidate.mkdir(mode=0o700)
    publisher._write_extension_candidate_owner(candidate, payload)
    (candidate / "owned").write_text("owned\n", encoding="utf-8")
    replacement.mkdir()
    foreign = replacement / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")
    foreign_before = foreign.stat()

    def remove() -> None:
        try:
            publisher._remove_owned_extension_candidate(payload)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=remove)
    worker.start()
    claimed.wait(timeout=5)
    deletion.rename(displaced)
    replacement.rename(deletion)
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    preserved = deletion / "untrusted"
    foreign_after = preserved.stat()
    assert (foreign_after.st_dev, foreign_after.st_ino) == (
        foreign_before.st_dev,
        foreign_before.st_ino,
    )
    assert preserved.read_text(encoding="utf-8") == "external\n"
    assert (displaced / "owned").read_text(encoding="utf-8") == "owned\n"


def test_stale_activation_handle_cannot_stop_second_service_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GenerationOperations:
        current: dict[str, str] = {}
        generation = 0
        signals: list[str] = []

        def __init__(self, _data_root: Path, _release_root: Path) -> None:
            pass

        def state(self, kind: str) -> str:
            return "owned" if kind in self.current else "absent"

        def launch(self, kind: str, _activation_fd: int | None = None) -> str:
            self.__class__.generation += 1
            identity = f"{kind}-{self.generation}"
            self.current[kind] = identity
            return identity

        def backend_healthy(self) -> bool:
            return True

        def stop(self, kind: str) -> None:
            self.signals.append(self.current.pop(kind))

        def stop_matching(self, kind: str, identity: object) -> None:
            if self.current.get(kind) != identity:
                raise process_state.ServiceError(f"{kind} generation changed")
            self.signals.append(self.current.pop(kind))

    monkeypatch.setattr(
        process_state,
        "SystemServiceOperations",
        GenerationOperations,
    )
    data_root, release = _layout(tmp_path)
    first = publish_install.SystemPublicationServices(data_root, release)
    first.start_precommit()
    GenerationOperations.current.clear()

    second = publish_install.SystemPublicationServices(data_root, release)
    second.start_precommit()
    second_generation = dict(GenerationOperations.current)

    with pytest.raises(ExceptionGroup, match="candidate cleanup failed"):
        first.stop_candidate()

    assert GenerationOperations.current == second_generation
    assert GenerationOperations.signals == []

    second.stop_candidate()
    signals_after_cleanup = list(GenerationOperations.signals)
    second.stop_candidate()
    assert GenerationOperations.signals == signals_after_cleanup
