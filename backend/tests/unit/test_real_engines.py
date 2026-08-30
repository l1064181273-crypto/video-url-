import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from lvt.core.errors import LVTError
from lvt.core.processes import ProcessResult
from lvt.engines.base import DownloadedMedia
from lvt.engines.media import YtDlpFFmpegDownloader
from lvt.engines.mlx_whisper import MLXWhisperASREngine
from lvt.engines.sherpa_diarization import SherpaOnnxDiarizationEngine


def write_wav(path: Path, samples: list[int], sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_mlx_whisper_normalizes_and_filters_segments(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    write_wav(audio, [0] * 16000)

    def transcribe(_audio: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "language": "EN",
            "segments": [
                {"start": -0.2, "end": 0.4, "text": " Hello. "},
                {"start": 0.4, "end": 2.0, "text": "World."},
                {"start": 0.9, "end": 0.9, "text": "invalid"},
                {"start": 0.2, "end": 0.3, "text": "   "},
            ],
        }

    result = MLXWhisperASREngine(transcribe_fn=transcribe).transcribe(audio)

    assert result.language == "en"
    assert [(item.start_ms, item.end_ms, item.text) for item in result.segments] == [
        (0, 400, "Hello."),
        (400, 1000, "World."),
    ]


def test_mlx_whisper_rejects_empty_transcript(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    write_wav(audio, [0] * 1600)

    engine = MLXWhisperASREngine(
        transcribe_fn=lambda *_args, **_kwargs: {"language": "en", "segments": []}
    )
    with pytest.raises(LVTError, match="没有可导出的语音文本"):
        engine.transcribe(audio)


def test_mlx_whisper_uses_requested_persisted_model(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    write_wav(audio, [0] * 16_000)
    captured: dict[str, Any] = {}

    def transcribe(_audio: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "Hello."}],
        }

    engine = MLXWhisperASREngine(transcribe_fn=transcribe)
    engine.transcribe_with_model(audio, "mlx-community/custom-model")

    assert captured["path_or_hf_repo"] == "mlx-community/custom-model"
    assert engine.version_for_model("mlx-community/custom-model").endswith(
        "model=mlx-community/custom-model"
    )


def test_mlx_whisper_uses_app_owned_path_for_installed_default(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    write_wav(audio, [0] * 16_000)
    model_path = tmp_path / "models/asr/whisper-small-mlx"
    model_path.mkdir(parents=True)
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    (model_path / "weights.npz").write_bytes(b"weights")
    captured: dict[str, Any] = {}

    def transcribe(_audio: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "language": "en",
            "segments": [{"start": 0.0, "end": 1.0, "text": "Hello."}],
        }

    engine = MLXWhisperASREngine(model_path=model_path, transcribe_fn=transcribe)
    engine.transcribe_with_model(audio, "mlx-community/whisper-small-mlx")

    assert captured["path_or_hf_repo"] == str(model_path)
    assert engine.version_for_model("mlx-community/whisper-small-mlx").endswith(
        "model=mlx-community/whisper-small-mlx"
    )


def test_ffmpeg_normalizer_accepts_verified_sibling_checkpoint_input(
    tmp_path: Path,
) -> None:
    downloaded_dir = tmp_path / "run" / "downloaded_media"
    normalized_dir = tmp_path / "run" / ".normalized_audio.tmp"
    downloaded_dir.mkdir(parents=True)
    normalized_dir.mkdir(parents=True)
    downloaded = downloaded_dir / "download.bin"
    downloaded.write_bytes(b"media")

    class Executor:
        def run(self, command: list[str], **_kwargs: Any) -> ProcessResult:
            if "-version" in command:
                stdout = "ffmpeg version 7.0\n"
            elif "-show_entries" in command:
                stdout = '{"format": {"duration": "1.0"}}'
            else:
                write_wav(Path(command[-1]), [0] * 16_000)
                stdout = ""
            return ProcessResult(tuple(command), 1, 0, stdout, "")

    engine = YtDlpFFmpegDownloader(
        ffmpeg_path=Path("/tools/ffmpeg"),
        ffprobe_path=Path("/tools/ffprobe"),
        process_executor=Executor(),  # type: ignore[arg-type]
    )

    result = engine.normalize_audio(
        DownloadedMedia(downloaded, "Sibling input"),
        normalized_dir,
    )

    assert result.audio_path.parent == normalized_dir
    assert result.duration_ms == 1_000


def test_downloader_routes_ytdlp_ffmpeg_and_ffprobe_through_one_executor(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    class Executor:
        def run(self, command: list[str], **_kwargs: Any) -> ProcessResult:
            normalized = tuple(str(value) for value in command)
            commands.append(normalized)
            if "-version" in normalized:
                stdout = "ffmpeg version 7.0\n"
            elif "yt_dlp" in normalized:
                template = Path(normalized[normalized.index("--output") + 1])
                downloaded = Path(str(template).replace("%(ext)s", "m4a"))
                downloaded.write_bytes(b"downloaded")
                stdout = f"__LVT_PATH__{downloaded}\n__LVT_TITLE__Downloaded title\n"
            elif "-show_entries" in normalized:
                stdout = '{"format": {"duration": "1.0"}}'
            else:
                write_wav(Path(normalized[-1]), [0] * 16_000)
                stdout = ""
            return ProcessResult(normalized, 1, 0, stdout, "")

    engine = YtDlpFFmpegDownloader(
        ffmpeg_path=Path("/tools/ffmpeg"),
        ffprobe_path=Path("/tools/ffprobe"),
        process_executor=Executor(),  # type: ignore[arg-type]
    )
    work_dir = tmp_path / "run"

    result = engine.download("https://example.test/video", work_dir)

    assert result.title == "Downloaded title"
    assert result.duration_ms == 1_000
    assert any("yt_dlp" in command for command in commands)
    assert any(command[0] == str(Path("/tools/ffmpeg")) and "-i" in command for command in commands)
    assert any(command[0] == str(Path("/tools/ffprobe")) for command in commands)


def test_installed_media_attaches_job_run_and_kind_ownership(tmp_path: Path) -> None:
    ownership: list[Any] = []
    job_id = "22222222-2222-4222-8222-222222222222"
    run_id = "11111111-1111-4111-8111-111111111111"
    run_root = tmp_path / "work" / job_id / "runs" / run_id
    downloaded_dir = run_root / "downloaded_media"
    normalized_dir = run_root / ".normalized_audio.tmp"
    downloaded_dir.mkdir(parents=True)
    normalized_dir.mkdir()
    downloaded = downloaded_dir / "download.bin"
    downloaded.write_bytes(b"media")
    supervisor = tmp_path / "tool_supervisor.py"
    supervisor.write_text("# fixture\n", encoding="utf-8")

    class Executor:
        def run(self, command: list[str], **kwargs: Any) -> ProcessResult:
            ownership.append(kwargs.get("ownership"))
            if "-version" in command:
                stdout = "ffmpeg version 7.0\n"
            elif "-show_entries" in command:
                stdout = '{"format": {"duration": "1.0"}}'
            else:
                write_wav(Path(command[-1]), [0] * 16_000)
                stdout = ""
            return ProcessResult(tuple(command), 1, 0, stdout, "")

    engine = YtDlpFFmpegDownloader(
        ffmpeg_path=Path("/tools/ffmpeg"),
        ffprobe_path=Path("/tools/ffprobe"),
        process_executor=Executor(),  # type: ignore[arg-type]
        process_root=tmp_path / "runtime/processes",
        supervisor_path=supervisor,
    )

    engine.normalize_audio(DownloadedMedia(downloaded, "Owned input"), normalized_dir)

    assert ownership[0] is None
    assert [(item.job_id, item.run_id, item.kind) for item in ownership[1:]] == [
        (job_id, run_id, "ffmpeg"),
        (job_id, run_id, "ffprobe"),
    ]


def test_sherpa_diarization_returns_empty_for_silence_without_loading_models(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "silence.wav"
    write_wav(audio, [0] * 16000)
    engine = SherpaOnnxDiarizationEngine(
        segmentation_model=tmp_path / "missing-seg.onnx",
        embedding_model=tmp_path / "missing-embed.onnx",
    )

    assert engine.diarize(audio) == []


def test_sherpa_diarization_requires_models_for_speech(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    write_wav(audio, [1000, -1000] * 8000)
    engine = SherpaOnnxDiarizationEngine(
        segmentation_model=tmp_path / "missing-seg.onnx",
        embedding_model=tmp_path / "missing-embed.onnx",
    )

    with pytest.raises(LVTError, match="DIARIZATION_MODEL_MISSING"):
        engine.diarize(audio)
