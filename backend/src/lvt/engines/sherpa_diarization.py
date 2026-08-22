from __future__ import annotations

import os
import wave
from importlib import metadata
from pathlib import Path

import numpy as np

from lvt.core.errors import LVTError
from lvt.engines.base import SpeakerInterval


class SherpaOnnxDiarizationEngine:
    def __init__(
        self,
        *,
        segmentation_model: Path,
        embedding_model: Path,
        num_speakers: int | None = None,
        clustering_threshold: float = 0.5,
    ) -> None:
        if num_speakers is not None and num_speakers < 1:
            raise ValueError("num_speakers must be positive")
        if not 0 < clustering_threshold < 1:
            raise ValueError("clustering_threshold must be between 0 and 1")
        self.segmentation_model = segmentation_model
        self.embedding_model = embedding_model
        self.num_speakers = num_speakers
        self.clustering_threshold = clustering_threshold
        try:
            package_version = metadata.version("sherpa-onnx")
        except metadata.PackageNotFoundError:
            package_version = "missing"
        mode = (
            str(num_speakers) if num_speakers is not None else f"threshold={clustering_threshold}"
        )
        self.version = f"sherpa-onnx:{package_version};clusters={mode}"
        self._engine: object | None = None

    def diarize(self, audio_path: Path) -> list[SpeakerInterval]:
        samples, sample_rate = self._read_wav(audio_path)
        if samples.size == 0 or float(np.sqrt(np.mean(np.square(samples)))) < 1e-5:
            return []
        engine = self._get_engine()
        try:
            raw_result = engine.process(samples).sort_by_start_time()  # type: ignore[attr-defined]
        except Exception as exc:
            raise LVTError("DIARIZATION_FAILED", f"sherpa-onnx 处理失败：{exc}") from exc
        duration_ms = round(samples.size / sample_rate * 1000)
        intervals: list[SpeakerInterval] = []
        for item in raw_result:
            start_ms = max(0, round(float(item.start) * 1000))
            end_ms = min(duration_ms, round(float(item.end) * 1000))
            if start_ms >= end_ms:
                continue
            intervals.append(
                SpeakerInterval(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    raw_speaker=f"SPEAKER_{int(item.speaker):02d}",
                )
            )
        return intervals

    def _get_engine(self) -> object:
        if self._engine is not None:
            return self._engine
        if not self.segmentation_model.is_file() or not self.embedding_model.is_file():
            raise LVTError("DIARIZATION_MODEL_MISSING", "说话人模型文件不存在")
        try:
            import sherpa_onnx  # type: ignore[import-untyped]

            clustering = sherpa_onnx.FastClusteringConfig(
                num_clusters=self.num_speakers or -1,
                threshold=self.clustering_threshold,
            )
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=os.fspath(self.segmentation_model)
                    )
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=os.fspath(self.embedding_model)
                ),
                clustering=clustering,
            )
            if not config.validate():
                raise ValueError("invalid sherpa-onnx configuration")
            self._engine = sherpa_onnx.OfflineSpeakerDiarization(config)
        except (ImportError, ValueError, RuntimeError) as exc:
            raise LVTError("DIARIZATION_FAILED", f"无法加载说话人模型：{exc}") from exc
        return self._engine

    @staticmethod
    def _read_wav(path: Path) -> tuple[np.ndarray, int]:
        try:
            with wave.open(os.fspath(path), "rb") as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise LVTError("MEDIA_INVALID", "diarization 需要单声道 16-bit PCM WAV")
                sample_rate = wav.getframerate()
                if sample_rate != 16000:
                    raise LVTError("MEDIA_INVALID", "diarization 需要 16kHz WAV")
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, OSError) as exc:
            raise LVTError("MEDIA_INVALID", "无法读取 diarization WAV") from exc
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return samples, sample_rate
