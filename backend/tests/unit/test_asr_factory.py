from __future__ import annotations

from pathlib import Path
from typing import Any

from lvt.core.platform_runtime import RuntimePlatform
from lvt.engines.asr_factory import (
    ASRBackend,
    asr_runtime_profile,
    create_asr_engine,
)


class _Engine:
    version = "test"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_macos_asr_profile_preserves_mlx_contract() -> None:
    profile = asr_runtime_profile(RuntimePlatform.MACOS)

    assert profile.backend is ASRBackend.MLX_WHISPER
    assert profile.default_model == "mlx-community/whisper-small-mlx"
    assert profile.installed_model_directory == Path("asr/whisper-small-mlx")
    assert profile.package_name == "mlx-whisper"
    assert profile.required_model_files == ("config.json", "weights.npz")


def test_windows_asr_profile_selects_cpu_faster_whisper() -> None:
    profile = asr_runtime_profile(RuntimePlatform.WINDOWS)

    assert profile.backend is ASRBackend.FASTER_WHISPER
    assert profile.default_model == "Systran/faster-whisper-small"
    assert profile.installed_model_directory == Path("asr/faster-whisper-small")
    assert profile.package_name == "faster-whisper"
    assert profile.required_model_files == (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    )
    assert profile.device == "cpu"
    assert profile.compute_type == "int8"


def test_create_asr_engine_selects_mlx_on_macos(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def mlx_factory(**kwargs: Any) -> _Engine:
        calls.append(kwargs)
        return _Engine(**kwargs)

    engine = create_asr_engine(
        platform=RuntimePlatform.MACOS,
        model="mlx-community/whisper-small-mlx",
        model_path=tmp_path / "mlx",
        ffmpeg_path=tmp_path / "ffmpeg",
        mlx_factory=mlx_factory,
        faster_whisper_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Windows factory was called")
        ),
    )

    assert isinstance(engine, _Engine)
    assert calls == [
        {
            "ffmpeg_path": tmp_path / "ffmpeg",
            "model": "mlx-community/whisper-small-mlx",
            "model_path": tmp_path / "mlx",
        }
    ]


def test_create_asr_engine_selects_faster_whisper_on_windows(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def faster_factory(**kwargs: Any) -> _Engine:
        calls.append(kwargs)
        return _Engine(**kwargs)

    engine = create_asr_engine(
        platform=RuntimePlatform.WINDOWS,
        model="Systran/faster-whisper-small",
        model_path=tmp_path / "faster-whisper",
        ffmpeg_path=tmp_path / "ffmpeg.exe",
        mlx_factory=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("macOS factory was called")
        ),
        faster_whisper_factory=faster_factory,
    )

    assert isinstance(engine, _Engine)
    assert calls == [
        {
            "compute_type": "int8",
            "device": "cpu",
            "model": "Systran/faster-whisper-small",
            "model_path": tmp_path / "faster-whisper",
        }
    ]
