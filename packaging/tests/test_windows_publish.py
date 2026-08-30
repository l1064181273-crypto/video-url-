from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

from windows_lifecycle import LifecycleResult  # noqa: E402
from windows_publish_install import (  # noqa: E402
    SystemWindowsPublicationServices,
    WindowsInstallPublisher,
    WindowsPublishError,
)


class FakePublicationServices:
    def __init__(
        self,
        *,
        runtime_ok: bool = True,
        activate_error: bool = False,
    ) -> None:
        self.runtime_ok = runtime_ok
        self.activate_error = activate_error
        self.calls: list[str] = []
        self.active = False

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
        if self.activate_error:
            raise WindowsPublishError("activation failed")
        self.active = True

    def healthy(self) -> bool:
        self.calls.append("health")
        return self.active

    def stop_candidate(self) -> None:
        self.calls.append("stop:candidate")
        self.active = False

    def ensure_committed_running(self) -> object:
        self.calls.append("ensure:committed")
        return object()

    def restore_rollback(self) -> None:
        self.calls.append("restore:rollback")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.1"
    extension = release / "extension"
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        '{"manifest_version":3,"version":"0.1.1"}\n',
        encoding="utf-8",
    )
    (extension / "sidepanel.html").write_text("<main></main>\n", encoding="utf-8")
    (release / "VERSION").write_text("0.1.1\n", encoding="utf-8")
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "install-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "core": {
                    "release": "app/releases/0.1.1",
                    "verified": True,
                    "version": "0.1.1",
                },
            }
        ),
        encoding="utf-8",
    )
    return data_root, release


def test_publish_commits_before_activation_and_marks_install_state(tmp_path: Path) -> None:
    data_root, release = _layout(tmp_path)
    services = FakePublicationServices()
    publisher = WindowsInstallPublisher(data_root, release, services=services)

    publisher.publish(lock_held=True)

    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ACTIVATED"
    assert publisher.journal.verify_critical("ACTIVATED")
    state = json.loads((data_root / "runtime/install-state.json").read_text(encoding="utf-8"))
    assert state["core"]["activated"] is True
    assert json.loads((data_root / "app/current.json").read_text(encoding="utf-8")) == {
        "release": "app/releases/0.1.1",
        "version": "0.1.1",
    }
    assert (data_root / "extension/manifest.json").is_file()
    assert services.calls == [
        "validate:staging-core",
        "validate:dependencies",
        "start:precommit",
        "validate:runtime-full",
        "activate",
        "health",
    ]


def test_precommit_failure_rolls_back_and_stops_candidate(tmp_path: Path) -> None:
    data_root, release = _layout(tmp_path)
    services = FakePublicationServices(runtime_ok=False)
    publisher = WindowsInstallPublisher(data_root, release, services=services)

    with pytest.raises(WindowsPublishError, match="runtime-full"):
        publisher.publish(lock_held=True)

    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ROLLED_BACK"
    assert not (data_root / "app/current.json").exists()
    assert not (data_root / "extension").exists()
    assert "stop:candidate" in services.calls
    state = json.loads((data_root / "runtime/install-state.json").read_text(encoding="utf-8"))
    assert "activated" not in state["core"]


def test_postcommit_failure_keeps_candidate_and_reconcile_rolls_forward(
    tmp_path: Path,
) -> None:
    data_root, release = _layout(tmp_path)
    services = FakePublicationServices(activate_error=True)
    publisher = WindowsInstallPublisher(data_root, release, services=services)

    with pytest.raises(WindowsPublishError, match="activation"):
        publisher.publish(lock_held=True)

    assert publisher.journal.committed_direction() == "committed"
    assert "stop:candidate" not in services.calls
    services.activate_error = False

    publisher.reconcile(lock_held=True)
    publisher.reconcile(lock_held=True)

    latest = publisher.journal.read_latest()
    assert latest is not None
    assert latest.payload["state"] == "ACTIVATED"
    assert services.calls.count("ensure:committed") == 1


