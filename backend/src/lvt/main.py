from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from lvt.api.app import create_app
from lvt.core.capabilities import (
    CapabilitiesProvider,
    LocalCapabilitiesConfig,
    LocalCapabilityProbes,
    default_model_cache_root,
)
from lvt.core.config import Settings
from lvt.db.repository import JobRepository
from lvt.pipeline.factory import RealPipelineConfig, create_real_pipeline
from lvt.pipeline.runner import Pipeline
from lvt.security.token import load_or_create_token

settings = Settings.from_env()
settings.ensure_directories()
project_root = Path(__file__).resolve().parents[3]
pipeline_config = RealPipelineConfig(
    work_root=settings.data_root / "work",
    export_root=settings.data_root / "exports",
    segmentation_model=project_root
    / "vendor/diarization-models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
    embedding_model=project_root / "vendor/diarization-models/embed.onnx",
)
local_capability_probes = LocalCapabilityProbes(
    LocalCapabilitiesConfig(
        asr_model=pipeline_config.asr_model,
        segmentation_model=pipeline_config.segmentation_model,
        embedding_model=pipeline_config.embedding_model,
        ollama_url=pipeline_config.ollama_url,
        primary_translation_model=pipeline_config.primary_translation_model,
        fallback_translation_model=pipeline_config.fallback_translation_model,
        model_cache_root=default_model_cache_root(),
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
