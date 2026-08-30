from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from lvt.core.platform_runtime import (
    RuntimePlatform,
    default_asr_model,
    resolve_runtime_platform,
)
from lvt.engines.base import ConfigurableASREngine
from lvt.engines.faster_whisper import FasterWhisperASREngine
from lvt.engines.mlx_whisper import MLXWhisperASREngine


class ASRBackend(StrEnum):
    MLX_WHISPER = "mlx-whisper"
    FASTER_WHISPER = "faster-whisper"


@dataclass(frozen=True)
class ASRRuntimeProfile:
    platform: RuntimePlatform
    backend: ASRBackend
    default_model: str
    installed_model_directory: Path
    package_name: str
    required_model_files: tuple[str, ...]
    device: str
    compute_type: str


EngineFactory = Callable[..., ConfigurableASREngine]


def asr_runtime_profile(
    platform: RuntimePlatform | None = None,
) -> ASRRuntimeProfile:
    selected = resolve_runtime_platform() if platform is None else platform
    if selected is RuntimePlatform.MACOS:
        return ASRRuntimeProfile(
            platform=selected,
            backend=ASRBackend.MLX_WHISPER,
            default_model=default_asr_model(selected),
            installed_model_directory=Path("asr/whisper-small-mlx"),
            package_name="mlx-whisper",
            required_model_files=("config.json", "weights.npz"),
            device="metal",
            compute_type="float16",
        )
    return ASRRuntimeProfile(
        platform=selected,
        backend=ASRBackend.FASTER_WHISPER,
        default_model=default_asr_model(selected),
        installed_model_directory=Path("asr/faster-whisper-small"),
        package_name="faster-whisper",
        required_model_files=(
            "config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.txt",
        ),
        device="cpu",
        compute_type="int8",
    )


def create_asr_engine(
    *,
    platform: RuntimePlatform,
    model: str,
    model_path: Path | None,
    ffmpeg_path: Path | None,
    mlx_factory: EngineFactory = MLXWhisperASREngine,
    faster_whisper_factory: EngineFactory = FasterWhisperASREngine,
) -> ConfigurableASREngine:
    profile = asr_runtime_profile(platform)
    if profile.backend is ASRBackend.MLX_WHISPER:
        return mlx_factory(
            model=model,
            model_path=model_path,
            ffmpeg_path=ffmpeg_path,
        )
    return faster_whisper_factory(
        model=model,
        model_path=model_path,
        device=profile.device,
        compute_type=profile.compute_type,
    )