def test_failpoint_after_committed_cannot_roll_back(tmp_path: Path) -> None:
    data_root, release = _layout(tmp_path)
    services = FakePublicationServices()

    def failpoint(name: str) -> None:
        if name == "activation:before_install_state":
            raise WindowsPublishError("injected post-commit failure")

    publisher = WindowsInstallPublisher(
        data_root,
        release,
        services=services,
        failpoint=failpoint,
    )

    with pytest.raises(WindowsPublishError, match="post-commit"):
        publisher.publish(lock_held=True)

    assert publisher.journal.verify_critical("COMMITTED")
    assert "stop:candidate" not in services.calls
    state = json.loads((data_root / "runtime/install-state.json").read_text(encoding="utf-8"))
    assert "activated" not in state["core"]


def test_upgrade_rollback_restores_old_pointer_extension_and_activated_state(
    tmp_path: Path,
) -> None:
    data_root, release = _layout(tmp_path)
    old_release = data_root / "app/releases/0.1.0"
    old_release.mkdir(parents=True)
    (old_release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (data_root / "app/current.json").write_text(
        '{"release":"app/releases/0.1.0","version":"0.1.0"}\n',
        encoding="ascii",
    )
    stable_extension = data_root / "extension"
    stable_extension.mkdir()
    (stable_extension / "manifest.json").write_text(
        '{"manifest_version":3,"version":"0.1.0"}\n',
        encoding="utf-8",
    )
    services = FakePublicationServices(runtime_ok=False)
    publisher = WindowsInstallPublisher(data_root, release, services=services)

    with pytest.raises(WindowsPublishError, match="runtime-full"):
        publisher.publish(lock_held=True)

    assert json.loads((data_root / "app/current.json").read_text(encoding="ascii")) == {
        "release": "app/releases/0.1.0",
        "version": "0.1.0",
    }
    assert (
        json.loads((data_root / "extension/manifest.json").read_text(encoding="utf-8"))["version"]
        == "0.1.0"
    )
    state = json.loads((data_root / "runtime/install-state.json").read_text(encoding="utf-8"))
    assert state["core"] == {
        "release": "app/releases/0.1.0",
        "verified": True,
        "activated": True,
        "version": "0.1.0",
    }
    assert services.calls[-1] == "restore:rollback"


def test_install_state_activation_is_written_only_after_activated_barrier(
    tmp_path: Path,
) -> None:
    data_root, release = _layout(tmp_path)
    services = FakePublicationServices()
    publisher = WindowsInstallPublisher(data_root, release, services=services)
    original = publisher._write_install_state_for_current

    def assert_activated_barrier(*, activated: bool) -> None:
        publisher.journal.verify_critical("ACTIVATED")
        original(activated=activated)

    publisher._write_install_state_for_current = assert_activated_barrier  # type: ignore[method-assign]

    publisher.publish(lock_held=True)


def test_system_precommit_activation_uses_exact_file_nonce(tmp_path: Path) -> None:
    class Operations:
        def __init__(self) -> None:
            self.backend_environment_overrides: dict[str, str] = {}

    class Manager:
        def start(self, *, lock_held: bool) -> LifecycleResult:
            assert lock_held
            return LifecycleResult(0, "started")

        def stop(self, *, lock_held: bool) -> LifecycleResult:
            assert lock_held
            return LifecycleResult(0, "stopped")

    data_root, release = _layout(tmp_path)
    operations = Operations()
    services = SystemWindowsPublicationServices(
        data_root,
        release,
        operations=operations,  # type: ignore[arg-type]
        manager=Manager(),  # type: ignore[arg-type]
    )

    handle = services.start_precommit()

    assert operations.backend_environment_overrides == {
        "LVT_PRECOMMIT_ACTIVATION_FILE": str(handle.path),
        "LVT_PRECOMMIT_ACTIVATION_TOKEN": handle.token,
    }
    assert not handle.path.exists()

    services.activate(handle)

    assert handle.path.read_text(encoding="ascii") == f"{handle.token}\n"
    assert handle.activated
