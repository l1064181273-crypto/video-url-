from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lvt.api.app import create_app
from lvt.workers.runner import FileActivationBarrier


def _wait_closed(barrier: FileActivationBarrier) -> None:
    assert barrier.wait_closed(2)
    barrier.close()


def test_file_activation_barrier_waits_for_exact_nonce(tmp_path: Path) -> None:
    path = tmp_path / "activation"
    barrier = FileActivationBarrier(path, "a" * 32)
    barrier.start()

    time.sleep(0.05)
    assert not barrier.activated
    staged = tmp_path / "activation.staged"
    staged.write_text("a" * 32 + "\n", encoding="ascii")
    staged.replace(path)
    _wait_closed(barrier)

    assert barrier.activated
    assert barrier.activation_count == 1


def test_file_activation_barrier_rejects_wrong_content(tmp_path: Path) -> None:
    path = tmp_path / "activation"
    path.write_text("b" * 32 + "\n", encoding="ascii")
    barrier = FileActivationBarrier(path, "a" * 32)
    barrier.start()
    _wait_closed(barrier)

    assert not barrier.activated
    assert barrier.activation_count == 0


def test_file_activation_barrier_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("a" * 32 + "\n", encoding="ascii")
    path = tmp_path / "activation"
    path.symlink_to(target)
    barrier = FileActivationBarrier(path, "a" * 32)
    barrier.start()
    _wait_closed(barrier)

    assert not barrier.activated


def test_file_activation_barrier_detects_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "activation"
    path.write_text("a" * 32 + "\n", encoding="ascii")
    replacement = tmp_path / "replacement"
    replacement.write_text("a" * 32 + "\n", encoding="ascii")
    original_read = os.read
    replaced = False

    def replace_after_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        content = original_read(descriptor, count)
        if not replaced:
            replaced = True
            replacement.replace(path)
        return content

    monkeypatch.setattr(os, "read", replace_after_read)
    barrier = FileActivationBarrier(path, "a" * 32)
    barrier.start()
    _wait_closed(barrier)

    assert not barrier.activated


def test_app_selects_file_activation_barrier_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "activation"
    monkeypatch.setenv("LVT_PRECOMMIT_ACTIVATION_FILE", str(path))
    monkeypatch.setenv("LVT_PRECOMMIT_ACTIVATION_TOKEN", "c" * 32)

    app = create_app(db_path=tmp_path / "db.sqlite3", api_token="t" * 32)

    assert isinstance(app.state.activation_barrier, FileActivationBarrier)


def test_app_rejects_partial_file_activation_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LVT_PRECOMMIT_ACTIVATION_FILE", str(tmp_path / "activation"))

    with pytest.raises(ValueError, match="configured together"):
        create_app(db_path=tmp_path / "db.sqlite3", api_token="t" * 32)
