from pathlib import Path

import pytest

from lvt.core.config import Settings


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_settings_allows_loopback_hosts(host: str) -> None:
    assert Settings(data_root=Path("/tmp/lvt"), host=host).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "localhost", "8.8.8.8"])
def test_settings_rejects_non_literal_loopback_hosts(host: str) -> None:
    with pytest.raises(ValueError, match="127.0.0.1 or ::1"):
        Settings(data_root=Path("/tmp/lvt"), host=host)


def test_from_env_rejects_external_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LVT_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="127.0.0.1 or ::1"):
        Settings.from_env()


@pytest.mark.parametrize("worker_concurrency", [1, 2])
def test_settings_allows_supported_worker_concurrency(
    worker_concurrency: int, tmp_path: Path
) -> None:
    assert (
        Settings(data_root=tmp_path, worker_concurrency=worker_concurrency).worker_concurrency
        == worker_concurrency
    )


@pytest.mark.parametrize("worker_concurrency", [-1, 0, 3, 10])
def test_settings_rejects_unsupported_worker_concurrency(
    worker_concurrency: int, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="1 or 2"):
        Settings(data_root=tmp_path, worker_concurrency=worker_concurrency)


def test_from_env_reads_worker_concurrency(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LVT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LVT_WORKER_CONCURRENCY", "2")

    assert Settings.from_env().worker_concurrency == 2
