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


def _payload(
    *,
    state: str = "PREPARED",
    decision_id: str | None = None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    decision = {
        "COMMITTED": "committed",
        "ACTIVATED": "activated",
    }.get(state, "pending")
    cleanup = "intent_written" if state == "ACTIVATED" else "pending"
    return {
        "operation": "first_install",
        "transaction_id": transaction_id or str(uuid.uuid4()),
        "decision_id": decision_id or str(uuid.uuid4()),
        "version": "0.1.0",
        "state": state,
        "decision": decision,
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
            "cleanup": cleanup,
        },
        "recovery": {
            "component": "none",
            "action": "none",
            "phase": "idle",
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
    journal.write_progress(
        _payload(
            transaction_id=payload["transaction_id"],
            decision_id=payload["decision_id"],
        )
    )
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
    journal.write_progress(
        _payload(
            transaction_id=first["transaction_id"],
            decision_id=first["decision_id"],
        )
    )
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


def test_activated_requires_durable_committed_barrier(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    prepared = journal.write_progress(_payload())
    activated = {
        **prepared.payload,
        "state": "ACTIVATED",
        "decision": "activated",
        "substate": {**prepared.payload["substate"], "cleanup": "intent_written"},
    }

    with pytest.raises(JournalError, match="COMMITTED"):
        journal.write_critical(activated)


def test_windows_current_pointer_file_identity_is_valid(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    payload = _payload()
    payload["paths"]["current"] = {
        "live": "app/current.json",
        "next": "app/current.next.json",
        "previous": "app/current.previous.json",
    }
    payload["identities"]["current"]["new"] = {
        "kind": "file",
        "sha256": "3" * 64,
    }

    entry = journal.write_progress(payload)

    assert entry.payload["identities"]["current"]["new"]["kind"] == "file"


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


@pytest.mark.parametrize("damage", ["delete", "truncate", "checksum", "schema"])
def test_existing_journal_with_zero_valid_slots_fails_closed(
    tmp_path: Path,
    damage: str,
) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    journal.write_progress(_payload())
    for path in journal.root.glob("slot-*.json"):
        if damage == "delete":
            path.unlink()
        elif damage == "truncate":
            path.write_bytes(b"{")
        else:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if damage == "checksum":
                envelope["checksum"] = "0" * 64
            else:
                envelope["schema_version"] = 999
            path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(JournalError, match="corrupt"):
        journal.read_latest()
    with pytest.raises(JournalError, match="corrupt"):
        journal.committed_direction()


def test_new_transaction_is_not_contaminated_by_previous_activation(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    old = _payload(state="ACTIVATED")
    old["substate"]["cleanup"] = "complete"
    journal.write_progress(
        _payload(
            transaction_id=old["transaction_id"],
            decision_id=old["decision_id"],
        )
    )
    journal.write_critical(
        {
            **old,
            "state": "COMMITTED",
            "decision": "committed",
            "substate": {**old["substate"], "cleanup": "pending"},
        }
    )
    journal.write_critical(old)
    new = _payload(state="PREPARED")

    journal.write_progress(new)

    latest = journal.read_latest()
    assert latest is not None
    assert latest.payload["transaction_id"] == new["transaction_id"]
    assert journal.committed_direction() == "rollback"


def test_partial_activated_cleanup_copy_repairs_newest_adjacent_progress(
    tmp_path: Path,
) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    activated = _payload(state="ACTIVATED")
    journal.write_progress(
        _payload(
            transaction_id=activated["transaction_id"],
            decision_id=activated["decision_id"],
        )
    )
    journal.write_critical(
        {
            **activated,
            "state": "COMMITTED",
            "decision": "committed",
            "substate": {**activated["substate"], "cleanup": "pending"},
        }
    )
    first, second = journal.write_critical(activated)
    progressed = {
        **activated,
        "substate": {**activated["substate"], "cleanup": "parent_synced"},
    }
    journal._write_slot(first.path, second.generation + 1, progressed)

    assert journal.committed_direction() == "committed"
    repaired = journal.repair_critical("ACTIVATED")
    assert repaired[0].payload == repaired[1].payload == progressed


def test_partial_critical_recovery_copy_repairs_newest_adjacent_progress(
    tmp_path: Path,
) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    committed = _payload(state="COMMITTED")
    journal.write_progress(
        _payload(
            transaction_id=committed["transaction_id"],
            decision_id=committed["decision_id"],
        )
    )
    first, second = journal.write_critical(committed)
    recovering = {
        **committed,
        "recovery": {
            "component": "current",
            "action": "remove_previous",
            "phase": "intent_written",
        },
    }
    journal._write_slot(first.path, second.generation + 1, recovering)

    assert journal.committed_direction() == "committed"
    repaired = journal.repair_critical("COMMITTED")
    assert repaired[0].payload == repaired[1].payload == recovering


@pytest.mark.parametrize(
    ("state", "decision", "cleanup"),
    [
        ("PREPARED", "committed", "pending"),
        ("ACTIVATED", "activated", "pending"),
        ("COMMITTED", "pending", "pending"),
    ],
)
def test_illegal_state_decision_substate_combinations_are_rejected(
    tmp_path: Path,
    state: str,
    decision: str,
    cleanup: str,
) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    payload = _payload()
    payload["state"] = state
    payload["decision"] = decision
    payload["substate"]["cleanup"] = cleanup

    with pytest.raises(JournalError):
        journal.write_progress(payload)


def test_same_transaction_cannot_regress_from_critical_to_progress(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path / "journal")
    transaction_id = str(uuid.uuid4())
    committed = _payload(state="COMMITTED", transaction_id=transaction_id)
    journal.write_progress(
        _payload(
            transaction_id=transaction_id,
            decision_id=committed["decision_id"],
        )
    )
    journal.write_critical(committed)

    with pytest.raises(JournalError, match="transition"):
        journal.write_progress(
            _payload(
                transaction_id=transaction_id,
                decision_id=committed["decision_id"],
            )
        )


def test_new_journal_directories_have_parent_fsync_failpoints(tmp_path: Path) -> None:
    observed: list[str] = []
    root = tmp_path / "one" / "two" / "journal"
    journal = TransactionJournal(root, failpoint=observed.append)

    journal.write_progress(_payload())

    assert "root:before_mkdir:one" in observed
    assert "root:after_mkdir:one" in observed
    assert "root:before_parent_fsync:one" in observed
    assert "root:after_parent_fsync:one" in observed
    assert "root:before_parent_fsync:journal" in observed
    assert "root:after_parent_fsync:journal" in observed
