from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


class UnsupportedRuntimePlatformError(RuntimeError):
    pass


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def path_is_link_like(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


@dataclass(frozen=True)
class RuntimeLayout:
    system: str
    target: str
    architecture: str
    dependency_manifest: str
    uv_executable: str
    python_executable: str
    venv_python: str
    ffmpeg_executables: Mapping[str, str]
    ollama_executables: Mapping[str, str]
    executable_format: str
    model_artifact_ids: tuple[str, ...]
    required_packages: tuple[str, ...]


_MACOS = RuntimeLayout(
    system="darwin",
    target="macos-arm64",
    architecture="arm64",
    dependency_manifest="packaging/dependencies.json",
    uv_executable="uv",
    python_executable="bin/python3",
    venv_python=".venv/bin/python",
    ffmpeg_executables=MappingProxyType(
        {
            "ffmpeg": "ffmpeg",
            "ffprobe": "ffprobe",
        }
    ),
    ollama_executables=MappingProxyType(
        {
            "ollama": "ollama",
            "llama-server": "llama-server",
            "llama-quantize": "llama-quantize",
        }
    ),
    executable_format="macho-arm64",
    model_artifact_ids=(
        "asr-whisper-small-mlx-config",
        "asr-whisper-small-mlx-weights",
        "diarization-segmentation",
        "diarization-embedding",
        "hy-mt2",
    ),
    required_packages=("mlx_whisper", "sherpa_onnx"),
)

_WINDOWS = RuntimeLayout(
    system="win32",
    target="windows-x64",
    architecture="x86_64",
    dependency_manifest="packaging/dependencies.windows-x64.json",
    uv_executable="uv.exe",
    python_executable="python.exe",
    venv_python=".venv/Scripts/python.exe",
    ffmpeg_executables=MappingProxyType(
        {
            "ffmpeg": "ffmpeg.exe",
            "ffprobe": "ffprobe.exe",
        }
    ),
    ollama_executables=MappingProxyType(
        {
            "ollama": "ollama.exe",
            "llama-server": "llama-server.exe",
            "llama-quantize": "llama-quantize.exe",
        }
    ),
    executable_format="pe-x64",
    model_artifact_ids=(
        "asr-faster-whisper-small-config",
        "asr-faster-whisper-small-model",
        "asr-faster-whisper-small-tokenizer",
        "asr-faster-whisper-small-vocabulary",
        "diarization-segmentation",
        "diarization-embedding",
        "hy-mt2",
    ),
    required_packages=("faster_whisper", "ctranslate2", "sherpa_onnx"),
)


def runtime_layout(system: str | None = None) -> RuntimeLayout:
    observed = sys.platform if system is None else system
    if observed == "darwin":
        return _MACOS
    if observed == "win32":
        return _WINDOWS
    raise UnsupportedRuntimePlatformError(f"unsupported operating system: {observed}")


def runtime_layout_for_target(target: str) -> RuntimeLayout:
    if target == _MACOS.target:
        return _MACOS
    if target == _WINDOWS.target:
        return _WINDOWS
    raise UnsupportedRuntimePlatformError(f"unsupported runtime target: {target}")
