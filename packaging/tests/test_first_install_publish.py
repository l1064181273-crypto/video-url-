from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging" / "tools"))

from publish_install import (  # noqa: E402
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
