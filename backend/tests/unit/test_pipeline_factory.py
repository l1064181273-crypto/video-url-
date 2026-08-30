from pathlib import Path
from typing import Any

from lvt.db.repository import JobRepository
from lvt.pipeline import factory
from lvt.pipeline.factory import RealPipelineConfig, create_real_pipeline


class _Engine:
    version = "test-engine"

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_create_real_pipeline_accepts_repository_for_checkpoint_execution(
    monkeypatch: Any, tmp_path: Path
) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    monkeypatch.setattr(
        factory,
        "discover_ffmpeg_binaries",
        lambda: (Path("/tools/ffmpeg"), Path("/tools/ffprobe")),
    )
    monkeypatch.setattr(factory, "YtDlpFFmpegDownloader", _Engine)
    monkeypatch.setattr(factory, "create_asr_engine", _Engine)
    monkeypatch.setattr(factory, "SherpaOnnxDiarizationEngine", _Engine)
    monkeypatch.setattr(factory, "OllamaTranslationEngine", _Engine)
    monkeypatch.setattr(factory, "FallbackTranslationEngine", _Engine)
    monkeypatch.setattr(factory, "FilteringTranslationEngine", lambda engine: engine)

    pipeline = create_real_pipeline(
        RealPipelineConfig(
            work_root=tmp_path / "work",
            export_root=tmp_path / "exports",
            segmentation_model=tmp_path / "seg.onnx",
            embedding_model=tmp_path / "emb.onnx",
        ),
        repository=repository,
    )

    assert pipeline.repository is repository
