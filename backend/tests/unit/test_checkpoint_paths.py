from pathlib import Path

import pytest

from lvt.pipeline.checkpoints import CheckpointStore


def test_macos_var_alias_is_canonicalized_for_trusted_work_root(
    tmp_path: Path,
) -> None:
    resolved = tmp_path.resolve()
    if not str(resolved).startswith("/private/var/"):
        pytest.skip("macOS /var alias is not present on this filesystem")
    alias = Path("/var") / resolved.relative_to("/private/var") / "work"

    store = CheckpointStore(alias)

    assert store.work_root == (resolved / "work").resolve()
