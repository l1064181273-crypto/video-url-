import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from lvt.core.errors import LVTError
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
