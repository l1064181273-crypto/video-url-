from __future__ import annotations

import json
import os
import select
import signal
import stat
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
import verify_install  # noqa: E402
from lvt.api.app import create_app  # noqa: E402
from lvt.core.jobs import JobStatus  # noqa: E402
from lvt.core.processes import CancellationToken  # noqa: E402
from lvt.db.repository import JobRepository  # noqa: E402
from publish_install import (  # noqa: E402
    ActivationHandle,
    FirstInstallPublisher,
    PublishError,
    path_identity,
    tree_identity,
)
from transaction_journal import JournalError, TransactionJournal  # noqa: E402


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


class VerifyContractServices(publish_install.SystemPublicationServices):
    def __init__(self, data_root: Path, release_root: Path) -> None:
        super().__init__(data_root, release_root)
        self.calls: list[str] = []

    def _validate(self, phase: str, release: Path) -> bool:
        self.calls.append(f"validate:{phase}")
        if phase != "runtime-full":
            return True
        current, current_checks = verify_install._resolve_current_release(
            self.data_root,
            release,
        )
        if current is None or any(
            check.status is not verify_install.CheckStatus.OK for check in current_checks
        ):
            return False
        extension = verify_install._validate_stable_extension(self.data_root, current)
        return extension.status is verify_install.CheckStatus.OK

    def start_precommit(self) -> object:
        self.calls.append("start:precommit")
        return object()

    def activate(self, handle: object) -> None:
        self.calls.append("activate")

    def healthy(self) -> bool:
        self.calls.append("health:normal")
        return True

    def stop_candidate(self) -> None:
        self.calls.append("stop:candidate")

    def copy_token(self, token_path: Path) -> None:
        self.calls.append("copy:token")


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


