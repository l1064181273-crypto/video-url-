#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from runtime_layout import path_is_link_like

SCHEMA_VERSION = 1
SLOTS = ("slot-a.json", "slot-b.json")
JOURNAL_MARKER = "journal-v1"
STATES = {
    "PREPARED",
    "CURRENT_SWITCHING",
    "CURRENT_SWITCHED",
    "EXTENSION_SWITCHING",
    "EXTENSION_SWITCHED",
    "SERVICE_PRECOMMIT_READY",
    "COMMITTED",
    "ACTIVATED",
    "ROLLED_BACK",
}
CRITICAL_STATES = {"COMMITTED", "ACTIVATED", "ROLLED_BACK"}
STATE_ORDER = {
    "PREPARED": 0,
    "CURRENT_SWITCHING": 1,
    "CURRENT_SWITCHED": 2,
    "EXTENSION_SWITCHING": 3,
    "EXTENSION_SWITCHED": 4,
    "SERVICE_PRECOMMIT_READY": 5,
    "COMMITTED": 6,
    "ACTIVATED": 7,
    "ROLLED_BACK": 8,
}
SWITCH_SUBSTATES = {
    "pending",
    "intent_written",
    "next_prepared",
    "next_parent_synced",
    "old_to_previous_renamed",
    "parent_synced_after_old",
    "next_to_live_renamed",
    "parent_synced_after_live",
    "identity_verified",
}
SWITCH_SUBSTATE_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "pending",
            "intent_written",
            "next_prepared",
            "next_parent_synced",
            "old_to_previous_renamed",
            "parent_synced_after_old",
            "next_to_live_renamed",
            "parent_synced_after_live",
            "identity_verified",
        )
    )
}
CLEANUP_SUBSTATE_ORDER = {
    "pending": 0,
    "intent_written": 1,
    "current_removed": 2,
    "current_parent_synced": 3,
    "extension_removed": 4,
    "parent_synced": 5,
    "complete": 6,
}
FORBIDDEN_KEYS = {
    "argv",
    "environment",
    "env",
    "exception",
    "media_path",
    "token",
    "traceback",
}
PAYLOAD_KEYS = {
    "operation",
    "transaction_id",
    "decision_id",
    "version",
    "state",
    "decision",
    "paths",
    "identities",
    "substate",
    "recovery",
}


class JournalError(RuntimeError):
    pass


@dataclass(frozen=True)
class JournalEntry:
    path: Path
    generation: int
    payload: dict[str, Any]
    checksum: str


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _checksum(generation: int, payload: dict[str, Any]) -> str:
    return _sha256(
        canonical_json(
            {
                "schema_version": SCHEMA_VERSION,
                "generation": generation,
                "payload": payload,
            }
        )
    )


