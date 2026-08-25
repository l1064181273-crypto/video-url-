from __future__ import annotations

from pathlib import Path
from typing import Any

from lvt.core.capabilities import (
    CapabilityStatus,
    LocalCapabilitiesConfig,
    LocalCapabilityProbes,
)
from lvt.pipeline import factory
from lvt.pipeline.factory import RealPipelineConfig, create_real_pipeline

ROOT = Path(__file__).resolve().parents[3]


class _Engine:
    version = "test-engine"

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_installed_pipeline_uses_configured_models_ffmpeg_and_ollama(
    monkeypatch: Any, tmp_path: Path
) -> None:
    ffmpeg_dir = tmp_path / "app" / "tools" / "ffmpeg" / "8.0" / "bin"
    install_state = tmp_path / "runtime" / "install-state.json"
    resolver_calls: list[dict[str, Any]] = []
    ollama_calls: list[dict[str, Any]] = []

    def resolve(**kwargs: Any) -> tuple[Path, Path]:
        resolver_calls.append(kwargs)
        return ffmpeg_dir / "ffmpeg", ffmpeg_dir / "ffprobe"

    class _OllamaEngine(_Engine):
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            super().__init__(*_args, **kwargs)
            ollama_calls.append(kwargs)

    monkeypatch.setattr(factory, "discover_ffmpeg_binaries", resolve)
    monkeypatch.setattr(factory, "YtDlpFFmpegDownloader", _Engine)
    monkeypatch.setattr(factory, "MLXWhisperASREngine", _Engine)
    monkeypatch.setattr(factory, "SherpaOnnxDiarizationEngine", _Engine)
    monkeypatch.setattr(factory, "OllamaTranslationEngine", _OllamaEngine)
    monkeypatch.setattr(factory, "FallbackTranslationEngine", _Engine)
    monkeypatch.setattr(factory, "FilteringTranslationEngine", lambda engine: engine)

    config = RealPipelineConfig(
        work_root=tmp_path / "work",
        export_root=tmp_path / "exports",
        segmentation_model=tmp_path / "models" / "diarization" / "segmentation" / "model.onnx",
        embedding_model=tmp_path
        / "models"
        / "diarization"
        / "embedding"
        / "nemo_en_titanet_small.onnx",
        ollama_url="http://127.0.0.1:11435",
        installed_mode=True,
        ffmpeg_dir=ffmpeg_dir,
        app_root=tmp_path / "app",
        install_state=install_state,
    )

    pipeline = create_real_pipeline(config)

    assert resolver_calls == [
        {
            "installed_mode": True,
            "ffmpeg_dir": ffmpeg_dir,
            "app_root": tmp_path / "app",
            "install_state": install_state,
        }
    ]
    assert pipeline.downloader.kwargs == {
        "ffmpeg_path": ffmpeg_dir / "ffmpeg",
        "ffprobe_path": ffmpeg_dir / "ffprobe",
        "process_root": tmp_path / "runtime/processes",
        "supervisor_path": ROOT / "packaging/tools/tool_supervisor.py",
    }
    assert pipeline.asr.kwargs["ffmpeg_path"] == ffmpeg_dir / "ffmpeg"
    assert pipeline.diarizer.kwargs["segmentation_model"] == config.segmentation_model
    assert pipeline.diarizer.kwargs["embedding_model"] == config.embedding_model
    assert [call["base_url"] for call in ollama_calls] == [
        "http://127.0.0.1:11435",
        "http://127.0.0.1:11435",
    ]


def test_capability_probe_only_contacts_configured_project_ollama(tmp_path: Path) -> None:
    requested: list[str] = []

    def request(url: str, _timeout: float) -> dict[str, Any]:
        requested.append(url)
        if url.endswith("/api/version"):
            return {"version": "0.32.15"}
        return {"models": []}

    probes = LocalCapabilityProbes(
        LocalCapabilitiesConfig(
            asr_model="mlx-community/whisper-small-mlx",
            segmentation_model=tmp_path / "models/diarization/segmentation/model.onnx",
            embedding_model=tmp_path / "models/diarization/embedding/nemo_en_titanet_small.onnx",
            ollama_url="http://127.0.0.1:11435",
            primary_translation_model="hy-mt2:1.8b-q4km-fixed",
            fallback_translation_model="qwen2.5:1.5b",
            model_cache_root=tmp_path / "models/huggingface",
        ),
        request_json=request,
    )

    assert probes.ollama(1).capability.status is CapabilityStatus.AVAILABLE
    assert requested == [
        "http://127.0.0.1:11435/api/version",
        "http://127.0.0.1:11435/api/tags",
    ]
    assert all("11434" not in url for url in requested)


def test_app_owned_model_probes_report_missing_without_download(
    monkeypatch: Any, tmp_path: Path
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("model probe attempted a download")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", forbidden)
    monkeypatch.setattr("huggingface_hub.snapshot_download", forbidden)
    model_root = tmp_path / "models"
    probes = LocalCapabilityProbes(
        LocalCapabilitiesConfig(
            asr_model="mlx-community/whisper-small-mlx",
            segmentation_model=model_root / "diarization/segmentation/model.onnx",
            embedding_model=model_root / "diarization/embedding/nemo_en_titanet_small.onnx",
            ollama_url="http://127.0.0.1:11435",
            primary_translation_model="hy-mt2:1.8b-q4km-fixed",
            fallback_translation_model="qwen2.5:1.5b",
            model_cache_root=model_root / "huggingface",
        ),
        package_version=lambda _name: "installed",
    )

    assert probes.asr_model(1).status is CapabilityStatus.MISSING
    assert probes.diarization(1).status is CapabilityStatus.MISSING


def test_strict_ffmpeg_capability_uses_configured_binary_without_path_fallback(
    tmp_path: Path,
) -> None:
    ffmpeg = tmp_path / "app/tools/ffmpeg/8.0/bin/ffmpeg"
    ffprobe = ffmpeg.with_name("ffprobe")
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.touch()
    ffprobe.touch()
    commands: list[list[str]] = []

    def run(command: list[str], _timeout: float) -> tuple[int, str]:
        commands.append(command)
        return 0, "ffmpeg version 8.0"

    probes = LocalCapabilityProbes(
        LocalCapabilitiesConfig(
            asr_model="mlx-community/whisper-small-mlx",
            segmentation_model=tmp_path / "models/diarization/segmentation/model.onnx",
            embedding_model=tmp_path / "models/diarization/embedding/nemo_en_titanet_small.onnx",
            ollama_url="http://127.0.0.1:11435",
            primary_translation_model="hy-mt2:1.8b-q4km-fixed",
            fallback_translation_model="qwen2.5:1.5b",
            model_cache_root=tmp_path / "models/huggingface",
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            strict_ffmpeg=True,
        ),
        which=lambda _name: (_ for _ in ()).throw(
            AssertionError("strict capability probe used PATH")
        ),
        run_command=run,
    )

    assert probes.ffmpeg(1).status is CapabilityStatus.AVAILABLE
    assert commands == [[str(ffmpeg), "-version"]]