def _additional_release(data_root: Path, version: str) -> Path:
    release = data_root / f"app/releases/{version}"
    extension = release / "extension"
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": version}) + "\n",
        encoding="utf-8",
    )
    (extension / "sidepanel.html").write_text(
        f"<main>{version}</main>\n",
        encoding="utf-8",
    )
    (extension / "release.js").write_text(
        f"export const version = {version!r};\n",
        encoding="utf-8",
    )
    (release / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return release


def test_system_services_runtime_full_contract_accepts_first_publication(
    tmp_path: Path,
) -> None:
    data_root, release = _layout(tmp_path)
    services = VerifyContractServices(data_root, release)
    publisher = FirstInstallPublisher(data_root, release, services=services)

    publisher.publish(lock_held=True)

    assert (
        path_identity(publisher.current, "current")
        == publisher.prepare_payload()["identities"]["current"]["new"]
    )
    assert path_identity(publisher.extension, "extension") == tree_identity(release / "extension")
    assert services.calls.index("validate:runtime-full") < services.calls.index("activate")


def test_system_services_runtime_full_contract_accepts_cross_version_publication(
    tmp_path: Path,
) -> None:
    data_root, first_release = _layout(tmp_path)
    FirstInstallPublisher(
        data_root,
        first_release,
        services=FakeServices(),
    ).publish(lock_held=True)
    second_release = _additional_release(data_root, "0.2.0")
    services = VerifyContractServices(data_root, second_release)
    publisher = FirstInstallPublisher(data_root, second_release, services=services)

    publisher.publish(lock_held=True)

    assert publisher.current.resolve(strict=True) == second_release
    assert path_identity(publisher.extension, "extension") == tree_identity(
        second_release / "extension"
    )


def _assert_only_journaled_publication_artifacts(
    publisher: FirstInstallPublisher,
) -> None:
    latest = publisher.journal.read_latest()
    assert latest is not None
    for component in ("current", "extension"):
        known = (
            latest.payload["identities"][component]["old"],
            latest.payload["identities"][component]["new"],
            {"kind": "absent"},
        )
        for path in publisher._paths(component):
            assert path_identity(path, component) in known
    publisher._validate_retained_extension_quarantines()


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


def test_runtime_failure_before_commit_restores_strict_first_install_absence(
    tmp_path: Path,
) -> None:
    services = FakeServices(runtime_ok=False)
    publisher = _publisher(tmp_path, services)

    with pytest.raises(PublishError, match="runtime"):
        publisher.publish(lock_held=True)

    assert services.activate_call_count == 0
    assert services.calls[-1] == "stop:candidate"
    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ROLLED_BACK"
    assert latest.payload["substate"]["cleanup"] == "complete"
    for component in ("current", "extension"):
        assert all(
            path_identity(path, component) == {"kind": "absent"}
            for path in publisher._paths(component)
        )

    publisher.reconcile(lock_held=True)
    publisher.reconcile(lock_held=True)
    for component in ("current", "extension"):
        assert all(
            path_identity(path, component) == {"kind": "absent"}
            for path in publisher._paths(component)
        )


def test_runtime_failure_restores_exact_previous_publication(tmp_path: Path) -> None:
    first = _publisher(tmp_path, FakeServices())
    first.publish(lock_held=True)
    old_current = path_identity(first.current, "current")
    old_extension = path_identity(first.extension, "extension")
    second_release = _additional_release(first.data_root, "0.2.0")
    second = FirstInstallPublisher(
        first.data_root,
        second_release,
        services=FakeServices(runtime_ok=False),
    )

    with pytest.raises(PublishError, match="runtime"):
        second.publish(lock_held=True)

    latest = second.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ROLLED_BACK"
    assert path_identity(second.current, "current") == old_current
    assert path_identity(second.extension, "extension") == old_extension
    assert path_identity(second.current_next, "current") == {"kind": "absent"}
    assert path_identity(second.current_previous, "current") == {"kind": "absent"}
    assert path_identity(second.extension_next, "extension") == {"kind": "absent"}
    assert path_identity(second.extension_previous, "extension") == {"kind": "absent"}

    second.reconcile(lock_held=True)
    second.reconcile(lock_held=True)
    assert path_identity(second.current, "current") == old_current
    assert path_identity(second.extension, "extension") == old_extension


def test_finalize_rollback_rejects_unrestored_stable_identity(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    publisher.current.symlink_to(payload["identities"]["current"]["new"]["target"])

    with pytest.raises(PublishError, match="rollback identity"):
        publisher._finalize_rollback(payload)

    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] != "ROLLED_BACK"
    assert latest.payload["substate"]["cleanup"] != "complete"


def test_post_committed_switch_failure_releases_handle_and_same_instance_recovers(
    tmp_path: Path,
) -> None:
    class StatefulServices(FakeServices):
        def __init__(self) -> None:
            super().__init__()
            self.active: ActivationHandle | None = None
            self.read_fd = -1
            self.first_write_fd = -1

        def start_precommit(self) -> object:
            if self.active is not None and not self.active.closed:
                raise PublishError("candidate services are already active")
            self.read_fd, write_fd = os.pipe()
            if self.first_write_fd < 0:
                self.first_write_fd = write_fd
            self.active = ActivationHandle(write_fd)
            self.calls.append("start:precommit")
            return self.active

        def activate(self, handle: object) -> None:
            assert handle is self.active
            assert isinstance(handle, ActivationHandle)
            os.close(handle.write_fd)
            os.close(self.read_fd)
            handle.write_fd = -1
            handle.activated = True
            handle.closed = True
            self.active = None
            self.calls.append("activate")

        def stop_candidate(self) -> None:
            self.calls.append("stop:candidate")
            if self.active is None:
                return
            if self.active.write_fd >= 0:
                os.close(self.active.write_fd)
                self.active.write_fd = -1
            if self.read_fd >= 0:
                os.close(self.read_fd)
                self.read_fd = -1
            self.active.closed = True
            self.active = None

    current_switches = 0

    def failpoint(name: str) -> None:
        nonlocal current_switches
        if name == "current:before_switch":
            current_switches += 1
        if current_switches == 2 and name == "current:before_switch":
            raise PublishError("injected committed switch failure")

    services = StatefulServices()
    publisher = _publisher(tmp_path, services, failpoint=failpoint)

    with pytest.raises(PublishError, match="committed switch"):
        publisher.publish(lock_held=True)

    assert services.active is None
    with pytest.raises(OSError):
        os.fstat(services.first_write_fd)

    publisher._failpoint = lambda _name: None
    publisher.reconcile(lock_held=True)
    publisher.reconcile(lock_held=True)

    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ACTIVATED"
    assert (
        path_identity(publisher.current, "current")
        == latest.payload["identities"]["current"]["new"]
    )
    assert (
        path_identity(publisher.extension, "extension")
        == latest.payload["identities"]["extension"]["new"]
    )


def test_publish_preserves_original_and_cleanup_errors(tmp_path: Path) -> None:
    class CleanupFailureServices(FakeServices):
        def stop_candidate(self) -> None:
            raise ExceptionGroup(
                "candidate cleanup failed",
                [RuntimeError("backend cleanup failed"), RuntimeError("ollama cleanup failed")],
            )

    switch_count = 0

    def failpoint(name: str) -> None:
        nonlocal switch_count
        if name == "current:before_switch":
            switch_count += 1
        if switch_count == 2 and name == "current:before_switch":
            raise PublishError("original switch failure")

    publisher = _publisher(
        tmp_path,
        CleanupFailureServices(),
        failpoint=failpoint,
    )

    with pytest.raises(ExceptionGroup) as caught:
        publisher.publish(lock_held=True)

    rendered = repr(caught.value)
    assert "original switch failure" in rendered
    assert "candidate cleanup failed" in rendered


def test_activated_postprocessing_failure_keeps_service_running(
    tmp_path: Path,
) -> None:
    class ActivatedServices(FakeServices):
        def __init__(self) -> None:
            super().__init__()
            self.active = False
            self.start_count = 0
            self.stop_count = 0

        def start_precommit(self) -> object:
            self.active = True
            self.start_count += 1
            return super().start_precommit()

        def stop_candidate(self) -> None:
            self.active = False
            self.stop_count += 1
            super().stop_candidate()

        def copy_token(self, token_path: Path) -> None:
            raise PublishError("clipboard failed")

    services = ActivatedServices()
    publisher = _publisher(tmp_path, services)

    with pytest.raises(PublishError, match="clipboard"):
        publisher.publish(lock_held=True)

    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ACTIVATED"
    assert services.active
    assert services.start_count == 1
    assert services.stop_count == 0

    publisher.reconcile(lock_held=True)
    assert services.active
    assert services.start_count == 1
    assert services.stop_count == 0


def test_durable_activated_write_exception_keeps_service_running(
    tmp_path: Path,
) -> None:
    class ActivatedServices(FakeServices):
        def __init__(self) -> None:
            super().__init__()
            self.active = False
            self.start_count = 0
            self.stop_count = 0

        def start_precommit(self) -> object:
            self.active = True
            self.start_count += 1
            return super().start_precommit()

        def stop_candidate(self) -> None:
            self.active = False
            self.stop_count += 1
            super().stop_candidate()

    services = ActivatedServices()
    publisher = _publisher(tmp_path, services)
    original_write_critical = publisher.journal.write_critical
    injected = False

    def fail_after_durable_activated_write(
        payload: dict[str, Any],
    ) -> tuple[Any, Any]:
        nonlocal injected
        result = original_write_critical(payload)
        if payload["state"] == "ACTIVATED" and not injected:
            injected = True
            raise OSError("injected post-ACTIVATED write failure")
        return result

    publisher.journal.write_critical = fail_after_durable_activated_write  # type: ignore[method-assign]

    with pytest.raises(OSError, match="post-ACTIVATED"):
        publisher.publish(lock_held=True)

    assert publisher.journal.verify_critical("ACTIVATED")
    assert services.active
    assert services.start_count == 1
    assert services.stop_count == 0

    publisher.journal.write_critical = original_write_critical  # type: ignore[method-assign]
    publisher.reconcile(lock_held=True)
    assert services.active
    assert services.start_count == 1
    assert services.stop_count == 0


def test_single_durable_activated_slot_keeps_service_running_and_repairs(
    tmp_path: Path,
) -> None:
    class ActivatedServices(FakeServices):
        def __init__(self) -> None:
            super().__init__()
            self.active = False
            self.stop_count = 0

        def start_precommit(self) -> object:
            self.active = True
            return super().start_precommit()

        def stop_candidate(self) -> None:
            self.active = False
            self.stop_count += 1
            super().stop_candidate()

    services = ActivatedServices()
    publisher = _publisher(tmp_path, services)
    journal_root = publisher.journal.root
    injected = False

    def fail_second_activated_slot(name: str) -> None:
        nonlocal injected
        if not injected and "activate" in services.calls and name == "slot-b:before_temp_write":
            injected = True
            raise OSError("injected second ACTIVATED slot failure")

    publisher.journal = TransactionJournal(journal_root, failpoint=fail_second_activated_slot)

    with pytest.raises(OSError, match="second ACTIVATED"):
        publisher.publish(lock_held=True)

    latest = TransactionJournal(journal_root).read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ACTIVATED"
    assert services.active
    assert services.stop_count == 0

    publisher.journal = TransactionJournal(journal_root)
    publisher.reconcile(lock_held=True)

    assert publisher.journal.verify_critical("ACTIVATED")
    assert services.active
    assert services.stop_count == 0


def test_publish_preserves_original_cleanup_and_direction_errors(
    tmp_path: Path,
) -> None:
    class CleanupFailureServices(FakeServices):
        def stop_candidate(self) -> None:
            raise RuntimeError("cleanup failed")

    publisher = _publisher(
        tmp_path,
        CleanupFailureServices(runtime_ok=False),
    )

    def fail_direction() -> str:
        raise JournalError("direction failed")

    publisher.journal.committed_direction = fail_direction  # type: ignore[method-assign]

    with pytest.raises(ExceptionGroup) as caught:
        publisher.publish(lock_held=True)

    assert [type(error) for error in caught.value.exceptions] == [
        PublishError,
        RuntimeError,
        JournalError,
    ]
    assert [str(error) for error in caught.value.exceptions] == [
        "runtime-full validation failed",
        "cleanup failed",
        "direction failed",
    ]


def test_reconcile_attempts_filesystem_rollback_when_service_stop_fails(
    tmp_path: Path,
) -> None:
    class StopFailureServices(FakeServices):
        def stop_candidate(self) -> None:
            raise RuntimeError("stop failed")

    data_root, release = _layout(tmp_path)
    publisher = FirstInstallPublisher(
        data_root,
        release,
        services=StopFailureServices(),
    )
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    publisher.current.symlink_to(payload["identities"]["current"]["new"]["target"])
    publisher.copy_tree(release / "extension", publisher.extension)

    with pytest.raises(RuntimeError, match="stop failed"):
        publisher.reconcile(lock_held=True)

    assert path_identity(publisher.current, "current") == {"kind": "absent"}
    assert path_identity(publisher.extension, "extension") == {"kind": "absent"}
    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] != "ROLLED_BACK"


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
def test_switch_failpoints_recover_to_journaled_retained_state(
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

    _assert_only_journaled_publication_artifacts(recovered)


@pytest.mark.parametrize(
    "boundary",
    ["before_next_file_fsync", "after_next_file_fsync"],
)
def test_extension_file_fsync_failpoints_recover_without_partial_artifacts(
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
    _assert_only_journaled_publication_artifacts(recovered)


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
        _assert_only_journaled_publication_artifacts(recovered)


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
        _assert_only_journaled_publication_artifacts(publisher)
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


def test_different_extension_publish_ignores_verified_historical_candidate(
    tmp_path: Path,
) -> None:
    first = _publisher(tmp_path, FakeServices())
    first.publish(lock_held=True)
    retained = list(first.data_root.glob("extension.next.candidate-*"))
    assert len(retained) == 1
    retained_before = retained[0].stat()
    second_release = _additional_release(first.data_root, "0.2.0")
    expected_extension = tree_identity(second_release / "extension")
    second = FirstInstallPublisher(first.data_root, second_release, services=FakeServices())

    second.publish(lock_held=True)

    assert path_identity(second.current, "current") == {
        "kind": "symlink",
        "target": "releases/0.2.0",
        "sha256": publish_install._sha256_bytes(b"releases/0.2.0"),
    }
    assert path_identity(second.extension, "extension") == expected_extension
    retained_after = retained[0].stat()
    assert (retained_after.st_dev, retained_after.st_ino) == (
        retained_before.st_dev,
        retained_before.st_ino,
    )
    assert second._extension_candidate_is_owned(
        {"transaction_id": retained[0].name.removeprefix("extension.next.candidate-")},
        candidate=retained[0],
    )


def test_different_extension_crash_reconcile_is_idempotent_with_historical_candidate(
    tmp_path: Path,
) -> None:
    first = _publisher(tmp_path, FakeServices())
    first.publish(lock_held=True)
    historical = list(first.data_root.glob("extension.next.candidate-*"))
    assert len(historical) == 1
    historical_before = historical[0].stat()
    second_release = _additional_release(first.data_root, "0.2.0")
    fired = False

    def failpoint(name: str) -> None:
        nonlocal fired
        if not fired and name == "extension-copy:after_first_file":
            fired = True
            raise SimulatedCrash

    second = FirstInstallPublisher(
        first.data_root,
        second_release,
        services=FakeServices(),
        failpoint=failpoint,
    )
    with pytest.raises(SimulatedCrash):
        second.publish(lock_held=True)

    recovered = FirstInstallPublisher(
        first.data_root,
        second_release,
        services=FakeServices(),
    )
    recovered.reconcile(lock_held=True)
    first_snapshot = recovered.filesystem_snapshot()
    recovered.reconcile(lock_held=True)

    assert recovered.filesystem_snapshot() == first_snapshot
    assert path_identity(recovered.extension, "extension") != tree_identity(
        second_release / "extension"
    )
    historical_after = historical[0].stat()
    assert (historical_after.st_dev, historical_after.st_ino) == (
        historical_before.st_dev,
        historical_before.st_ino,
    )

    recovered.publish(lock_held=True)
    recovered.reconcile(lock_held=True)
    assert path_identity(recovered.extension, "extension") == tree_identity(
        second_release / "extension"
    )


def test_multiple_historical_candidates_allow_third_extension_identity(
    tmp_path: Path,
) -> None:
    first = _publisher(tmp_path, FakeServices())
    first.publish(lock_held=True)
    second_release = _additional_release(first.data_root, "0.2.0")
    second = FirstInstallPublisher(
        first.data_root,
        second_release,
        services=FakeServices(),
    )
    second.publish(lock_held=True)
    retained = list(first.data_root.glob("extension.next.candidate-*"))
    assert len(retained) == 2
    retained_metadata = {path.name: (path.stat().st_dev, path.stat().st_ino) for path in retained}
    third_release = _additional_release(first.data_root, "0.3.0")
    third = FirstInstallPublisher(
        first.data_root,
        third_release,
        services=FakeServices(),
    )

    third.publish(lock_held=True)
    third.reconcile(lock_held=True)
    third.reconcile(lock_held=True)

    assert path_identity(third.extension, "extension") == tree_identity(third_release / "extension")
    for path in retained:
        assert (path.stat().st_dev, path.stat().st_ino) == retained_metadata[path.name]


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

        _assert_only_journaled_publication_artifacts(recovered)
        assert not list(data_root.glob("extension.next.candidate-*.owner"))
        retained = [
            *data_root.glob("extension.next.candidate-*"),
            *data_root.glob(".extension.next.bootstrap-*"),
            *data_root.glob(".extension.next.tombstone-*"),
            *data_root.glob(".extension.next.deleting-*"),
        ]
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
) -> None:
    before_claim = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "extension-candidate:before_tombstone_claim":
            before_claim.wait(timeout=5)
            resume.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
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
    foreign_before = foreign.stat()

    def remove() -> None:
        try:
            publisher._remove_owned_extension_candidate(payload)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=remove)
    worker.start()
    before_claim.wait(timeout=5)
    candidate.rename(displaced)
    replacement.rename(candidate)
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    preserved = candidate / "untrusted"
    foreign_after = preserved.stat()
    assert (foreign_after.st_dev, foreign_after.st_ino) == (
        foreign_before.st_dev,
        foreign_before.st_ino,
    )
    assert preserved.read_text(encoding="utf-8") == "external\n"
    assert displaced.is_dir()
    assert (displaced / "owned").read_text(encoding="utf-8") == "owned\n"


