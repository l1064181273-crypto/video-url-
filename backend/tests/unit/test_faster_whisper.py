from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lvt.core.errors import LVTError
from lvt.engines.faster_whisper import FasterWhisperASREngine


def _write_wav(path: Path, *, frames: int = 16_000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * frames)


def _model_directory(tmp_path: Path) -> Path:
    root = tmp_path / "models/asr/faster-whisper-small"
    root.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        (root / name).write_bytes(name.encode("ascii"))
    return root


@dataclass(frozen=True)
class _Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class _Info:
    language: str


class _Model:
    def __init__(self, result: tuple[list[_Segment], _Info]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(self, audio_path: str, **kwargs: Any) -> tuple[list[_Segment], _Info]:
        self.calls.append((audio_path, kwargs))
        return self.result


def test_faster_whisper_uses_app_owned_model_without_download(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    model_path = _model_directory(tmp_path)
    model = _Model(
        (
            [
                _Segment(0.7, 1.4, " later "),
                _Segment(-1.0, 0.5, " first "),
                _Segment(0.2, 0.3, "   "),
                _Segment(1.5, 2.0, "outside duration"),
            ],
            _Info("EN"),
        )
    )
    factory_calls: list[tuple[str, dict[str, Any]]] = []

    def model_factory(model_name: str, **kwargs: Any) -> _Model:
        factory_calls.append((model_name, kwargs))
        return model

    engine = FasterWhisperASREngine(
        model="Systran/faster-whisper-small",
        model_path=model_path,
        model_factory=model_factory,
    )

    result = engine.transcribe(audio)

    assert result.language == "en"
    assert [(item.start_ms, item.end_ms, item.text) for item in result.segments] == [
        (0, 500, "first"),
        (700, 1_000, "later"),
    ]
    assert factory_calls == [
        (
            str(model_path),
            {"compute_type": "int8", "device": "cpu", "local_files_only": True},
        )
    ]
    assert model.calls == [(str(audio), {"vad_filter": True, "word_timestamps": False})]


def test_faster_whisper_reuses_one_loaded_model(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    model = _Model(([_Segment(0.0, 0.5, "hello")], _Info("en")))
    factory_calls = 0

    def model_factory(_model_name: str, **_kwargs: Any) -> _Model:
        nonlocal factory_calls
        factory_calls += 1
        return model

    engine = FasterWhisperASREngine(model_factory=model_factory)

    engine.transcribe(audio)
    engine.transcribe(audio)

    assert factory_calls == 1
    assert len(model.calls) == 2


@pytest.mark.parametrize(
    "missing", ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
)
def test_faster_whisper_rejects_incomplete_app_owned_model(tmp_path: Path, missing: str) -> None:
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    model_path = _model_directory(tmp_path)
    (model_path / missing).unlink()

    engine = FasterWhisperASREngine(
        model_path=model_path,
        model_factory=lambda *_args, **_kwargs: pytest.fail("model factory was called"),
    )

    with pytest.raises(LVTError) as caught:
        engine.transcribe(audio)

    assert caught.value.code == "ASR_MODEL_UNAVAILABLE"


def test_faster_whisper_installed_model_rejects_runtime_model_switch(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    engine = FasterWhisperASREngine(
        model_path=_model_directory(tmp_path),
        model_factory=lambda *_args, **_kwargs: pytest.fail("model factory was called"),
    )

    with pytest.raises(LVTError) as caught:
        engine.transcribe_with_model(audio, "untrusted/remote-model")

    assert caught.value.code == "ASR_MODEL_UNAVAILABLE"


def test_faster_whisper_requires_language_and_nonempty_segments(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    missing_language = FasterWhisperASREngine(
        model_factory=lambda *_args, **_kwargs: _Model(([_Segment(0.0, 0.5, "hello")], _Info("")))
    )
    empty_segments = FasterWhisperASREngine(
        model_factory=lambda *_args, **_kwargs: _Model(([_Segment(0.0, 0.5, "   ")], _Info("en")))
    )

    with pytest.raises(LVTError, match="未返回源语言"):
        missing_language.transcribe(audio)
    with pytest.raises(LVTError, match="没有可导出的语音文本"):
        empty_segments.transcribe(audio)


def test_faster_whisper_wraps_runtime_failure(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    _write_wav(audio)

    def fail(*_args: Any, **_kwargs: Any) -> _Model:
        raise RuntimeError("injected failure")

    engine = FasterWhisperASREngine(model_factory=fail)

    with pytest.raises(LVTError) as caught:
        engine.transcribe(audio)

    assert caught.value.code == "TRANSCRIPTION_FAILED"
    assert "injected failure" in str(caught.value)
