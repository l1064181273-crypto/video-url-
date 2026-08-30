from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lvt.core.models import DEFAULT_ASR_MODEL
from lvt.core.platform_runtime import RuntimePlatform, resolve_runtime_platform
from lvt.db.repository import JobRepository
from lvt.engines.asr_factory import asr_runtime_profile, create_asr_engine
from lvt.engines.media import YtDlpFFmpegDownloader, discover_ffmpeg_binaries
from lvt.engines.ollama import FallbackTranslationEngine, OllamaTranslationEngine
from lvt.engines.sherpa_diarization import SherpaOnnxDiarizationEngine
from lvt.engines.translation import FilteringTranslationEngine
from lvt.pipeline.runner import Pipeline


@dataclass(frozen=True)
class RealPipelineConfig:
    work_root: Path
    export_root: Path
    segmentation_model: Path
    embedding_model: Path
    asr_model: str = DEFAULT_ASR_MODEL
    asr_model_path: Path | None = None
    runtime_platform: RuntimePlatform | None = None
    ollama_url: str = "http://127.0.0.1:11435"
    primary_translation_model: str = "hy-mt2:1.8b-q4km-fixed"
    fallback_translation_model: str = "qwen2.5:1.5b"
    diarization_threshold: float = 0.5
    installed_mode: bool = False
    ffmpeg_dir: Path | None = None
    app_root: Path | None = None
    install_state: Path | None = None

    def __post_init__(self) -> None:
        selected = self.runtime_platform or resolve_runtime_platform()
        object.__setattr__(self, "runtime_platform", selected)
        if selected is RuntimePlatform.WINDOWS and self.asr_model == DEFAULT_ASR_MODEL:
            object.__setattr__(self, "asr_model", asr_runtime_profile(selected).default_model)


def create_real_pipeline(
    config: RealPipelineConfig,
    *,
    repository: JobRepository | None = None,
) -> Pipeline:
    assert config.runtime_platform is not None
    if config.installed_mode:
        ffmpeg, ffprobe = discover_ffmpeg_binaries(
            installed_mode=True,
            ffmpeg_dir=config.ffmpeg_dir,
            app_root=config.app_root,
            install_state=config.install_state,
            runtime_platform=config.runtime_platform,
        )
    else:
        ffmpeg, ffprobe = discover_ffmpeg_binaries()
    return Pipeline(
        downloader=YtDlpFFmpegDownloader(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            process_root=(
                config.app_root.parent / "runtime/processes"
                if config.installed_mode and config.app_root is not None
                else None
            ),
            supervisor_path=(
                Path(__file__).resolve().parents[4] / "packaging/tools/tool_supervisor.py"
                if config.installed_mode
                else None
            ),
        ),
        asr=create_asr_engine(
            platform=config.runtime_platform,
            model=config.asr_model,
            model_path=config.asr_model_path,
            ffmpeg_path=ffmpeg,
        ),
        diarizer=SherpaOnnxDiarizationEngine(
            segmentation_model=config.segmentation_model,
            embedding_model=config.embedding_model,
            clustering_threshold=config.diarization_threshold,
        ),
        translator=FilteringTranslationEngine(
            FallbackTranslationEngine(
                primary=OllamaTranslationEngine(
                    model=config.primary_translation_model,
                    base_url=config.ollama_url,
                ),
                fallback=OllamaTranslationEngine(
                    model=config.fallback_translation_model,
                    base_url=config.ollama_url,
                ),
            ),
        ),
        work_root=config.work_root,
        export_root=config.export_root,
        repository=repository,
    )