def test_remove_known_preserves_source_replacement_at_original_name(
    tmp_path: Path,
) -> None:
    before_claim = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "retained:extension:before_source_check":
            before_claim.wait(timeout=5)
            resume.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    payload = publisher.prepare_payload()
    previous = publisher.extension_previous
    displaced = previous.parent / "displaced-owned-previous"
    replacement = previous.parent / "foreign-previous"
    previous.mkdir()
    (previous / "owned").write_text("owned\n", encoding="utf-8")
    identity = tree_identity(previous)
    replacement.mkdir()
    foreign = replacement / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")
    foreign_before = foreign.stat()

    def retain() -> None:
        try:
            publisher._remove_known(
                previous,
                "extension",
                (identity, {"kind": "absent"}),
                payload,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=retain)
    worker.start()
    before_claim.wait(timeout=5)
    previous.rename(displaced)
    replacement.rename(previous)
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    preserved = previous / "untrusted"
    foreign_after = preserved.stat()
    assert (foreign_after.st_dev, foreign_after.st_ino) == (
        foreign_before.st_dev,
        foreign_before.st_ino,
    )
    assert preserved.read_text(encoding="utf-8") == "external\n"
    assert (displaced / "owned").read_text(encoding="utf-8") == "owned\n"


def test_remove_known_preserves_replacement_after_source_check(
    tmp_path: Path,
) -> None:
    checked = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "retained:extension:after_source_check":
            checked.wait(timeout=5)
            resume.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    payload = publisher.prepare_payload()
    previous = publisher.extension_previous
    displaced = previous.parent / "displaced-after-check"
    replacement = previous.parent / "foreign-after-check"
    previous.mkdir()
    (previous / "owned").write_text("owned\n", encoding="utf-8")
    identity = tree_identity(previous)
    replacement.mkdir()
    foreign = replacement / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")

    def retain() -> None:
        try:
            publisher._remove_known(
                previous,
                "extension",
                (identity, {"kind": "absent"}),
                payload,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=retain)
    worker.start()
    checked.wait(timeout=5)
    previous.rename(displaced)
    replacement.rename(previous)
    foreign_before = (previous / "untrusted").stat()
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    foreign_after = (previous / "untrusted").stat()
    assert (foreign_after.st_dev, foreign_after.st_ino) == (
        foreign_before.st_dev,
        foreign_before.st_ino,
    )
    assert (previous / "untrusted").read_text(encoding="utf-8") == "external\n"
    assert (displaced / "owned").read_text(encoding="utf-8") == "owned\n"


