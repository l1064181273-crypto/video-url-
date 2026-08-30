from __future__ import annotations

import os

import uvicorn

from lvt.api.app import create_app
from lvt.core.capabilities import (
    CapabilitiesProvider,
    LocalCapabilitiesConfig,
    LocalCapabilityProbes,
)
from lvt.core.config import Settings
from lvt.core.platform_runtime import executable_name
from lvt.db.repository import JobRepository
from lvt.engines.asr_factory import asr_runtime_profile
from lvt.pipeline.factory import RealPipelineConfig, create_real_pipeline
from lvt.pipeline.runner import Pipeline
from lvt.security.token import load_or_create_token

settings = Settings.from_env()
settings.configure_model_environment()
settings.ensure_directories()
assert settings.model_root is not None
asr_profile = asr_runtime_profile()
pipeline_config = RealPipelineConfig(
    work_root=settings.data_root / "work",
    export_root=settings.data_root / "exports",
    segmentation_model=settings.model_root / "diarization" / "segmentation" / "model.onnx",
    embedding_model=settings.model_root
    / "diarization"
    / "embedding"
    / "nemo_en_titanet_small.onnx",
    asr_model_path=(
        settings.model_root / asr_profile.installed_model_directory
        if settings.installed_mode
        else None
    ),
    runtime_platform=asr_profile.platform,
    ollama_url=settings.ollama_url,
    installed_mode=settings.installed_mode,
    ffmpeg_dir=settings.ffmpeg_dir,
    app_root=settings.data_root / "app",
    install_state=settings.install_state,
)
ffmpeg_path = (
    settings.ffmpeg_dir / executable_name("ffmpeg", asr_profile.platform)
    if settings.ffmpeg_dir
    else None
)
ffprobe_path = (
    settings.ffmpeg_dir / executable_name("ffprobe", asr_profile.platform)
    if settings.ffmpeg_dir
    else None
)
local_capability_probes = LocalCapabilityProbes(
    LocalCapabilitiesConfig(
        asr_model=pipeline_config.asr_model,
        asr_model_path=pipeline_config.asr_model_path,
        segmentation_model=pipeline_config.segmentation_model,
        embedding_model=pipeline_config.embedding_model,
        ollama_url=pipeline_config.ollama_url,
        primary_translation_model=pipeline_config.primary_translation_model,
        fallback_translation_model=pipeline_config.fallback_translation_model,
        model_cache_root=settings.model_root / "huggingface",
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        strict_ffmpeg=settings.installed_mode,
        asr_package_name=asr_profile.package_name,
        asr_required_model_files=asr_profile.required_model_files,
    )
)
capabilities_provider = CapabilitiesProvider(
    probes=local_capability_probes.as_probes(),
    asr_model=pipeline_config.asr_model,
    primary_translation_model=pipeline_config.primary_translation_model,
    fallback_translation_model=pipeline_config.fallback_translation_model,
)


def build_pipeline(repository: JobRepository) -> Pipeline:
    return create_real_pipeline(
        pipeline_config,
        repository=repository,
    )


app = create_app(
    db_path=settings.data_root / "db" / "lvt.sqlite3",
    api_token=os.environ.get("LVT_TOKEN")
    or load_or_create_token(settings.data_root / "config" / "api-token"),
    work_root=settings.data_root / "work",
    pipeline_builder=build_pipeline,
    capabilities_provider=capabilities_provider,
    worker_concurrency=(
        settings.worker_concurrency if "LVT_WORKER_CONCURRENCY" in os.environ else None
    ),
)


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
