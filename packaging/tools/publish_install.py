#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lifecycle_lock import LifecycleLock
from transaction_journal import JournalError, TransactionJournal


class PublishError(RuntimeError):
    pass


class PublicationServices(Protocol):
    def validate_candidate(self, phase: str) -> bool: ...

    def start_precommit(self) -> object: ...

    def runtime_full(self) -> bool: ...

    def activate(self, handle: object) -> None: ...

    def healthy(self) -> bool: ...

    def stop_candidate(self) -> None: ...

    def copy_token(self, token_path: Path) -> None: ...


@dataclass
class ActivationHandle:
    write_fd: int
    created_ollama: bool
    activated: bool = False


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise PublishError("extension candidate contains an unsafe file")
            _fsync_file(path)
        for name in directories:
            path = current_path / name
            if path.is_symlink() or not path.is_dir():
                raise PublishError("extension candidate contains an unsafe directory")
            _fsync_directory(path)
        _fsync_directory(current_path)


def tree_identity(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_dir():
        raise PublishError("tree identity source is unsafe")
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = candidate.relative_to(path).as_posix()
        metadata = candidate.lstat()
        if candidate.is_symlink() or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            raise PublishError("tree identity contains an unsafe entry")
        digest.update(b"D" if stat.S_ISDIR(metadata.st_mode) else b"F")
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{metadata.st_mode & 0o777:o}".encode("ascii"))
        digest.update(b"\0")
        if candidate.is_file():
            with candidate.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        digest.update(b"\0")
    return {"kind": "tree", "sha256": digest.hexdigest()}


def symlink_identity(path: Path) -> dict[str, str]:
    if not path.is_symlink():
        raise PublishError("symlink identity source is unsafe")
    target = os.readlink(path)
    if Path(target).is_absolute() or ".." in Path(target).parts:
        raise PublishError("symlink target is unsafe")
    return {
        "kind": "symlink",
        "target": target,
        "sha256": _sha256_bytes(target.encode("utf-8")),
    }


def path_identity(path: Path, kind: str) -> dict[str, str]:
    if not path.exists() and not path.is_symlink():
        return {"kind": "absent"}
    return symlink_identity(path) if kind == "current" else tree_identity(path)


def prepare_runtime_files(source_root: Path, release_root: Path) -> None:
    source_root = source_root.resolve(strict=True)
    release_root = release_root.resolve(strict=True)
    files = (
        ("packaging/tools/process_state.py", 0o755),
        ("packaging/tools/transaction_journal.py", 0o755),
        ("packaging/tools/publish_install.py", 0o755),
        ("packaging/schemas/transaction-journal-v1.schema.json", 0o644),
    )
    for relative, mode in files:
        source = source_root / relative
        destination = release_root / relative
        source_metadata = source.lstat()
        if source.is_symlink() or not stat.S_ISREG(source_metadata.st_mode):
            raise PublishError("CP7 runtime source is unsafe")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() or destination.is_symlink():
            if (
                destination.is_symlink()
                or not destination.is_file()
                or _file_sha256(destination) != _file_sha256(source)
            ):
                raise PublishError("installed CP7 runtime differs from the release")
            continue
        temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
        shutil.copyfile(source, temporary)
        temporary.chmod(mode)
        _fsync_file(temporary)
        temporary.rename(destination)
        _fsync_directory(destination.parent)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class FirstInstallPublisher:
    def __init__(
        self,
        data_root: Path,
        release_root: Path,
        *,
        services: PublicationServices | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if not data_root.is_absolute() or not release_root.is_absolute():
            raise PublishError("publication roots must be absolute")
        self.data_root = data_root
        self.release_root = release_root
        self.current = data_root / "app/current"
        self.current_next = data_root / "app/current.next"
        self.current_previous = data_root / "app/current.previous"
        self.extension = data_root / "extension"
        self.extension_next = data_root / "extension.next"
        self.extension_previous = data_root / "extension.previous"
        self.journal = TransactionJournal(data_root / "runtime/transaction-journal")
        self.services = services or SystemPublicationServices(data_root, release_root)
        self._failpoint = failpoint or (lambda _name: None)

    def publish(self, *, lock_held: bool = False) -> None:
        if not lock_held:
            lock = LifecycleLock(self.data_root / "app", operation="install")
            lock.acquire_flock(blocking=True)
            try:
                self.publish(lock_held=True)
                return
            finally:
                lock.close()

        self.reconcile(lock_held=True)
        if not self.services.validate_candidate("staging-core"):
            raise PublishError("staging-core validation failed")
        if not self.services.validate_candidate("dependencies"):
            raise PublishError("dependencies validation failed")
        payload = self.prepare_payload()
        self.journal.write_progress(payload)
        handle: object | None = None
        try:
            payload = self._switch("current", payload)
            payload = self._switch("extension", payload)
            handle = self.services.start_precommit()
            payload = self._progress(payload, state="SERVICE_PRECOMMIT_READY")
            if not self.services.runtime_full():
                raise PublishError("runtime-full validation failed")
            payload = {
                **payload,
                "state": "COMMITTED",
                "decision": "committed",
            }
            self.journal.write_critical(payload)
            self.journal.verify_critical("COMMITTED")
            self.services.activate(handle)
            if not self.services.healthy():
                raise PublishError("activated service health failed")
            payload = {
                **payload,
                "state": "ACTIVATED",
                "decision": "activated",
                "substate": {**payload["substate"], "cleanup": "intent_written"},
            }
            self._point("cleanup", "before_intent")
            self.journal.write_critical(payload)
            self._point("cleanup", "after_intent")
            self._cleanup_previous(payload)
            self.services.copy_token(self.data_root / "config/api-token")
        except Exception:
            if self.journal.committed_direction() == "rollback":
                if handle is not None:
                    self.services.stop_candidate()
                self.converge_payload(payload, committed=False)
                self._finalize_rollback(payload)
            raise

    def reconcile(self, *, lock_held: bool = False) -> None:
        if not lock_held:
            lock = LifecycleLock(self.data_root / "app", operation="install")
            lock.acquire_flock(blocking=True)
            try:
                self.reconcile(lock_held=True)
                return
            finally:
                lock.close()
        try:
            latest = self.journal.read_latest()
            if latest is None:
                return
            committed = self.journal.committed_direction() == "committed"
        except JournalError as exc:
            raise PublishError(str(exc)) from exc
        if not committed:
            self.services.stop_candidate()
            self.converge_payload(latest.payload, committed=False)
            self._finalize_rollback(latest.payload)
            return

        state = str(latest.payload["state"])
        critical_state = "ACTIVATED" if state == "ACTIVATED" else "COMMITTED"
        try:
            self.journal.verify_critical(critical_state)
        except JournalError:
            try:
                self.journal.repair_critical(critical_state)
            except JournalError as exc:
                raise PublishError(str(exc)) from exc
        self.converge_payload(latest.payload, committed=True)
        if critical_state == "COMMITTED":
            handle = self.services.start_precommit()
            self.services.activate(handle)
            if not self.services.healthy():
                raise PublishError("recovered service health failed")
            activated = {
                **latest.payload,
                "state": "ACTIVATED",
                "decision": "activated",
                "substate": {
                    **latest.payload["substate"],
                    "cleanup": "intent_written",
                },
            }
            self._point("cleanup", "before_intent")
            self.journal.write_critical(activated)
            self._point("cleanup", "after_intent")
            latest_payload = activated
        else:
            latest_payload = latest.payload
        self._cleanup_previous(latest_payload)

    def prepare_payload(
        self,
        *,
        old_current_target: str | None = None,
        old_extension_identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            version = (self.release_root / "VERSION").read_text(encoding="utf-8").strip()
            self.release_root.resolve(strict=True).relative_to(
                (self.data_root / "app/releases").resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise PublishError("release candidate is unsafe") from exc
        current_target = self.release_root.relative_to(self.data_root / "app").as_posix()
        new_current = {
            "kind": "symlink",
            "target": current_target,
            "sha256": _sha256_bytes(current_target.encode("utf-8")),
        }
        if old_current_target is not None:
            old_current = {
                "kind": "symlink",
                "target": old_current_target,
                "sha256": _sha256_bytes(old_current_target.encode("utf-8")),
            }
        else:
            old_current = path_identity(self.current, "current")
        old_extension = (
            old_extension_identity
            if old_extension_identity is not None
            else path_identity(self.extension, "extension")
        )
        return {
            "operation": "first_install",
            "transaction_id": str(uuid.uuid4()),
            "decision_id": str(uuid.uuid4()),
            "version": version,
            "state": "PREPARED",
            "decision": "pending",
            "paths": {
                "current": {
                    "live": "app/current",
                    "next": "app/current.next",
                    "previous": "app/current.previous",
                },
                "extension": {
                    "live": "extension",
                    "next": "extension.next",
                    "previous": "extension.previous",
                },
            },
            "identities": {
                "current": {"old": old_current, "new": new_current},
                "extension": {
                    "old": old_extension,
                    "new": tree_identity(self.release_root / "extension"),
                },
            },
            "substate": {
                "current": "pending",
                "extension": "pending",
                "cleanup": "pending",
            },
            "recovery": {
                "component": "none",
                "action": "none",
                "phase": "idle",
            },
        }

    def converge(self, *, committed: bool) -> None:
        latest = self.journal.read_latest()
        if latest is None:
            raise PublishError("journal is unavailable")
        self.converge_payload(latest.payload, committed=committed)

    def converge_payload(
        self,
        payload: dict[str, Any],
        *,
        committed: bool,
    ) -> dict[str, Any]:
        payload = self._resume_recovery(payload)
        payload = self._converge_component("current", payload, committed)
        return self._converge_component("extension", payload, committed)

    def copy_tree(self, source: Path, destination: Path, *, sync: bool = True) -> None:
        if destination.exists() or destination.is_symlink():
            raise PublishError("tree destination already exists")
        if source.is_symlink() or not source.is_dir():
            raise PublishError("tree source is unsafe")
        shutil.copytree(source, destination, symlinks=False)
        if sync:
            _fsync_tree(destination)
            _fsync_directory(destination.parent)

    def filesystem_snapshot(self) -> dict[str, dict[str, str]]:
        return {
            "current": path_identity(self.current, "current"),
            "current_next": path_identity(self.current_next, "current"),
            "current_previous": path_identity(self.current_previous, "current"),
            "extension": path_identity(self.extension, "extension"),
            "extension_next": path_identity(self.extension_next, "extension"),
            "extension_previous": path_identity(self.extension_previous, "extension"),
        }

    def _switch(self, component: str, payload: dict[str, Any]) -> dict[str, Any]:
        live, next_path, previous = self._paths(component)
        switching = "CURRENT_SWITCHING" if component == "current" else "EXTENSION_SWITCHING"
        switched = "CURRENT_SWITCHED" if component == "current" else "EXTENSION_SWITCHED"
        self._point(component, "before_intent")
        payload = self._progress(
            payload,
            state=switching,
            component=component,
            substate="intent_written",
        )
        self._point(component, "after_intent")
        self._point(component, "before_next_prepare")
        if component == "current":
            next_path.symlink_to(payload["identities"]["current"]["new"]["target"])
        else:
            self.copy_tree(self.release_root / "extension", next_path, sync=False)
            self._point(component, "before_next_file_fsync")
            _fsync_tree(next_path)
            self._point(component, "after_next_file_fsync")
        payload = self._progress(payload, component=component, substate="next_prepared")
        self._point(component, "after_next_prepare")
        self._point(component, "before_next_parent_fsync")
        _fsync_directory(next_path.parent)
        payload = self._progress(payload, component=component, substate="next_parent_synced")
        self._point(component, "after_next_parent_fsync")

        self._point(component, "before_old_rename")
        if live.exists() or live.is_symlink():
            if previous.exists() or previous.is_symlink():
                raise PublishError("previous publication path is occupied")
            live.rename(previous)
        payload = self._progress(
            payload,
            component=component,
            substate="old_to_previous_renamed",
        )
        self._point(component, "after_old_rename")
        self._point(component, "before_old_parent_fsync")
        _fsync_directory(live.parent)
        payload = self._progress(
            payload,
            component=component,
            substate="parent_synced_after_old",
        )
        self._point(component, "after_old_parent_fsync")

        self._point(component, "before_live_rename")
        next_path.rename(live)
        payload = self._progress(
            payload,
            component=component,
            substate="next_to_live_renamed",
        )
        self._point(component, "after_live_rename")
        self._point(component, "before_live_parent_fsync")
        _fsync_directory(live.parent)
        payload = self._progress(
            payload,
            component=component,
            substate="parent_synced_after_live",
        )
        self._point(component, "after_live_parent_fsync")
        if path_identity(live, component) != payload["identities"][component]["new"]:
            raise PublishError("published identity did not verify")
        return self._progress(
            payload,
            state=switched,
            component=component,
            substate="identity_verified",
        )

    def _converge_component(
        self,
        component: str,
        payload: dict[str, Any],
        committed: bool,
    ) -> dict[str, Any]:
        live, next_path, previous = self._paths(component)
        identities = payload["identities"][component]
        desired = identities["new"] if committed else identities["old"]
        known = (identities["old"], identities["new"], {"kind": "absent"})
        observed = self._observed_component(component)
        if any(identity not in known for identity in observed.values()):
            raise PublishError("filesystem identity conflicts with journal")
        if desired["kind"] == "absent":
            for label, path in (
                ("live", live),
                ("next", next_path),
                ("previous", previous),
            ):
                if path_identity(path, component) != {"kind": "absent"}:
                    payload = self._durable_recovery_action(
                        payload,
                        component,
                        f"remove_{label}",
                    )
            return payload

        source = next((path for path, identity in observed.items() if identity == desired), None)
        if source is None:
            if desired != identities["new"]:
                raise PublishError("old publication artifact is unavailable")
            if path_identity(live, component) != {"kind": "absent"}:
                payload = self._durable_recovery_action(payload, component, "remove_live")
            payload = self._durable_recovery_action(payload, component, "rebuild_live")
        elif source != live:
            if path_identity(live, component) != {"kind": "absent"}:
                payload = self._durable_recovery_action(payload, component, "remove_live")
            source_label = "next" if source == next_path else "previous"
            payload = self._durable_recovery_action(
                payload,
                component,
                f"rename_{source_label}_to_live",
            )
        for label, path in (("next", next_path), ("previous", previous)):
            if path_identity(path, component) != {"kind": "absent"}:
                payload = self._durable_recovery_action(
                    payload,
                    component,
                    f"remove_{label}",
                )
        if path_identity(live, component) != desired:
            raise PublishError("publication convergence failed")
        return payload

    def _observed_component(self, component: str) -> dict[Path, dict[str, str]]:
        live, next_path, previous = self._paths(component)
        return {
            live: path_identity(live, component),
            next_path: path_identity(next_path, component),
            previous: path_identity(previous, component),
        }

    def _durable_recovery_action(
        self,
        payload: dict[str, Any],
        component: str,
        action: str,
    ) -> dict[str, Any]:
        point = f"recovery:{component}:{action}"
        self._point(point, "before_intent")
        payload = self._record_recovery(payload, component, action, "intent_written")
        self._point(point, "after_intent")
        return self._resume_recovery(payload)

    def _resume_recovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        recovery = payload["recovery"]
        if recovery["phase"] == "idle":
            return payload
        component = str(recovery["component"])
        action = str(recovery["action"])
        phase = str(recovery["phase"])
        point = f"recovery:{component}:{action}"
        if phase == "intent_written":
            self._point(point, "before_effect")
            self._apply_recovery_effect(component, action, payload)
            self._point(point, "after_effect")
            payload = self._record_recovery(
                payload,
                component,
                action,
                "effect_observed",
            )
            phase = "effect_observed"
        if phase == "effect_observed":
            self._point(point, "before_file_fsync")
            self._sync_recovery_artifact(component, action)
            self._point(point, "after_file_fsync")
            payload = self._record_recovery(payload, component, action, "file_synced")
            phase = "file_synced"
        if phase == "file_synced":
            parent = self._paths(component)[0].parent
            self._point(point, "before_parent_fsync")
            _fsync_directory(parent)
            self._point(point, "after_parent_fsync")
            payload = self._record_recovery(payload, component, action, "parent_synced")
        return self._record_recovery(payload, "none", "none", "idle")

    def _apply_recovery_effect(
        self,
        component: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        live, next_path, previous = self._paths(component)
        by_label = {"live": live, "next": next_path, "previous": previous}
        identities = payload["identities"][component]
        known = (identities["old"], identities["new"], {"kind": "absent"})
        if action.startswith("remove_"):
            self._remove_known(by_label[action.removeprefix("remove_")], component, known)
            return
        if action.startswith("rename_") and action.endswith("_to_live"):
            source = by_label[action.removeprefix("rename_").removesuffix("_to_live")]
            live_identity = path_identity(live, component)
            source_identity = path_identity(source, component)
            if live_identity in (identities["new"], identities["old"]) and source_identity == {
                "kind": "absent"
            }:
                return
            if live_identity != {"kind": "absent"}:
                raise PublishError("recovery rename destination is occupied")
            if source_identity == {"kind": "absent"}:
                raise PublishError("recovery rename source is unavailable")
            source.rename(live)
            return
        if action == "rebuild_live":
            expected = identities["new"]
            if path_identity(live, component) == expected:
                return
            if path_identity(live, component) != {"kind": "absent"}:
                raise PublishError("recovery rebuild destination is occupied")
            self._rebuild_new(component, live, payload, sync=False)
            return
        raise PublishError("unknown recovery action")

    def _sync_recovery_artifact(self, component: str, action: str) -> None:
        if action != "rebuild_live":
            return
        live = self._paths(component)[0]
        if component == "extension":
            _fsync_tree(live)

    def _rebuild_new(
        self,
        component: str,
        live: Path,
        payload: dict[str, Any],
        *,
        sync: bool = True,
    ) -> None:
        if component == "current":
            live.symlink_to(payload["identities"]["current"]["new"]["target"])
            if sync:
                _fsync_directory(live.parent)
        else:
            self.copy_tree(self.release_root / "extension", live, sync=sync)

    def _remove_known(
        self,
        path: Path,
        component: str,
        known: tuple[dict[str, str], ...],
    ) -> None:
        identity = path_identity(path, component)
        if identity == {"kind": "absent"}:
            return
        if identity not in known:
            raise PublishError("refusing to remove an unknown publication identity")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _cleanup_previous(self, payload: dict[str, Any]) -> None:
        latest = self.journal.read_latest()
        if latest is not None and latest.payload["transaction_id"] == payload["transaction_id"]:
            payload = latest.payload
        stages = {
            "intent_written": 0,
            "current_removed": 1,
            "current_parent_synced": 2,
            "extension_removed": 3,
            "parent_synced": 4,
            "complete": 5,
        }
        stage = stages[payload["substate"]["cleanup"]]
        if stage < 1:
            payload = self._cleanup_remove("current", self.current_previous, payload)
            stage = 1
        if stage < 2:
            payload = self._cleanup_sync_parent("current", self.current_previous, payload)
            stage = 2
        if stage < 3:
            payload = self._cleanup_remove("extension", self.extension_previous, payload)
            stage = 3
        if stage < 4:
            payload = self._cleanup_sync_parent("extension", self.extension_previous, payload)
        if payload["substate"]["cleanup"] != "complete":
            self._point("cleanup", "before_complete")
            payload = self._with_cleanup(payload, "complete")
            self.journal.write_critical(payload)
            self._point("cleanup", "after_complete")

    def _cleanup_remove(
        self,
        component: str,
        path: Path,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._point(f"cleanup:{component}", "before_remove")
        identities = payload["identities"][component]
        known = (identities["old"], identities["new"], {"kind": "absent"})
        self._remove_known(path, component, known)
        self._point(f"cleanup:{component}", "after_remove")
        substate = "current_removed" if component == "current" else "extension_removed"
        updated = self._with_cleanup(payload, substate)
        self.journal.write_critical(updated)
        return updated

    def _cleanup_sync_parent(
        self,
        component: str,
        path: Path,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._point(f"cleanup:{component}", "before_parent_fsync")
        _fsync_directory(path.parent)
        self._point(f"cleanup:{component}", "after_parent_fsync")
        substate = "current_parent_synced" if component == "current" else "parent_synced"
        updated = self._with_cleanup(payload, substate)
        self.journal.write_critical(updated)
        return updated

    @staticmethod
    def _with_cleanup(payload: dict[str, Any], cleanup: str) -> dict[str, Any]:
        return {
            **payload,
            "substate": {**payload["substate"], "cleanup": cleanup},
        }

    def _record_recovery(
        self,
        payload: dict[str, Any],
        component: str,
        action: str,
        phase: str,
    ) -> dict[str, Any]:
        updated = {
            **payload,
            "recovery": {
                "component": component,
                "action": action,
                "phase": phase,
            },
        }
        if updated["state"] in {"COMMITTED", "ACTIVATED", "ROLLED_BACK"}:
            self.journal.write_critical(updated)
        else:
            self.journal.write_progress(updated)
        return updated

    def _finalize_rollback(self, payload: dict[str, Any]) -> None:
        latest = self.journal.read_latest()
        if latest is not None and latest.payload["transaction_id"] == payload["transaction_id"]:
            payload = latest.payload
        rolled_back = {
            **payload,
            "state": "ROLLED_BACK",
            "decision": "rolled_back",
            "substate": {**payload["substate"], "cleanup": "complete"},
            "recovery": {
                "component": "none",
                "action": "none",
                "phase": "idle",
            },
        }
        self.journal.write_critical(rolled_back)

    def _progress(
        self,
        payload: dict[str, Any],
        *,
        state: str | None = None,
        component: str | None = None,
        substate: str | None = None,
    ) -> dict[str, Any]:
        updated = dict(payload)
        if state is not None:
            updated["state"] = state
        if component is not None and substate is not None:
            updated["substate"] = {**payload["substate"], component: substate}
        self.journal.write_progress(updated)
        return updated

    def _paths(self, component: str) -> tuple[Path, Path, Path]:
        if component == "current":
            return self.current, self.current_next, self.current_previous
        if component == "extension":
            return self.extension, self.extension_next, self.extension_previous
        raise PublishError("unknown publication component")

    def _point(self, component: str, boundary: str) -> None:
        self._failpoint(f"{component}:{boundary}")


class SystemPublicationServices:
    def __init__(self, data_root: Path, release_root: Path) -> None:
        self.data_root = data_root
        self.release_root = release_root
        self._active_handle: ActivationHandle | None = None

    def validate_candidate(self, phase: str) -> bool:
        return self._validate(phase, self.release_root)

    def start_precommit(self) -> object:
        from process_state import ServiceError, SystemServiceOperations

        operations = SystemServiceOperations(self.data_root, self.release_root)
        backend_state = operations.state("backend")
        if backend_state == "unsafe":
            raise PublishError("backend ownership is unsafe")
        if backend_state == "owned":
            operations.stop("backend")
        ollama_state = operations.state("ollama")
        if ollama_state == "unsafe":
            raise PublishError("project Ollama ownership is unsafe")
        created_ollama = False
        activation_read, activation_write = os.pipe()
        try:
            if ollama_state == "absent":
                operations.launch("ollama")
                created_ollama = True
            operations.launch("backend", activation_read)
            if not operations.backend_healthy():
                raise PublishError("precommit backend health failed")
        except (OSError, ServiceError, PublishError) as start_error:
            with suppress(OSError):
                os.close(activation_write)
            cleanup_errors: list[Exception] = []
            for kind, should_stop in (
                ("backend", operations.state("backend") == "owned"),
                (
                    "ollama",
                    created_ollama and operations.state("ollama") == "owned",
                ),
            ):
                if not should_stop:
                    continue
                try:
                    operations.stop(kind)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExceptionGroup(
                    "precommit start and cleanup failed",
                    [start_error, *cleanup_errors],
                ) from start_error
            raise
        finally:
            os.close(activation_read)
        handle = ActivationHandle(activation_write, created_ollama)
        self._active_handle = handle
        return handle

    def runtime_full(self) -> bool:
        return self._validate("runtime-full", self.release_root)

    def activate(self, handle: object) -> None:
        if not isinstance(handle, ActivationHandle) or handle is not self._active_handle:
            raise PublishError("activation handle is invalid")
        if handle.activated:
            return
        os.write(handle.write_fd, b"A")
        os.close(handle.write_fd)
        handle.write_fd = -1
        handle.activated = True

    def healthy(self) -> bool:
        from process_state import SystemServiceOperations

        return SystemServiceOperations(self.data_root, self.release_root).backend_healthy()

    def stop_candidate(self) -> None:
        from process_state import SystemServiceOperations

        operations = SystemServiceOperations(self.data_root, self.release_root)
        if self._active_handle is not None and self._active_handle.write_fd >= 0:
            with suppress(OSError):
                os.close(self._active_handle.write_fd)
            self._active_handle.write_fd = -1
        cleanup_errors: list[Exception] = []
        for kind, should_stop in (
            ("backend", operations.state("backend") == "owned"),
            (
                "ollama",
                self._active_handle is not None
                and self._active_handle.created_ollama
                and operations.state("ollama") == "owned",
            ),
        ):
            if not should_stop:
                continue
            try:
                operations.stop(kind)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise ExceptionGroup("candidate cleanup failed", cleanup_errors)

    def copy_token(self, token_path: Path) -> None:
        completed = subprocess_run_pbcopy(token_path)
        if completed != 0:
            raise PublishError("clipboard copy failed")

    def _validate(self, phase: str, release: Path) -> bool:
        import subprocess

        completed = subprocess.run(
            [
                str(release / ".venv/bin/python"),
                str(release / "packaging/tools/verify_install.py"),
                "--phase",
                phase,
                "--data-root",
                str(self.data_root),
                "--release-root",
                str(release),
                "--json",
            ],
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return completed.returncode == 0 and report.get("status") == "healthy"


def subprocess_run_pbcopy(token_path: Path) -> int:
    import subprocess

    with token_path.open("rb") as stream:
        completed = subprocess.run(
            ["/usr/bin/pbcopy"],
            stdin=stream,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            check=False,
        )
    return completed.returncode


def chrome_connection_instructions(data_root: Path) -> tuple[str, str, str]:
    extension = data_root / "extension"
    rendered = str(extension)
    home = str(Path.home())
    if rendered == home:
        rendered = "~"
    elif rendered.startswith(f"{home}/"):
        rendered = f"~/{rendered[len(home) + 1 :]}"
    return (
        f"CHROME_EXTENSION_PATH：{rendered}",
        "CHROME_LOAD_UNPACKED：在 chrome://extensions 中加载上述目录",
        "TOKEN_COPIED：连接 Token 已安全复制到剪贴板",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布 Local Video Transcriber 首次安装")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        prepare_runtime_files(Path(__file__).resolve().parents[2], arguments.release_root)
        FirstInstallPublisher(arguments.data_root, arguments.release_root).publish()
    except Exception:
        print("[ERROR] FIRST_INSTALL_PUBLISH_FAILED：首次发布未完成", file=sys.stderr)
        return 2
    print("[INFO] FIRST_INSTALL_PUBLISHED：首次发布和服务激活完成")
    for line in chrome_connection_instructions(arguments.data_root):
        print(f"[INFO] {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
