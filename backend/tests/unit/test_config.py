import os
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


def test_from_env_defaults_runtime_paths_to_app_owned_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LVT_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("LVT_MODEL_ROOT", raising=False)
    monkeypatch.delenv("LVT_FFMPEG_DIR", raising=False)
    monkeypatch.delenv("LVT_OLLAMA_URL", raising=False)
    monkeypatch.delenv("LVT_INSTALLED_MODE", raising=False)

    settings = Settings.from_env()

    assert settings.model_root == tmp_path / "models"
    assert settings.ollama_url == "http://127.0.0.1:11435"
    assert settings.installed_mode is False
    assert settings.ffmpeg_dir is None
    assert settings.install_state == tmp_path / "runtime" / "install-state.json"


def test_installed_mode_requires_explicit_app_owned_ffmpeg_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LVT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LVT_INSTALLED_MODE", "1")
    monkeypatch.delenv("LVT_FFMPEG_DIR", raising=False)

    with pytest.raises(ValueError, match="LVT_FFMPEG_DIR"):
        Settings.from_env()


def test_installed_mode_rejects_user_ollama_and_external_model_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "data"
    monkeypatch.setenv("LVT_DATA_ROOT", str(data_root))
    monkeypatch.setenv("LVT_INSTALLED_MODE", "1")
    monkeypatch.setenv("LVT_FFMPEG_DIR", str(data_root / "app/tools/ffmpeg/8.0/bin"))
    monkeypatch.setenv("LVT_OLLAMA_URL", "http://127.0.0.1:11434")

    with pytest.raises(ValueError, match="11435"):
        Settings.from_env()

    monkeypatch.setenv("LVT_OLLAMA_URL", "http://127.0.0.1:11435")
    monkeypatch.setenv("LVT_MODEL_ROOT", str(tmp_path / "external-models"))
    with pytest.raises(ValueError, match="inside LVT_DATA_ROOT"):
        Settings.from_env()


def test_launcher_model_environment_overrides_ambient_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_root = tmp_path / "应用 数据" / "models"
    monkeypatch.setenv("HF_HOME", "/tmp/untrusted-hf-home")
    monkeypatch.setenv("HF_HUB_CACHE", "/tmp/untrusted-hf-cache")
    settings = Settings(data_root=tmp_path, model_root=model_root)

    settings.configure_model_environment()

    assert os.environ["HF_HOME"] == str(model_root / "huggingface")
    assert os.environ["HF_HUB_CACHE"] == str(model_root / "huggingface")