def test_remove_known_inode_bound_rename_preserves_final_replacement(
    tmp_path: Path,
) -> None:
    checked = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "retained:extension:before_inode_rename":
            checked.wait(timeout=5)
            resume.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    payload = publisher.prepare_payload()
    previous = publisher.extension_previous
    displaced = previous.parent / "displaced-before-inode-rename"
    replacement = previous.parent / "foreign-before-inode-rename"
    previous.mkdir()
    (previous / "owned").write_text("owned\n", encoding="utf-8")
    identity = tree_identity(previous)
    replacement.mkdir()
    foreign = replacement / "untrusted"
    foreign.write_text("external\n", encoding="utf-8")

    def retain() -> None:
        try:
            publisher._remove_known(
                previous,
                "extension",
                (identity, {"kind": "absent"}),
                payload,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=retain)
    worker.start()
    checked.wait(timeout=5)
    previous.rename(displaced)
    replacement.rename(previous)
    foreign_before = (previous / "untrusted").stat()
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    foreign_after = (previous / "untrusted").stat()
    assert (foreign_after.st_dev, foreign_after.st_ino) == (
        foreign_before.st_dev,
        foreign_before.st_ino,
    )
    assert (previous / "untrusted").read_text(encoding="utf-8") == "external\n"
    retained = previous.parent / (
        f".publication.retained-extension-{previous.name}-{payload['transaction_id']}"
    )
    assert (retained / "owned").read_text(encoding="utf-8") == "owned\n"
    assert not displaced.exists()


