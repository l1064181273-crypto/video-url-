from __future__ import annotations

import os
from pathlib import Path

import pytest

from lvt.core.instance_lock import InstanceAlreadyRunningError, ProcessInstanceLock


def test_instance_lock_is_exclusive_and_reusable_after_release(tmp_path: Path) -> None:
    path = tmp_path / "backend.instance.lock"
    owner = ProcessInstanceLock(path)
    contender = ProcessInstanceLock(path)

    owner.acquire()
    assert owner.acquired
    assert path.read_bytes() == f"pid={os.getpid()}\n".encode("ascii")
    with pytest.raises(InstanceAlreadyRunningError, match="already running"):
        contender.acquire()

    owner.release()
    owner.release()
    contender.acquire()
    assert contender.acquired
    contender.release()
