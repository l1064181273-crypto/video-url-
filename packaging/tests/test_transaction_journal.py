from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "packaging" / "tools"
sys.path.insert(0, str(TOOLS))

from transaction_journal import (  # noqa: E402
    JournalError,
    TransactionJournal,
    canonical_json,
)


def _payload(*, state: str = "PREPARED", decision_id: str | None = None) -> dict[str, Any]:
    return {
        "operation": "first_install",
        "transaction_id": str(uuid.uuid4()),
        "decision_id": decision_id or str(uuid.uuid4()),
        "version": "0.1.0",
        "state": state,
        "decision": "committed" if state in {"COMMITTED", "ACTIVATED"} else "pending",
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
            "current": {
                "old": {"kind": "absent"},
                "new": {
                    "kind": "symlink",
                    "target": "releases/0.1.0",
                    "sha256": "1" * 64,
                },
            },
            "extension": {
                "old": {"kind": "absent"},
                "new": {"kind": "tree", "sha256": "2" * 64},
            },
        },
        "substate": {
            "current": "intent_written",
            "extension": "intent_written",
            "cleanup": "pending",
        },
    }


def test_progress_uses_alternating_slots_and_recovers_from_damage(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    first = journal.write_progress(_payload())
    second_payload = {**first.payload, "state": "CURRENT_SWITCHING"}
    second = journal.write_progress(second_payload)

    assert first.generation == 1
    assert second.generation == 2
    assert {path.name for path in journal.root.glob("slot-*.json")} == {
        "slot-a.json",
        "slot-b.json",
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in journal.root.glob("slot-*.json"))

    second.path.write_bytes(b'{"truncated":')
    recovered = journal.read_latest()
    assert recovered is not None
    assert recovered.generation == 1


def test_progress_ignores_lagging_valid_slot(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    first = journal.write_progress(_payload())
    latest = journal.write_progress({**first.payload, "state": "CURRENT_SWITCHED"})

    recovered = journal.read_latest()
    assert recovered is not None
    assert recovered.generation == latest.generation
    assert recovered.payload["state"] == "CURRENT_SWITCHED"


def test_committed_barrier_writes_two_independent_consecutive_copies(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    prepared = journal.write_progress(_payload())
    committed_payload = {
        **prepared.payload,
        "state": "COMMITTED",
        "decision": "committed",
    }

    first, second = journal.write_critical(committed_payload)
    verified = journal.verify_critical("COMMITTED")

    assert [item.generation for item in verified] == [
        first.generation,
        second.generation,
    ]
    assert second.generation == first.generation + 1
    assert first.payload == second.payload == committed_payload
    raw = [json.loads(item.path.read_text(encoding="utf-8")) for item in verified]
    assert raw[0]["checksum"] != raw[1]["checksum"]


@pytest.mark.parametrize("damaged_slot", ["slot-a.json", "slot-b.json"])
@pytest.mark.parametrize("damage", ["delete", "truncate"])
def test_single_committed_copy_selects_new_direction_then_repairs_before_gate(
    tmp_path: Path,
    damaged_slot: str,
    damage: str,
) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    payload = _payload(state="COMMITTED")
    journal.write_critical(payload)
    path = journal.root / damaged_slot
    if damage == "delete":
        path.unlink()
    else:
        path.write_bytes(b"{")

    assert journal.committed_direction() == "committed"
    with pytest.raises(JournalError, match="critical barrier"):
        journal.verify_critical("COMMITTED")

    repaired = journal.repair_critical("COMMITTED")
    assert repaired[1].generation == repaired[0].generation + 1
    assert repaired[0].payload == repaired[1].payload == payload


def test_conflicting_valid_critical_copies_fail_closed(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    first = _payload(state="COMMITTED")
    journal.write_critical(first)
    conflicting = {
        **first,
        "decision_id": str(uuid.uuid4()),
        "version": "0.1.1",
    }
    journal._write_slot(journal.root / "slot-b.json", 3, conflicting)

    with pytest.raises(JournalError, match="conflicting critical decisions"):
        journal.read_latest()
    with pytest.raises(JournalError, match="conflicting critical decisions"):
        journal.repair_critical("COMMITTED")


@pytest.mark.parametrize(
    "forbidden",
    [
        {"token": "secret"},
        {"environment": {"HOME": "/private/user"}},
        {"argv": ["python", "secret"]},
        {"exception": "raw traceback"},
        {"media_path": "/Users/example/movie.mp4"},
    ],
)
def test_journal_rejects_forbidden_or_absolute_payload_data(
    tmp_path: Path,
    forbidden: dict[str, Any],
) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    payload = _payload()
    payload["substate"] = {**payload["substate"], **forbidden}

    with pytest.raises(JournalError):
        journal.write_progress(payload)
    assert not journal.root.exists() or not list(journal.root.glob("slot-*.json"))


def test_canonical_json_is_stable_and_compact() -> None:
    assert canonical_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


@pytest.mark.parametrize(
    "boundary",
    [
        "before_temp_write",
        "after_temp_write",
        "before_file_fsync",
        "after_file_fsync",
        "before_slot_rename",
        "after_slot_rename",
        "before_directory_fsync",
        "after_directory_fsync",
    ],
)
def test_each_slot_persistence_boundary_is_failpointed(
    tmp_path: Path,
    boundary: str,
) -> None:
    observed: list[str] = []

    def failpoint(name: str) -> None:
        observed.append(name)
        if name == f"slot-a:{boundary}":
            raise RuntimeError("simulated crash")

    journal = TransactionJournal(tmp_path / "journal", failpoint=failpoint)
    with pytest.raises(RuntimeError, match="simulated crash"):
        journal.write_progress(_payload())

    assert f"slot-a:{boundary}" in observed