def test_remove_known_inode_bound_rename_preserves_symlink_replacement(
    tmp_path: Path,
) -> None:
    checked = threading.Barrier(2)
    resume = threading.Barrier(2)
    errors: list[BaseException] = []

    def failpoint(name: str) -> None:
        if name == "retained:current:before_inode_rename":
            checked.wait(timeout=5)
            resume.wait(timeout=5)

    publisher = _publisher(tmp_path, FakeServices(), failpoint=failpoint)
    payload = publisher.prepare_payload()
    previous = publisher.current_previous
    displaced = previous.parent / "displaced-current-before-inode-rename"
    replacement = previous.parent / "foreign-current-before-inode-rename"
    previous.symlink_to("releases/0.1.0")
    identity = path_identity(previous, "current")
    replacement.symlink_to("releases/foreign")

    def retain() -> None:
        try:
            publisher._remove_known(
                previous,
                "current",
                (identity, {"kind": "absent"}),
                payload,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=retain)
    worker.start()
    checked.wait(timeout=5)
    previous.rename(displaced)
    replacement.rename(previous)
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    assert previous.is_symlink()
    assert os.readlink(previous) == "releases/foreign"
    retained = previous.parent / (
        f".publication.retained-current-{previous.name}-{payload['transaction_id']}"
    )
    assert retained.is_symlink()
    assert os.readlink(retained) == "releases/0.1.0"
    assert not displaced.exists() and not displaced.is_symlink()


def test_inode_bound_rename_fsyncs_bound_destination_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "publication-parent"
    parent.mkdir()
    source = parent / "source"
    source.mkdir()
    destination = parent / "retained"
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    parent_metadata = parent.stat()
    original_fsync = os.fsync
    bound_parent_fsyncs = 0

    def count_bound_parent_fsync(descriptor: int) -> None:
        nonlocal bound_parent_fsyncs
        metadata = os.fstat(descriptor)
        if (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == parent_metadata.st_dev
            and metadata.st_ino == parent_metadata.st_ino
        ):
            bound_parent_fsyncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(publish_install.os, "fsync", count_bound_parent_fsync)
    try:
        publish_install._rename_open_publication_exclusive(
            source_descriptor,
            destination,
            "extension",
        )
    finally:
        os.close(source_descriptor)

    assert bound_parent_fsyncs >= 1
    assert (parent / "retained").is_dir()
    assert not source.exists()


def test_inode_bound_rename_rejects_parent_replaced_after_source_parent_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "publication-parent"
    parent.mkdir()
    source = parent / "source"
    source.mkdir()
    destination = parent / "retained"
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    displaced_parent = tmp_path / "displaced-publication-parent"
    original_open_parent = publish_install._open_directory_descriptor
    displaced = False

    def displace_after_open(path: Path) -> int:
        nonlocal displaced
        descriptor = original_open_parent(path)
        if not displaced and path == parent:
            displaced = True
            parent.rename(displaced_parent)
            parent.mkdir()
        return descriptor

    monkeypatch.setattr(
        publish_install,
        "_open_directory_descriptor",
        displace_after_open,
    )
    try:
        with pytest.raises(PublishError, match="destination parent"):
            publish_install._rename_open_publication_exclusive(
                source_descriptor,
                destination,
                "extension",
            )
    finally:
        os.close(source_descriptor)

    assert (displaced_parent / "source").is_dir()
    assert not (displaced_parent / "retained").exists()
    assert not (parent / "retained").exists()


