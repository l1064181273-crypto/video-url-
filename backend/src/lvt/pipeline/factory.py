from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lvt.engines.media import YtDlpFFmpegDownloader, discover_ffmpeg_binaries
from lvt.engines.mlx_whisper import MLXWhisperASREngine
from lvt.engines.ollama import FallbackTranslationEngine, OllamaTranslationEngine
from lvt.engines.sherpa_diarization import SherpaOnnxDiarizationEngine
from lvt.pipeline.runner import Pipeline


@dataclass(frozen=True)
class RealPipelineConfig:
    work_root: Path
    export_root: Path
    segmentation_model: Path
    embedding_model: Path
    asr_model: str = "mlx-community/whisper-small-mlx"
    ollama_url: str = "http://127.0.0.1:11434"
    primary_translation_model: str = "hy-mt2:1.8b-q4km-fixed"
    fallback_translation_model: str = "qwen2.5:1.5b"
    diarization_threshold: float = 0.5


def create_real_pipeline(config: RealPipelineConfig) -> Pipeline:
    ffmpeg, ffprobe = discover_ffmpeg_binaries()
    return Pipeline(
        downloader=YtDlpFFmpegDownloader(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
        ),
        asr=MLXWhisperASREngine(
            model=config.asr_model,
            ffmpeg_path=ffmpeg,
        ),
        diarizer=SherpaOnnxDiarizationEngine(
            segmentation_model=config.segmentation_model,
            embedding_model=config.embedding_model,
            clustering_threshold=config.diarization_threshold,
        ),
        translator=FallbackTranslationEngine(
            primary=OllamaTranslationEngine(
                model=config.primary_translation_model,
                base_url=config.ollama_url,
            ),
            fallback=OllamaTranslationEngine(
                model=config.fallback_translation_model,
                base_url=config.ollama_url,
            ),
        ),
        work_root=config.work_root,
        export_root=config.export_root,
    )