def _relative_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and not PurePosixPath(value).is_absolute()
        and not PureWindowsPath(value).is_absolute()
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _validate_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _validate_identity(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("kind") not in {
        "absent",
        "file",
        "symlink",
        "tree",
    }:
        return False
    if value["kind"] == "absent":
        return set(value) == {"kind"}
    if value["kind"] == "file":
        return (
            set(value) == {"kind", "sha256"}
            and isinstance(value.get("sha256"), str)
            and len(value["sha256"]) == 64
        )
    if value["kind"] == "symlink":
        return (
            set(value) == {"kind", "target", "sha256"}
            and _relative_path(value.get("target"))
            and isinstance(value.get("sha256"), str)
            and len(value["sha256"]) == 64
        )
    return (
        set(value) == {"kind", "sha256"}
        and isinstance(value.get("sha256"), str)
        and len(value["sha256"]) == 64
    )


def _contains_forbidden(value: Any, *, key: str | None = None) -> bool:
    if key is not None and key.lower() in FORBIDDEN_KEYS:
        return True
    if isinstance(value, str):
        return value.startswith("/") or PureWindowsPath(value).is_absolute()
    if isinstance(value, dict):
        return any(_contains_forbidden(child, key=str(name)) for name, child in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(child) for child in value)
    return False


def validate_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        raise JournalError("journal payload fields are invalid")
    if (
        payload.get("operation") not in {"first_install", "upgrade"}
        or not _validate_uuid(payload.get("transaction_id"))
        or not _validate_uuid(payload.get("decision_id"))
        or not isinstance(payload.get("version"), str)
        or not payload["version"]
        or payload.get("state") not in STATES
        or payload.get("decision") not in {"pending", "committed", "activated", "rolled_back"}
    ):
        raise JournalError("journal transaction metadata is invalid")
    if _contains_forbidden(payload):
        raise JournalError("journal payload contains forbidden data")

    paths = payload.get("paths")
    if not isinstance(paths, dict) or set(paths) != {"current", "extension"}:
        raise JournalError("journal paths are invalid")
    for name in ("current", "extension"):
        group = paths.get(name)
        if (
            not isinstance(group, dict)
            or set(group) != {"live", "next", "previous"}
            or not all(_relative_path(item) for item in group.values())
        ):
            raise JournalError("journal paths are invalid")

    identities = payload.get("identities")
    if not isinstance(identities, dict) or set(identities) != {"current", "extension"}:
        raise JournalError("journal identities are invalid")
    for name in ("current", "extension"):
        group = identities.get(name)
        if (
            not isinstance(group, dict)
            or set(group) != {"old", "new"}
            or not _validate_identity(group.get("old"))
            or not _validate_identity(group.get("new"))
        ):
            raise JournalError("journal identities are invalid")

    substate = payload.get("substate")
    if (
        not isinstance(substate, dict)
        or set(substate) != {"current", "extension", "cleanup"}
        or substate.get("current") not in SWITCH_SUBSTATES
        or substate.get("extension") not in SWITCH_SUBSTATES
        or substate.get("cleanup")
        not in {
            "pending",
            "intent_written",
            "current_removed",
            "current_parent_synced",
            "extension_removed",
            "parent_synced",
            "complete",
        }
    ):
        raise JournalError("journal substate is invalid")
    recovery = payload.get("recovery")
    if (
        not isinstance(recovery, dict)
        or set(recovery) != {"component", "action", "phase"}
        or recovery.get("component") not in {"none", "current", "extension"}
        or not isinstance(recovery.get("action"), str)
        or not recovery["action"]
        or recovery.get("phase")
        not in {"idle", "intent_written", "effect_observed", "file_synced", "parent_synced"}
        or (recovery["component"] == "none") != (recovery["action"] == "none")
        or (recovery["component"] == "none") != (recovery["phase"] == "idle")
    ):
        raise JournalError("journal recovery metadata is invalid")
    state = str(payload["state"])
    decision = str(payload["decision"])
    cleanup = str(substate["cleanup"])
    if (
        (state in STATES - CRITICAL_STATES and decision != "pending")
        or (state == "COMMITTED" and (decision != "committed" or cleanup != "pending"))
        or (
            state == "ACTIVATED"
            and (
                decision != "activated"
                or cleanup
                not in {
                    "intent_written",
                    "current_removed",
                    "current_parent_synced",
                    "extension_removed",
                    "parent_synced",
                    "complete",
                }
            )
        )
        or (state == "ROLLED_BACK" and (decision != "rolled_back" or cleanup != "complete"))
    ):
        raise JournalError("journal state combination is invalid")


class TransactionJournal:
    def __init__(
        self,
        root: Path,
        *,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if not root.is_absolute():
            raise JournalError("journal root must be absolute")
        self.root = root
        self._failpoint = failpoint or (lambda _name: None)

    def read_latest(self) -> JournalEntry | None:
        entries = self._valid_entries()
        active = self._active_entries(entries)
        self._reject_critical_conflict(active)
        return max(active, key=lambda item: item.generation, default=None)

    def committed_direction(self) -> str:
        entries = self._valid_entries()
        entries = self._active_entries(entries)
        self._reject_critical_conflict(entries)
        return (
            "committed"
            if any(item.payload["state"] in {"COMMITTED", "ACTIVATED"} for item in entries)
            else "rollback"
        )

    def write_progress(self, payload: dict[str, Any]) -> JournalEntry:
        validate_payload(payload)
        if payload["state"] in CRITICAL_STATES:
            raise JournalError("critical state requires a double-copy barrier")
        entries = self._valid_entries()
        active = self._active_entries(entries)
        self._reject_critical_conflict(active)
        self._validate_transition(active, payload)
        generation = max((item.generation for item in entries), default=0) + 1
        path = self._inactive_slot(entries)
        return self._write_slot(path, generation, payload)

    def write_critical(
        self,
        payload: dict[str, Any],
    ) -> tuple[JournalEntry, JournalEntry]:
        validate_payload(payload)
        if payload["state"] not in CRITICAL_STATES:
            raise JournalError("journal state is not a critical decision")
        entries = self._valid_entries()
        active = self._active_entries(entries)
        self._reject_critical_conflict(active)
        self._validate_transition(active, payload)
        generation = max((item.generation for item in entries), default=0) + 1
        first_path = self.root / SLOTS[0]
        second_path = self.root / SLOTS[1]
        self._write_slot(first_path, generation, payload)
        self._write_slot(second_path, generation + 1, payload)
        return self.verify_critical(str(payload["state"]))

    def verify_critical(self, state: str) -> tuple[JournalEntry, JournalEntry]:
        if state not in CRITICAL_STATES:
            raise JournalError("unknown critical state")
        entries = self._valid_entries()
        entries = self._active_entries(entries)
        self._reject_critical_conflict(entries)
        selected = sorted(
            (item for item in entries if item.payload["state"] == state),
            key=lambda item: item.generation,
        )
        if (
            len(selected) != 2
            or selected[1].generation != selected[0].generation + 1
            or selected[0].payload != selected[1].payload
        ):
            raise JournalError("critical barrier is incomplete")
        return selected[0], selected[1]

    def repair_critical(self, state: str) -> tuple[JournalEntry, JournalEntry]:
        entries = self._valid_entries()
        entries = self._active_entries(entries)
        self._reject_critical_conflict(entries)
        matching = [item for item in entries if item.payload["state"] == state]
        if not matching:
            raise JournalError("critical decision is unavailable")
        payload = max(matching, key=lambda item: item.generation).payload
        return self.write_critical(payload)

    def _prepare_root(self) -> None:
        current = Path(self.root.anchor)
        for part in self.root.parts[1:]:
            current /= part
            if path_is_link_like(current):
                raise JournalError("journal root contains a symlink")
            if not current.exists():
                self._failpoint(f"root:before_mkdir:{current.name}")
                current.mkdir(mode=0o700)
                self._failpoint(f"root:after_mkdir:{current.name}")
                self._failpoint(f"root:before_parent_fsync:{current.name}")
                _fsync_directory(current.parent)
                self._failpoint(f"root:after_parent_fsync:{current.name}")
        if path_is_link_like(self.root) or not self.root.is_dir():
            raise JournalError("journal root is unsafe")
        self.root.chmod(0o700)
        marker = self.root / JOURNAL_MARKER
        if not marker.exists():
            self._failpoint("root:before_marker_write")
            descriptor = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                view = memoryview(b"1\n")
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise JournalError("journal marker write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._failpoint("root:after_marker_write")
            _fsync_directory(self.root)
        elif path_is_link_like(marker) or marker.read_bytes() != b"1\n":
            raise JournalError("journal marker is corrupt")

    def _valid_entries(self) -> list[JournalEntry]:
        if not self.root.exists():
            return []
        if path_is_link_like(self.root) or not self.root.is_dir():
            raise JournalError("journal root is unsafe")
        marker = self.root / JOURNAL_MARKER
        if path_is_link_like(marker) or not marker.is_file() or marker.read_bytes() != b"1\n":
            raise JournalError("journal marker is corrupt")
        entries: list[JournalEntry] = []
        for name in SLOTS:
            path = self.root / name
            entry = self._read_slot(path)
            if entry is not None:
                entries.append(entry)
        if not entries:
            raise JournalError("journal slots are corrupt")
        return entries

    def _read_slot(self, path: Path) -> JournalEntry | None:
        try:
            metadata = path.lstat()
            if (
                path_is_link_like(path)
                or not stat.S_ISREG(metadata.st_mode)
                or not _owned_by_current_user(metadata)
                or metadata.st_nlink != 1
                or (sys.platform != "win32" and metadata.st_mode & 0o777 != 0o600)
            ):
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"schema_version", "generation", "payload", "checksum"}
                or envelope.get("schema_version") != SCHEMA_VERSION
                or type(envelope.get("generation")) is not int
                or envelope["generation"] <= 0
                or not isinstance(envelope.get("payload"), dict)
                or not isinstance(envelope.get("checksum"), str)
            ):
                return None
            payload = envelope["payload"]
            validate_payload(payload)
            expected = _checksum(envelope["generation"], payload)
            if envelope["checksum"] != expected:
                return None
            return JournalEntry(path, envelope["generation"], payload, expected)
        except (OSError, ValueError, json.JSONDecodeError, JournalError):
            return None

    def _inactive_slot(self, entries: list[JournalEntry]) -> Path:
        by_name = {item.path.name: item for item in entries}
        missing = next((name for name in SLOTS if name not in by_name), None)
        if missing is not None:
            return self.root / missing
        return min(entries, key=lambda item: item.generation).path

    def _write_slot(
        self,
        path: Path,
        generation: int,
        payload: dict[str, Any],
    ) -> JournalEntry:
        validate_payload(payload)
        if type(generation) is not int or generation <= 0:
            raise JournalError("journal generation is invalid")
        self._prepare_root()
        if path.parent != self.root or path.name not in SLOTS:
            raise JournalError("journal slot path is invalid")
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "payload": payload,
            "checksum": _checksum(generation, payload),
        }
        encoded = canonical_json(envelope) + b"\n"
        temporary = self.root / f".{path.name}.tmp-{uuid.uuid4().hex}"
        self._call(path, "before_temp_write")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            self._call(path, "after_temp_write")
            self._call(path, "before_file_fsync")
            os.fsync(descriptor)
            self._call(path, "after_file_fsync")
        finally:
            os.close(descriptor)
        try:
            self._call(path, "before_slot_rename")
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            self._call(path, "after_slot_rename")
            self._call(path, "before_directory_fsync")
            _fsync_directory(self.root)
            self._call(path, "after_directory_fsync")
        finally:
            temporary.unlink(missing_ok=True)
        entry = self._read_slot(path)
        if entry is None:
            raise JournalError("journal slot failed post-write validation")
        return entry

    def _call(self, path: Path, boundary: str) -> None:
        self._failpoint(f"{path.stem}:{boundary}")

    @staticmethod
    def _active_entries(entries: list[JournalEntry]) -> list[JournalEntry]:
        if not entries:
            return []
        latest = max(entries, key=lambda item: item.generation)
        transaction_id = latest.payload["transaction_id"]
        active = [item for item in entries if item.payload["transaction_id"] == transaction_id]
        older = [item for item in entries if item.payload["transaction_id"] != transaction_id]
        if older:
            if latest.payload["state"] != "PREPARED" or latest.payload["decision"] != "pending":
                raise JournalError("conflicting journal transaction boundary")
            for item in older:
                if not (
                    item.payload["state"] in {"ACTIVATED", "ROLLED_BACK"}
                    and item.payload["decision"] in {"activated", "rolled_back"}
                    and item.payload["substate"]["cleanup"] == "complete"
                ):
                    raise JournalError("previous journal transaction is not finalized")
        return active

    @staticmethod
    def _validate_transition(entries: list[JournalEntry], payload: dict[str, Any]) -> None:
        if not entries:
            if payload["state"] != "PREPARED" or payload["decision"] != "pending":
                raise JournalError("journal transaction must start prepared")
            return
        if payload["state"] == "ACTIVATED" and not any(
            item.payload["state"] in {"COMMITTED", "ACTIVATED"}
            and item.payload["decision"] in {"committed", "activated"}
            for item in entries
        ):
            raise JournalError("ACTIVATED requires a durable COMMITTED decision")
        latest = max(entries, key=lambda item: item.generation).payload
        if latest["transaction_id"] != payload["transaction_id"]:
            if not (
                (
                    (latest["state"] == "ACTIVATED" and latest["decision"] == "activated")
                    or (latest["state"] == "ROLLED_BACK" and latest["decision"] == "rolled_back")
                )
                and latest["substate"]["cleanup"] == "complete"
                and payload["state"] == "PREPARED"
                and payload["decision"] == "pending"
            ):
                raise JournalError("journal transaction transition is invalid")
            return
        immutable = PAYLOAD_KEYS - {"state", "decision", "substate", "recovery"}
        if any(latest[key] != payload[key] for key in immutable):
            raise JournalError("journal decision identity changed")
        if (
            latest["decision"] in {"committed", "activated"}
            and payload["decision"] == "rolled_back"
        ):
            raise JournalError("journal transition regressed")
        if STATE_ORDER[payload["state"]] < STATE_ORDER[latest["state"]]:
            raise JournalError("journal transition regressed")
        decision_order = {"pending": 0, "committed": 1, "activated": 2, "rolled_back": 2}
        if decision_order[payload["decision"]] < decision_order[latest["decision"]]:
            raise JournalError("journal transition regressed")
        for component in ("current", "extension"):
            if (
                SWITCH_SUBSTATE_ORDER[payload["substate"][component]]
                < SWITCH_SUBSTATE_ORDER[latest["substate"][component]]
            ):
                raise JournalError("journal transition regressed")
        if (
            CLEANUP_SUBSTATE_ORDER[payload["substate"]["cleanup"]]
            < CLEANUP_SUBSTATE_ORDER[latest["substate"]["cleanup"]]
        ):
            raise JournalError("journal transition regressed")
        previous_recovery = latest["recovery"]
        next_recovery = payload["recovery"]
        recovery_order = {
            "idle": 0,
            "intent_written": 1,
            "effect_observed": 2,
            "file_synced": 3,
            "parent_synced": 4,
        }
        if (
            previous_recovery["component"],
            previous_recovery["action"],
        ) == (next_recovery["component"], next_recovery["action"]):
            if recovery_order[next_recovery["phase"]] < recovery_order[
                previous_recovery["phase"]
            ] and not (
                previous_recovery["phase"] == "parent_synced" and next_recovery["phase"] == "idle"
            ):
                raise JournalError("journal recovery transition regressed")
        elif not (
            previous_recovery["phase"] in {"idle", "parent_synced"}
            and next_recovery["phase"] in {"idle", "intent_written"}
        ):
            raise JournalError("journal recovery transition is invalid")

    @classmethod
    def _reject_critical_conflict(cls, entries: list[JournalEntry]) -> None:
        critical = [item for item in entries if item.payload["state"] in CRITICAL_STATES]
        if len(critical) != 2:
            return
        left, right = sorted(critical, key=lambda item: item.generation)
        comparable_keys = PAYLOAD_KEYS - {"state", "decision", "substate", "recovery"}
        if any(left.payload[key] != right.payload[key] for key in comparable_keys):
            raise JournalError("conflicting critical decisions")
        try:
            cls._validate_transition([left], right.payload)
        except JournalError as exc:
            raise JournalError("conflicting critical decisions") from exc


def _fsync_directory(path: Path) -> None:
    if sys.platform == "win32":
        from windows_publication import NativeWindowsPublicationApi

        api = NativeWindowsPublicationApi()
        handles = api.open_parent_chain(PureWindowsPath(str(path)))
        if not handles:
            raise JournalError("journal directory handle is unavailable")
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


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    getuid = getattr(os, "geteuid", None)
    return getuid is None or metadata.st_uid == getuid()