def test_inode_bound_rename_rejects_destination_parent_replaced_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "publication-parent"
    parent.mkdir()
    source = parent / "source"
    source.mkdir()
    destination = parent / "retained"
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    displaced_parent = tmp_path / "displaced-publication-parent"
    original_open_parent = publish_install._open_directory_descriptor
    displaced = False

    def displace_before_open(path: Path) -> int:
        nonlocal displaced
        if not displaced and path == parent:
            displaced = True
            parent.rename(displaced_parent)
            parent.mkdir()
        return original_open_parent(path)

    monkeypatch.setattr(
        publish_install,
        "_open_directory_descriptor",
        displace_before_open,
    )
    try:
        with pytest.raises(PublishError, match="parent"):
            publish_install._rename_open_publication_exclusive(
                source_descriptor,
                destination,
                "extension",
            )
    finally:
        os.close(source_descriptor)

    assert (displaced_parent / "source").is_dir()
    assert not (displaced_parent / "retained").exists()
    assert not (parent / "retained").exists()


def test_extension_candidate_is_retained_at_original_name(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    candidate = publisher._extension_candidate_paths(payload)[0]
    candidate.mkdir(mode=0o700)
    publisher._write_extension_candidate_owner(candidate, payload)
    nested = candidate / "payload/nested"
    nested.mkdir(parents=True)
    content = nested / "asset.js"
    content.write_text("owned-content\n", encoding="utf-8")
    before = content.stat()

    publisher._remove_owned_extension_candidate(payload)

    after = content.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert content.read_text(encoding="utf-8") == "owned-content\n"
    assert (candidate / ".owner.json").is_file()
    assert not publisher._extension_tombstone_path(payload).exists()
    assert not publisher._extension_deletion_path(payload).exists()


def test_extension_candidate_owner_rejects_zero_progress_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    candidate = publisher._extension_bootstrap_path(payload)
    candidate.mkdir(mode=0o700)
    original_write = os.write
    injected = False

    def zero_once(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal injected
        if not injected:
            injected = True
            return 0
        return original_write(descriptor, data)

    monkeypatch.setattr(publish_install.os, "write", zero_once)

    with pytest.raises(PublishError, match="no progress"):
        publisher._write_extension_candidate_owner(candidate, payload)


def test_partial_candidate_owner_write_recovers_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    original_write = os.write
    injected = False

    def partial_then_fail(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal injected
        if not injected:
            injected = True
            original_write(descriptor, data[: max(1, len(data) // 2)])
            raise OSError("injected partial owner write")
        return original_write(descriptor, data)

    with monkeypatch.context() as failure:
        failure.setattr(publish_install.os, "write", partial_then_fail)
        with pytest.raises(OSError, match="partial owner"):
            publisher._write_extension_candidate_owner(bootstrap, payload)

    assert not (bootstrap / ".owner.json").exists()
    assert list(bootstrap.glob(".owner.json.staged-*"))

    publisher.reconcile(lock_held=True)
    first = publisher.journal.read_latest()
    assert first is not None
    assert first.payload["state"] == "ROLLED_BACK"
    assert publisher._extension_candidate_is_owned(payload, candidate=bootstrap)
    assert publisher._extension_candidate_is_retained(payload, candidate=bootstrap)

    publisher.reconcile(lock_held=True)
    second = publisher.journal.read_latest()
    assert second is not None
    assert second.generation == first.generation


def test_foreign_bootstrap_staging_content_is_never_adopted(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    staged = bootstrap / f".owner.json.staged-{'a' * 32}"
    staged.write_text("foreign owner staging\n", encoding="utf-8")
    staged.chmod(0o600)

    with pytest.raises(PublishError, match="owner staging"):
        publisher.reconcile(lock_held=True)

    assert not (bootstrap / ".owner.json").exists()
    assert staged.read_text(encoding="utf-8") == "foreign owner staging\n"


@pytest.mark.parametrize("foreign_owner", [b"foreign owner\n", b"\xff"])
def test_foreign_canonical_bootstrap_owner_fails_closed_without_raw_decode_error(
    tmp_path: Path,
    foreign_owner: bytes,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    canonical = bootstrap / ".owner.json"
    canonical.write_bytes(foreign_owner)
    canonical.chmod(0o600)
    before = canonical.stat()

    with pytest.raises(PublishError, match="unverified content"):
        publisher.reconcile(lock_held=True)

    after = canonical.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert canonical.read_bytes() == foreign_owner


def test_foreign_bootstrap_short_owner_prefix_is_never_adopted(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    staged = bootstrap / f".owner.json.staged-{'a' * 32}"
    staged.write_bytes(b"{")
    staged.chmod(0o600)

    with pytest.raises(PublishError, match="owner staging"):
        publisher.reconcile(lock_held=True)

    assert not (bootstrap / ".owner.json").exists()
    assert staged.read_bytes() == b"{"


@pytest.mark.parametrize(
    ("proof_variant", "accepted"),
    [
        ("minimum_minus_one", False),
        ("minimum", True),
        ("minimum_plus_one", True),
        ("wrong_device", False),
        ("wrong_inode", False),
        ("trailing_byte", False),
    ],
)
def test_bootstrap_owner_proof_boundaries(
    tmp_path: Path,
    proof_variant: str,
    accepted: bool,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    metadata = bootstrap.stat()
    expected = publisher._extension_candidate_owner_bytes(payload, metadata)
    minimum = (
        json.dumps(
            {"device": metadata.st_dev, "inode": metadata.st_ino},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )[:-1]
        + ","
    ).encode("ascii")
    if proof_variant == "minimum_minus_one":
        proof = minimum[:-1]
    elif proof_variant == "minimum":
        proof = minimum
    elif proof_variant == "minimum_plus_one":
        proof = expected[: len(minimum) + 1]
    elif proof_variant == "wrong_device":
        proof = (
            json.dumps(
                {"device": metadata.st_dev + 1, "inode": metadata.st_ino},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )[:-1]
            + ","
        ).encode("ascii")
    elif proof_variant == "wrong_inode":
        proof = (
            json.dumps(
                {"device": metadata.st_dev, "inode": metadata.st_ino + 1},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )[:-1]
            + ","
        ).encode("ascii")
    elif proof_variant == "trailing_byte":
        proof = expected + b"x"
    else:
        raise AssertionError("unknown owner proof variant")
    staged = bootstrap / f".owner.json.staged-{'a' * 32}"
    staged.write_bytes(proof)
    staged.chmod(0o600)

    if accepted:
        publisher.reconcile(lock_held=True)
        assert publisher._extension_candidate_is_owned(payload, candidate=bootstrap)
    else:
        with pytest.raises(PublishError, match="owner staging"):
            publisher.reconcile(lock_held=True)
        assert not (bootstrap / ".owner.json").exists()


@pytest.mark.parametrize("replacement_read", [1, 2])
def test_bootstrap_owner_proof_rejects_post_open_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_read: int,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    metadata = bootstrap.stat()
    minimum = publisher._extension_candidate_owner_proof_bytes(metadata)
    staged = bootstrap / f".owner.json.staged-{'a' * 32}"
    displaced = bootstrap / "displaced-owner-proof"
    staged.write_bytes(minimum)
    staged.chmod(0o600)
    original_read = os.read
    read_count = 0

    def replace_before_read(descriptor: int, length: int) -> bytes:
        nonlocal read_count
        read_count += 1
        if read_count == replacement_read:
            staged.rename(displaced)
            staged.write_bytes(minimum)
            staged.chmod(0o600)
        return original_read(descriptor, length)

    monkeypatch.setattr(publish_install.os, "read", replace_before_read)

    with pytest.raises(PublishError, match="owner staging"):
        publisher.reconcile(lock_held=True)

    assert not (bootstrap / ".owner.json").exists()
    assert staged.read_bytes() == minimum
    assert displaced.read_bytes() == minimum


def test_bootstrap_owner_proof_schema_reordering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    publisher.journal.write_progress(payload)
    bootstrap = publisher._extension_bootstrap_path(payload)
    bootstrap.mkdir(mode=0o700)
    metadata = bootstrap.stat()
    staged = bootstrap / f".owner.json.staged-{'a' * 32}"
    staged.write_bytes(b'{"device":1,"inode":1,')
    staged.chmod(0o600)
    reordered = (
        json.dumps(
            {
                "schema_version": 1,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "transaction_nonce": payload["transaction_id"],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    monkeypatch.setattr(
        publisher,
        "_extension_candidate_owner_bytes",
        lambda _payload, _metadata: reordered,
    )

    with pytest.raises(PublishError, match="owner staging"):
        publisher.reconcile(lock_held=True)


def test_historical_retained_marker_replays_all_fsync_barriers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    payload = publisher.prepare_payload()
    candidate = publisher._extension_candidate_paths(payload)[0]
    candidate.mkdir(mode=0o700)
    publisher._write_extension_candidate_owner(candidate, payload)
    retained = candidate / ".retained"
    retained.touch(mode=0o600)
    marker_metadata = retained.stat()
    candidate_metadata = candidate.stat()
    parent_metadata = candidate.parent.stat()
    original_fsync = os.fsync
    fsynced: set[tuple[int, int]] = set()

    def track_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        fsynced.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    monkeypatch.setattr(publish_install.os, "fsync", track_fsync)

    current, historical = publisher._extension_candidate_namespace(payload)

    assert current == set()
    assert historical == {candidate}
    assert (marker_metadata.st_dev, marker_metadata.st_ino) in fsynced
    assert (candidate_metadata.st_dev, candidate_metadata.st_ino) in fsynced
    assert (parent_metadata.st_dev, parent_metadata.st_ino) in fsynced


def test_completed_candidate_is_historical_and_reconcile_is_quiescent(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path, FakeServices())
    publisher.publish(lock_held=True)
    latest = publisher.journal.read_latest()
    assert latest is not None
    candidate = publisher._extension_candidate_paths(latest.payload)[0]

    current, historical = publisher._extension_candidate_namespace(latest.payload)
    assert current == set()
    assert candidate in historical
    assert {entry.name for entry in candidate.iterdir()} == {
        ".owner.json",
        ".retained",
    }

    generation = latest.generation
    publisher.reconcile(lock_held=True)
    publisher.reconcile(lock_held=True)

    latest_after = publisher.journal.read_latest()
    assert latest_after is not None
    assert latest_after.generation == generation


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

    retained = candidate
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
    candidate.rename(displaced)
    replacement.rename(candidate)
    resume.wait(timeout=5)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PublishError)
    preserved = candidate / "untrusted"
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


def test_start_precommit_unexpected_failure_closes_pipes_and_cleans_started_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedFailureOperations:
        calls: list[str] = []

        def __init__(self, _data_root: Path, _release_root: Path) -> None:
            pass

        def state(self, kind: str) -> str:
            return "absent"

        def launch(self, kind: str, _activation_fd: int | None = None) -> str:
            if kind == "backend":
                raise ValueError("invalid backend metadata")
            return "ollama-generation"

        def stop_matching(self, kind: str, identity: object) -> None:
            self.calls.append(f"{kind}:{identity}")

    monkeypatch.setattr(
        process_state,
        "SystemServiceOperations",
        UnexpectedFailureOperations,
    )
    original_pipe = os.pipe
    pipe_fds: list[int] = []

    def observed_pipe() -> tuple[int, int]:
        descriptors = original_pipe()
        pipe_fds.extend(descriptors)
        return descriptors

    monkeypatch.setattr(publish_install.os, "pipe", observed_pipe)
    data_root, release = _layout(tmp_path)
    services = publish_install.SystemPublicationServices(data_root, release)

    try:
        with pytest.raises(ValueError) as caught:
            services.start_precommit()

        assert type(caught.value) is ValueError
        assert str(caught.value) == "invalid backend metadata"
        assert UnexpectedFailureOperations.calls == ["ollama:ollama-generation"]
        assert len(pipe_fds) == 2
        for descriptor in pipe_fds:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in pipe_fds:
            with suppress(OSError):
                os.close(descriptor)


def test_start_precommit_multiple_errors_preserve_start_backend_ollama_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MultipleFailureOperations:
        def __init__(self, _data_root: Path, _release_root: Path) -> None:
            pass

        def state(self, kind: str) -> str:
            return "absent"

        def launch(self, kind: str, _activation_fd: int | None = None) -> str:
            return f"{kind}-generation"

        def backend_healthy(self) -> bool:
            raise ValueError("health metadata invalid")

        def stop_matching(self, kind: str, identity: object) -> None:
            raise process_state.ServiceError(f"{kind}:{identity}")

    monkeypatch.setattr(
        process_state,
        "SystemServiceOperations",
        MultipleFailureOperations,
    )
    original_pipe = os.pipe
    pipe_fds: list[int] = []

    def observed_pipe() -> tuple[int, int]:
        descriptors = original_pipe()
        pipe_fds.extend(descriptors)
        return descriptors

    monkeypatch.setattr(publish_install.os, "pipe", observed_pipe)
    data_root, release = _layout(tmp_path)
    services = publish_install.SystemPublicationServices(data_root, release)

    try:
        with pytest.raises(ExceptionGroup) as caught:
            services.start_precommit()

        assert [type(error) for error in caught.value.exceptions] == [
            ValueError,
            process_state.ServiceError,
            process_state.ServiceError,
        ]
        assert [str(error) for error in caught.value.exceptions] == [
            "health metadata invalid",
            "backend:backend-generation",
            "ollama:ollama-generation",
        ]
        assert len(pipe_fds) == 2
        for descriptor in pipe_fds:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        for descriptor in pipe_fds:
            with suppress(OSError):
                os.close(descriptor)


@pytest.mark.parametrize("failed_kind", ["backend", "ollama"])
def test_stop_candidate_preserves_single_cleanup_error_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_kind: str,
) -> None:
    class FailingOperations:
        calls: list[str] = []

        def __init__(self, _data_root: Path, _release_root: Path) -> None:
            pass

        def stop_matching(self, kind: str, identity: object) -> None:
            self.calls.append(kind)
            assert identity == f"{kind}-generation"
            if kind == failed_kind:
                raise process_state.ServiceError(f"{kind} generation changed")

    monkeypatch.setattr(
        process_state,
        "SystemServiceOperations",
        FailingOperations,
    )
    data_root, release = _layout(tmp_path)
    services = publish_install.SystemPublicationServices(data_root, release)
    handle = ActivationHandle(
        -1,
        backend_identity="backend-generation",
        ollama_identity="ollama-generation",
    )
    services._active_handle = handle

    with pytest.raises(
        process_state.ServiceError,
        match=f"{failed_kind} generation changed",
    ):
        services.stop_candidate()

    assert FailingOperations.calls == ["backend", "ollama"]
    assert handle.closed
    assert services._active_handle is None
    services.stop_candidate()
    assert FailingOperations.calls == ["backend", "ollama"]


def test_stop_candidate_multiple_errors_have_stable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingOperations:
        def __init__(self, _data_root: Path, _release_root: Path) -> None:
            pass

        def stop_matching(self, kind: str, identity: object) -> None:
            raise process_state.ServiceError(f"{kind}:{identity}")

    monkeypatch.setattr(
        process_state,
        "SystemServiceOperations",
        FailingOperations,
    )
    data_root, release = _layout(tmp_path)
    services = publish_install.SystemPublicationServices(data_root, release)
    handle = ActivationHandle(
        -1,
        backend_identity="backend-generation",
        ollama_identity="ollama-generation",
    )
    services._active_handle = handle

    with pytest.raises(ExceptionGroup) as caught:
        services.stop_candidate()

    assert [type(error) for error in caught.value.exceptions] == [
        process_state.ServiceError,
        process_state.ServiceError,
    ]
    assert [str(error) for error in caught.value.exceptions] == [
        "backend:backend-generation",
        "ollama:ollama-generation",
    ]
    assert handle.closed
    assert services._active_handle is None


def test_stop_candidate_constructor_failure_still_closes_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConstructorFailure:
        def __init__(self, _data_root: Path, _release_root: Path) -> None:
            raise process_state.ServiceError("operations unavailable")

    monkeypatch.setattr(
        process_state,
        "SystemServiceOperations",
        ConstructorFailure,
    )
    data_root, release = _layout(tmp_path)
    services = publish_install.SystemPublicationServices(data_root, release)
    read_fd, write_fd = os.pipe()
    handle = ActivationHandle(
        write_fd,
        backend_identity="backend-generation",
    )
    services._active_handle = handle

    try:
        with pytest.raises(process_state.ServiceError, match="operations unavailable"):
            services.stop_candidate()
        with pytest.raises(OSError):
            os.fstat(write_fd)
        assert handle.closed
        assert services._active_handle is None
    finally:
        os.close(read_fd)
