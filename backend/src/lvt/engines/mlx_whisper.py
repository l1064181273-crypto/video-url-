from __future__ import annotations

import os
import wave
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from lvt.core.errors import LVTError
from lvt.engines.base import ASRResult, ASRSegment

TranscribeFn = Callable[..., dict[str, Any]]


def _default_transcribe(audio_path: str, **kwargs: Any) -> dict[str, Any]:
    import mlx_whisper  # type: ignore[import-untyped]

    result: dict[str, Any] = mlx_whisper.transcribe(audio_path, **kwargs)
    return result


class MLXWhisperASREngine:
    def __init__(
        self,
        *,
        model: str = "mlx-community/whisper-small-mlx",
        ffmpeg_path: Path | None = None,
        transcribe_fn: TranscribeFn = _default_transcribe,
    ) -> None:
        self.model = model
        self.ffmpeg_path = ffmpeg_path
        self.transcribe_fn = transcribe_fn
        try:
            package_version = metadata.version("mlx-whisper")
        except metadata.PackageNotFoundError:
            package_version = "missing"
        self.version = f"mlx-whisper:{package_version};model={model}"

    def transcribe(self, audio_path: Path) -> ASRResult:
        return self.transcribe_with_model(audio_path, self.model)

    def transcribe_with_model(self, audio_path: Path, model: str) -> ASRResult:
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise LVTError("MEDIA_INVALID", "ASR 输入音频不存在或为空")
        duration_ms = self._wav_duration_ms(audio_path)
        if self.ffmpeg_path is not None:
            ffmpeg_dir = os.fspath(self.ffmpeg_path.parent)
            current_path = os.environ.get("PATH", "")
            if ffmpeg_dir not in current_path.split(os.pathsep):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
        try:
            result = self.transcribe_fn(
                os.fspath(audio_path),
                path_or_hf_repo=model,
                word_timestamps=False,
                verbose=False,
            )
        except Exception as exc:
            raise LVTError("TRANSCRIPTION_FAILED", f"mlx-whisper 转写失败：{exc}") from exc

        language = str(result.get("language") or "").strip().lower()
        if not language:
            raise LVTError("TRANSCRIPTION_FAILED", "mlx-whisper 未返回源语言")
        segments: list[ASRSegment] = []
        for item in result.get("segments") or []:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                start_ms = max(0, round(float(item["start"]) * 1000))
                end_ms = min(duration_ms, round(float(item["end"]) * 1000))
            except (KeyError, TypeError, ValueError) as exc:
                raise LVTError("TRANSCRIPTION_FAILED", "ASR 时间戳格式无效") from exc
            if start_ms >= end_ms:
                continue
            segments.append(ASRSegment(start_ms=start_ms, end_ms=end_ms, text=text))
        segments.sort(key=lambda item: item.start_ms)
        if not segments:
            raise LVTError("TRANSCRIPTION_FAILED", "音频中没有可导出的语音文本")
        return ASRResult(language=language, segments=segments)

    @staticmethod
    def _wav_duration_ms(path: Path) -> int:
        try:
            with wave.open(os.fspath(path), "rb") as wav:
                return round(wav.getnframes() / wav.getframerate() * 1000)
        except (wave.Error, OSError, ZeroDivisionError) as exc:
            raise LVTError("MEDIA_INVALID", "无法读取 WAV 音频") from exc
