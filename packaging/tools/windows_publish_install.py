from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from lifecycle_lock import LifecycleLock
from runtime_layout import path_is_link_like
from transaction_journal import JournalError, TransactionJournal
from windows_lifecycle import SystemWindowsLifecycleOperations, WindowsLifecycleManager
from windows_publication import NativeWindowsPublicationApi, rename_exclusive


class WindowsPublishError(RuntimeError):
    pass


class WindowsPublicationServices(Protocol):
    def validate_candidate(self, phase: str) -> bool: ...

    def start_precommit(self) -> object: ...

    def runtime_full(self) -> bool: ...

    def activate(self, handle: object) -> None: ...

    def healthy(self) -> bool: ...

    def stop_candidate(self) -> None: ...

    def ensure_committed_running(self) -> object: ...

    def restore_rollback(self) -> None: ...


@dataclass
class WindowsActivationHandle:
    path: Path
    token: str
    activated: bool = False


class SystemWindowsPublicationServices:
    def __init__(
        self,
        data_root: Path,
        release_root: Path,
        *,
        operations: SystemWindowsLifecycleOperations | None = None,
        manager: WindowsLifecycleManager | None = None,
    ) -> None:
        self.data_root = data_root
        self.release_root = release_root
        self.operations = operations or SystemWindowsLifecycleOperations(data_root, release_root)
        self.manager = manager or WindowsLifecycleManager(self.operations)
        self.activation_handle: WindowsActivationHandle | None = None
        self.diagnostic_stage = "idle"

    def _validate(self, phase: str) -> bool:
        python = self.release_root / ".venv" / "Scripts" / "python.exe"
        validator = self.release_root / "packaging" / "tools" / "verify_install.py"
        completed = subprocess.run(
            [
                str(python),
                str(validator),
                "--phase",
                phase,
                "--target",
                "windows-x64",
                "--data-root",
                str(self.data_root),
                "--release-root",
                str(self.release_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            close_fds=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False
        return (
            completed.returncode == 0
            and isinstance(payload, dict)
            and payload.get("exit_code") == 0
        )

    def validate_candidate(self, phase: str) -> bool:
        return self._validate(phase)

    def start_precommit(self) -> object:
        self.diagnostic_stage = "stop_existing"
        if self.activation_handle is not None:
            raise WindowsPublishError("candidate activation handle is already active")
        handle = WindowsActivationHandle(
            path=self.data_root / "runtime" / f".precommit-activation-{uuid.uuid4().hex}",
            token=secrets.token_hex(16),
        )
        if handle.path.exists() or path_is_link_like(handle.path):
            raise WindowsPublishError("candidate activation path is occupied")
        self.operations.backend_environment_overrides = {
            "LVT_PRECOMMIT_ACTIVATION_FILE": str(handle.path),
            "LVT_PRECOMMIT_ACTIVATION_TOKEN": handle.token,
        }
        try:
            self.manager.stop(lock_held=True)
            self.diagnostic_stage = "start_candidate"
            result = self.manager.start(lock_held=True)
            if result.exit_code != 0:
                raise WindowsPublishError("candidate services failed to start")
        except Exception:
            self.operations.backend_environment_overrides = {}
            raise
        self.activation_handle = handle
        return handle

    def runtime_full(self) -> bool:
        try:
            pointer = json.loads(
                (self.data_root / "app" / "current.json").read_text(encoding="ascii")
            )
            version = (self.release_root / "VERSION").read_text(encoding="utf-8").strip()
            manifest = json.loads(
                (self.data_root / "extension" / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return (
            pointer
            == {
                "release": self.release_root.relative_to(self.data_root).as_posix(),
                "version": version,
            }
            and isinstance(manifest, dict)
            and manifest.get("version") == version
            and self.operations.state("ollama") == "owned"
            and self.operations.state("backend") == "owned"
            and self.operations.backend_healthy()
        )

    def activate(self, handle: object) -> None:
        if not isinstance(handle, WindowsActivationHandle) or handle is not self.activation_handle:
            raise WindowsPublishError("candidate activation handle is unavailable")
        if handle.activated:
            return
        staged = handle.path.parent / f".{handle.path.name}.staged-{uuid.uuid4().hex}"
        descriptor = os.open(
            staged,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            content = f"{handle.token}\n".encode("ascii")
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise WindowsPublishError("activation write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            staged.rename(handle.path)
            _fsync_directory(handle.path.parent)
        finally:
            staged.unlink(missing_ok=True)
        handle.activated = True

    def healthy(self) -> bool:
        return self.operations.backend_healthy()

    def stop_candidate(self) -> None:
        try:
            result = self.manager.stop(lock_held=True)
            if result.exit_code != 0:
                raise WindowsPublishError("candidate services failed to stop")
        finally:
            if self.activation_handle is not None:
                self.activation_handle.path.unlink(missing_ok=True)
                self.activation_handle = None
            self.operations.backend_environment_overrides = {}

    def ensure_committed_running(self) -> object:
        with suppress(Exception):
            self.manager.stop(lock_held=True)
        return self.start_precommit()

    def restore_rollback(self) -> None:
        if not self.data_root.joinpath("app", "current.json").is_file():
            return
        try:
            pointer = json.loads(
                self.data_root.joinpath("app", "current.json").read_text(encoding="ascii")
            )
            relative = pointer["release"]
            if not isinstance(relative, str):
                raise ValueError
            release = self.data_root.joinpath(*relative.split("/")).resolve(strict=True)
            release.relative_to((self.data_root / "app" / "releases").resolve(strict=True))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WindowsPublishError("rollback release pointer is invalid") from exc
        operations = SystemWindowsLifecycleOperations(self.data_root, release)
        result = WindowsLifecycleManager(operations).start(lock_held=True)
        if result.exit_code != 0:
            raise WindowsPublishError("rollback services failed to restart")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    if path_is_link_like(root) or not root.is_dir():
        raise WindowsPublishError("publication tree is unsafe")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path_is_link_like(path):
            raise WindowsPublishError("publication tree contains a reparse point")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        else:
            raise WindowsPublishError("publication tree contains an unsafe entry")
    return digest.hexdigest()


def _identity(path: Path, component: str) -> dict[str, str]:
    if not path.exists() and not path_is_link_like(path):
        return {"kind": "absent"}
    if path_is_link_like(path):
        raise WindowsPublishError("publication path is a reparse point")
    if component == "current":
        if not path.is_file():
            raise WindowsPublishError("current publication path is unsafe")
        return {"kind": "file", "sha256": _sha256_file(path)}
    if not path.is_dir():
        raise WindowsPublishError("extension publication path is unsafe")
    return {"kind": "tree", "sha256": _tree_sha256(path)}


def _fsync_file(path: Path) -> None:
    access = os.O_RDWR if sys.platform == "win32" else os.O_RDONLY
    descriptor = os.open(path, access | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        api = NativeWindowsPublicationApi()
        handles = api.open_parent_chain(PureWindowsPath(str(path)))
        if not handles:
            raise WindowsPublishError("publication directory handle is unavailable")
        try:
            api.flush_directory(handles[-1])
        finally:
            for handle in reversed(handles):
                api.close_handle(handle)
        return
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
            if path_is_link_like(path) or not path.is_file():
                raise WindowsPublishError("publication tree is unsafe")
            _fsync_file(path)
        for name in directories:
            path = current_path / name
            if path_is_link_like(path) or not path.is_dir():
                raise WindowsPublishError("publication tree is unsafe")
            _fsync_directory(path)
        _fsync_directory(current_path)


class WindowsInstallPublisher:
    def __init__(
        self,
        data_root: Path,
        release_root: Path,
        *,
        services: WindowsPublicationServices | None = None,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if not data_root.is_absolute() or not release_root.is_absolute():
            raise WindowsPublishError("publication roots must be absolute")
        self.data_root = data_root
        self.release_root = release_root
        self.current = data_root / "app" / "current.json"
        self.current_next = data_root / "app" / "current.next.json"
        self.current_previous = data_root / "app" / "current.previous.json"
        self.extension = data_root / "extension"
        self.extension_next = data_root / "extension.next"
        self.extension_previous = data_root / "extension.previous"
        self.history = data_root / "runtime" / "publication-history"
        self.journal = TransactionJournal(data_root / "runtime" / "transaction-journal")
        self.services = services or SystemWindowsPublicationServices(data_root, release_root)
        self._failpoint = failpoint or (lambda _name: None)
        self.diagnostic_stage = "idle"

    def _diagnostic_stage(self) -> str:
        service_stage = getattr(self.services, "diagnostic_stage", "idle")
        operation_stage = getattr(
            getattr(self.services, "operations", None),
            "diagnostic_stage",
            "idle",
        )
        return ".".join(
            stage
            for stage in (self.diagnostic_stage, service_stage, operation_stage)
            if stage != "idle"
        )

    def _point(self, name: str) -> None:
        self._failpoint(name)

    def _current_bytes(self) -> bytes:
        version = (self.release_root / "VERSION").read_text(encoding="utf-8").strip()
        relative = self.release_root.relative_to(self.data_root).as_posix()
        return (
            json.dumps(
                {"release": relative, "version": version},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")

    def prepare_payload(self) -> dict[str, Any]:
        version = (self.release_root / "VERSION").read_text(encoding="utf-8").strip()
        current_bytes = self._current_bytes()
        return {
            "operation": "first_install" if not self.current.exists() else "upgrade",
            "transaction_id": str(uuid.uuid4()),
            "decision_id": str(uuid.uuid4()),
            "version": version,
            "state": "PREPARED",
            "decision": "pending",
            "paths": {
                "current": {
                    "live": "app/current.json",
                    "next": "app/current.next.json",
                    "previous": "app/current.previous.json",
                },
                "extension": {
                    "live": "extension",
                    "next": "extension.next",
                    "previous": "extension.previous",
                },
            },
            "identities": {
                "current": {
                    "old": _identity(self.current, "current"),
                    "new": {
                        "kind": "file",
                        "sha256": hashlib.sha256(current_bytes).hexdigest(),
                    },
                },
                "extension": {
                    "old": _identity(self.extension, "extension"),
                    "new": {
                        "kind": "tree",
                        "sha256": _tree_sha256(self.release_root / "extension"),
                    },
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

    def _paths(self, component: str) -> tuple[Path, Path, Path]:
        if component == "current":
            return self.current, self.current_next, self.current_previous
        if component == "extension":
            return self.extension, self.extension_next, self.extension_previous
        raise WindowsPublishError("unknown publication component")

    def _write_current_next(self) -> None:
        if self.current_next.exists() or path_is_link_like(self.current_next):
            raise WindowsPublishError("current next path is occupied")
        descriptor = os.open(
            self.current_next,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            view = memoryview(self._current_bytes())
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise WindowsPublishError("current pointer write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.current_next.parent)

    def _copy_extension_next(self) -> None:
        if self.extension_next.exists() or path_is_link_like(self.extension_next):
            raise WindowsPublishError("extension next path is occupied")
        source = self.release_root / "extension"
        if path_is_link_like(source) or not source.is_dir():
            raise WindowsPublishError("extension source is unsafe")
        shutil.copytree(source, self.extension_next, symlinks=False)
        _fsync_tree(self.extension_next)
        _fsync_directory(self.extension_next.parent)

    def _stage(self, payload: dict[str, Any]) -> None:
        self._write_current_next()
        self._copy_extension_next()
        if _identity(self.current_next, "current") != payload["identities"]["current"]["new"]:
            raise WindowsPublishError("current staging identity mismatch")
        if _identity(self.extension_next, "extension") != payload["identities"]["extension"]["new"]:
            raise WindowsPublishError("extension staging identity mismatch")

    def _move(self, source: Path, destination: Path) -> None:
        if not source.exists() or path_is_link_like(source):
            raise WindowsPublishError("publication source is unavailable")
        if destination.exists() or path_is_link_like(destination):
            raise WindowsPublishError("publication destination is occupied")
        if sys.platform == "win32":
            rename_exclusive(
                PureWindowsPath(str(source)),
                PureWindowsPath(str(destination)),
                NativeWindowsPublicationApi(),
            )
        else:
            source.rename(destination)
            _fsync_directory(destination.parent)

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
            updated["substate"] = {
                **payload["substate"],
                component: substate,
            }
        self.journal.write_progress(updated)
        return updated

    def _switch(self, component: str, payload: dict[str, Any]) -> dict[str, Any]:
        live, next_path, previous = self._paths(component)
        expected = payload["identities"][component]
        switching = "CURRENT_SWITCHING" if component == "current" else "EXTENSION_SWITCHING"
        switched = "CURRENT_SWITCHED" if component == "current" else "EXTENSION_SWITCHED"
        payload = self._progress(
            payload,
            state=switching,
            component=component,
            substate="intent_written",
        )
        if live.exists() or path_is_link_like(live):
            if _identity(live, component) != expected["old"]:
                raise WindowsPublishError("live publication identity changed")
            self._move(live, previous)
        payload = self._progress(
            payload,
            component=component,
            substate="old_to_previous_renamed",
        )
        if _identity(next_path, component) != expected["new"]:
            raise WindowsPublishError("next publication identity changed")
        self._move(next_path, live)
        if _identity(live, component) != expected["new"]:
            raise WindowsPublishError("published identity did not verify")
        return self._progress(
            payload,
            state=switched,
            component=component,
            substate="identity_verified",
        )

    def _retain(self, path: Path, component: str, transaction_id: str) -> None:
        if not path.exists() and not path_is_link_like(path):
            return
        self.history.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path_is_link_like(self.history):
            raise WindowsPublishError("publication history is unsafe")
        destination = self.history / (
            f"{transaction_id}-{component}-{path.name}-{uuid.uuid4().hex}"
        )
        self._move(path, destination)

    def _stage_component(self, component: str) -> None:
        if component == "current":
            self._write_current_next()
        else:
            self._copy_extension_next()

    def _converge_component(
        self,
        component: str,
        payload: dict[str, Any],
        *,
        committed: bool,
    ) -> None:
        live, next_path, previous = self._paths(component)
        identities = payload["identities"][component]
        desired = identities["new"] if committed else identities["old"]
        known = (identities["old"], identities["new"], {"kind": "absent"})
        observed = {
            "live": _identity(live, component),
            "next": _identity(next_path, component),
            "previous": _identity(previous, component),
        }
        if any(identity not in known for identity in observed.values()):
            raise WindowsPublishError("filesystem identity conflicts with journal")
        if observed["live"] != desired:
            if observed["live"] != {"kind": "absent"}:
                self._retain(live, component, payload["transaction_id"])
                observed["live"] = {"kind": "absent"}
            source = (
                next(
                    (
                        path
                        for label, path in (("next", next_path), ("previous", previous))
                        if observed[label] == desired
                    ),
                    None,
                )
                if desired != {"kind": "absent"}
                else None
            )
            if source is None and desired == identities["new"]:
                self._stage_component(component)
                source = next_path
            if source is not None:
                self._move(source, live)
        for path in (next_path, previous):
            if path.exists() or path_is_link_like(path):
                self._retain(path, component, payload["transaction_id"])
        if _identity(live, component) != desired:
            raise WindowsPublishError("publication convergence failed")

    def converge(self, payload: dict[str, Any], *, committed: bool) -> None:
        self._converge_component("current", payload, committed=committed)
        self._converge_component("extension", payload, committed=committed)

    def _write_install_state_for_current(self, *, activated: bool) -> None:
        path = self.data_root / "runtime" / "install-state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            core = state["core"]
            if not isinstance(state, dict) or not isinstance(core, dict):
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WindowsPublishError("install state is invalid") from exc
        if activated:
            try:
                pointer = json.loads(self.current.read_text(encoding="ascii"))
                if (
                    not isinstance(pointer, dict)
                    or set(pointer) != {"release", "version"}
                    or not isinstance(pointer["release"], str)
                    or not isinstance(pointer["version"], str)
                ):
                    raise ValueError
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise WindowsPublishError("current release pointer is invalid") from exc
            core = {
                "release": pointer["release"],
                "verified": True,
                "activated": True,
                "version": pointer["version"],
            }
        else:
            core = {key: value for key, value in core.items() if key != "activated"}
        state = {**state, "core": core}
        encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        temporary = path.parent / f".install-state.activate-{uuid.uuid4().hex}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise WindowsPublishError("install state write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _finalize_activated(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.converge(payload, committed=True)
        activated = {
            **payload,
            "state": "ACTIVATED",
            "decision": "activated",
            "substate": {**payload["substate"], "cleanup": "complete"},
        }
        self.journal.write_critical(activated)
        self._write_install_state_for_current(activated=True)
        return activated

    def _finalize_rollback(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.converge(payload, committed=False)
        self._write_install_state_for_current(activated=self.current.exists())
        if self.current.exists() and hasattr(self.services, "restore_rollback"):
            self.services.restore_rollback()
        rolled_back = {
            **payload,
            "state": "ROLLED_BACK",
            "decision": "rolled_back",
            "substate": {**payload["substate"], "cleanup": "complete"},
        }
        self.journal.write_critical(rolled_back)
        return rolled_back

    def publish(self, *, lock_held: bool = False) -> None:
        if not lock_held:
            lock = LifecycleLock(self.data_root / "app", operation="install")
            lock.acquire_flock(blocking=True)
            try:
                self.publish(lock_held=True)
                return
            finally:
                lock.close()
        self.diagnostic_stage = "reconcile"
        self.reconcile(lock_held=True)
        self.diagnostic_stage = "validate_staging_core"
        if not self.services.validate_candidate("staging-core"):
            raise WindowsPublishError("staging-core validation failed")
        self.diagnostic_stage = "validate_dependencies"
        if not self.services.validate_candidate("dependencies"):
            raise WindowsPublishError("dependencies validation failed")
        self.diagnostic_stage = "prepare"
        payload = self.prepare_payload()
        self.journal.write_progress(payload)
        handle: object | None = None
        try:
            self.diagnostic_stage = "stage"
            self._stage(payload)
            self.diagnostic_stage = "switch_current"
            payload = self._switch("current", payload)
            self.diagnostic_stage = "switch_extension"
            payload = self._switch("extension", payload)
            self.diagnostic_stage = "start_precommit"
            handle = self.services.start_precommit()
            self.diagnostic_stage = "record_precommit"
            payload = self._progress(payload, state="SERVICE_PRECOMMIT_READY")
            self.diagnostic_stage = "runtime_full"
            if not self.services.runtime_full():
                raise WindowsPublishError("runtime-full validation failed")
            committed = {
                **payload,
                "state": "COMMITTED",
                "decision": "committed",
            }
            self.diagnostic_stage = "commit"
            self.journal.write_critical(committed)
            payload = committed
            self._point("activation:before_install_state")
            self.diagnostic_stage = "activate"
            self.services.activate(handle)
            self.diagnostic_stage = "health"
            if not self.services.healthy():
                raise WindowsPublishError("activated service health failed")
            activated = {
                **payload,
                "state": "ACTIVATED",
                "decision": "activated",
                "substate": {**payload["substate"], "cleanup": "intent_written"},
            }
            self.diagnostic_stage = "record_activated"
            self.journal.write_critical(activated)
            self._write_install_state_for_current(activated=True)
            self.diagnostic_stage = "finalize_activated"
            self._finalize_activated(activated)
        except Exception as publish_error:
            recovery_errors: list[Exception] = []
            try:
                direction = self.journal.committed_direction()
            except Exception as direction_error:
                direction = None
                recovery_errors.append(direction_error)
            if direction == "rollback":
                if handle is not None:
                    try:
                        self.services.stop_candidate()
                    except Exception as stop_error:
                        recovery_errors.append(stop_error)
                try:
                    self._finalize_rollback(payload)
                except Exception as rollback_error:
                    recovery_errors.append(rollback_error)
            if recovery_errors:
                raise ExceptionGroup(
                    "Windows publication and recovery failed",
                    [publish_error, *recovery_errors],
                ) from publish_error
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
        except JournalError as exc:
            raise WindowsPublishError(str(exc)) from exc
        if latest is None:
            return
        direction = self.journal.committed_direction()
        if direction == "rollback":
            if latest.payload["state"] == "ROLLED_BACK":
                try:
                    self.journal.verify_critical("ROLLED_BACK")
                except JournalError:
                    self.journal.repair_critical("ROLLED_BACK")
                return
            with suppress(Exception):
                self.services.stop_candidate()
            self._finalize_rollback(latest.payload)
            return
        if latest.payload["state"] == "ACTIVATED":
            try:
                self.journal.verify_critical("ACTIVATED")
            except JournalError:
                self.journal.repair_critical("ACTIVATED")
            self._write_install_state_for_current(activated=True)
            return
        self.converge(latest.payload, committed=True)
        handle = self.services.ensure_committed_running()
        self.services.activate(handle)
        if not self.services.healthy():
            raise WindowsPublishError("committed service health failed")
        activated = {
            **latest.payload,
            "state": "ACTIVATED",
            "decision": "activated",
            "substate": {**latest.payload["substate"], "cleanup": "intent_written"},
        }
        self.journal.write_critical(activated)
        self._write_install_state_for_current(activated=True)
        self._finalize_activated(activated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish Local Video Transcriber on Windows")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    arguments = parser.parse_args(argv)
    publisher = WindowsInstallPublisher(arguments.data_root, arguments.release_root)
    try:
        publisher.publish()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error_class": type(exc).__name__,
                    "error_stage": publisher._diagnostic_stage(),
                    "schema_version": 1,
                    "status": "unsafe_or_corrupt",
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps({"schema_version": 1, "status": "activated"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
