from __future__ import annotations

import os
import threading
import wave
from collections.abc import Callable, Iterable
from importlib import import_module, metadata
from pathlib import Path
from typing import Any, Protocol, cast

from lvt.core.errors import LVTError
from lvt.engines.base import ASRResult, ASRSegment


class FasterWhisperSegment(Protocol):
    start: float
    end: float
    text: str


class FasterWhisperInfo(Protocol):
    language: str


class FasterWhisperModel(Protocol):
    def transcribe(
        self,
        audio_path: str,
        *,
        vad_filter: bool,
        word_timestamps: bool,
    ) -> tuple[Iterable[FasterWhisperSegment], FasterWhisperInfo]: ...


ModelFactory = Callable[..., FasterWhisperModel]


def _default_model_factory(model_name: str, **kwargs: Any) -> FasterWhisperModel:
    whisper_model = import_module("faster_whisper").WhisperModel

    return cast(FasterWhisperModel, whisper_model(model_name, **kwargs))


class FasterWhisperASREngine:
    REQUIRED_MODEL_FILES = (
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    )

    def __init__(
        self,
        *,
        model: str = "Systran/faster-whisper-small",
        model_path: Path | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        model_factory: ModelFactory = _default_model_factory,
    ) -> None:
        self.model = model
        self.model_path = model_path
        self.device = device
        self.compute_type = compute_type
        self.model_factory = model_factory
        self._loaded_name: str | None = None
        self._loaded_model: FasterWhisperModel | None = None
        self._inference_lock = threading.Lock()
        try:
            package_version = metadata.version("faster-whisper")
        except metadata.PackageNotFoundError:
            package_version = "missing"
        self.package_version = package_version
        self.version = self.version_for_model(model)

    def version_for_model(self, model: str) -> str:
        return (
            f"faster-whisper:{self.package_version};"
            f"device={self.device};compute_type={self.compute_type};model={model}"
        )

    def transcribe(self, audio_path: Path) -> ASRResult:
        return self.transcribe_with_model(audio_path, self.model)

    def transcribe_with_model(self, audio_path: Path, model: str) -> ASRResult:
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise LVTError("MEDIA_INVALID", "ASR 输入音频不存在或为空")
        duration_ms = self._wav_duration_ms(audio_path)
        runtime_model, local_files_only = self._runtime_model(model)
        try:
            with self._inference_lock:
                loaded = self._load_model(runtime_model, local_files_only=local_files_only)
                observed_segments, info = loaded.transcribe(
                    os.fspath(audio_path),
                    vad_filter=True,
                    word_timestamps=False,
                )
                raw_segments = list(observed_segments)
        except Exception as exc:
            raise LVTError(
                "TRANSCRIPTION_FAILED",
                f"faster-whisper 转写失败：{exc}",
            ) from exc

        language = str(getattr(info, "language", "") or "").strip().lower()
        if not language:
            raise LVTError("TRANSCRIPTION_FAILED", "faster-whisper 未返回源语言")
        segments: list[ASRSegment] = []
        for item in raw_segments:
            text = str(getattr(item, "text", "") or "").strip()
            if not text:
                continue
            try:
                start_ms = max(0, round(float(item.start) * 1000))
                end_ms = min(duration_ms, round(float(item.end) * 1000))
            except (AttributeError, TypeError, ValueError) as exc:
                raise LVTError("TRANSCRIPTION_FAILED", "ASR 时间戳格式无效") from exc
            if start_ms >= end_ms:
                continue
            segments.append(ASRSegment(start_ms=start_ms, end_ms=end_ms, text=text))
        segments.sort(key=lambda item: item.start_ms)
        if not segments:
            raise LVTError("TRANSCRIPTION_FAILED", "音频中没有可导出的语音文本")
        return ASRResult(language=language, segments=segments)

    def _load_model(self, model_name: str, *, local_files_only: bool) -> FasterWhisperModel:
        if self._loaded_model is None or self._loaded_name != model_name:
            self._loaded_model = self.model_factory(
                model_name,
                compute_type=self.compute_type,
                device=self.device,
                local_files_only=local_files_only,
            )
            self._loaded_name = model_name
        return self._loaded_model

    def _runtime_model(self, model: str) -> tuple[str, bool]:
        if self.model_path is None:
            return model, False
        if model != self.model:
            raise LVTError(
                "ASR_MODEL_UNAVAILABLE",
                "安装模式不允许切换到未固定的 ASR 模型",
            )
        try:
            available = (
                not self.model_path.is_symlink()
                and self.model_path.is_dir()
                and all(
                    not path.is_symlink() and path.is_file() and path.stat().st_size > 0
                    for path in (self.model_path / name for name in self.REQUIRED_MODEL_FILES)
                )
            )
        except OSError:
            available = False
        if not available:
            raise LVTError("ASR_MODEL_UNAVAILABLE", "已安装 ASR 模型不可用")
        return os.fspath(self.model_path), True

    @staticmethod
    def _wav_duration_ms(path: Path) -> int:
        try:
            with wave.open(os.fspath(path), "rb") as wav:
                return round(wav.getnframes() / wav.getframerate() * 1000)
        except (wave.Error, OSError, ZeroDivisionError) as exc:
            raise LVTError("MEDIA_INVALID", "无法读取 WAV 音频") from exc
