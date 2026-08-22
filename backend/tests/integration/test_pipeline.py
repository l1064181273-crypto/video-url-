from pathlib import Path

from lvt.engines.base import (
    ASRResult,
    ASRSegment,
    MediaInfo,
    SpeakerInterval,
    TranslationResult,
)
from lvt.pipeline.runner import Pipeline


class FakeDownloader:
    version = "fake-downloader-1"

    def download(self, url: str, work_dir: Path) -> MediaInfo:
        audio_path = work_dir / "中文 音频.wav"
        audio_path.write_bytes(b"fake-wav")
        return MediaInfo(audio_path=audio_path, title="中文 / Sample", duration_ms=6000)


class FakeASR:
    version = "fake-asr-1"

    def transcribe(self, audio_path: Path) -> ASRResult:
        return ASRResult(
            language="ru",
            segments=[
                ASRSegment(0, 2600, "Привет."),
                ASRSegment(2700, 5900, "Локальная обработка."),
            ],
        )


class FakeDiarizer:
    version = "fake-diarizer-1"

    def diarize(self, audio_path: Path) -> list[SpeakerInterval]:
        return [
            SpeakerInterval(0, 2600, "SPEAKER_07"),
            SpeakerInterval(2700, 5900, "SPEAKER_02"),
        ]


class FakeTranslator:
    version = "fake-translator-1"

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        assert texts == {1: "Привет.", 2: "Локальная обработка."}
        assert source_language == "ru"
        return TranslationResult(
            texts={1: "你好。", 2: "本地处理。"},
            engine_version=self.version,
            warnings=[],
        )


def test_fake_engine_pipeline_generates_eight_aligned_artifacts(tmp_path: Path) -> None:
    pipeline = Pipeline(
        downloader=FakeDownloader(),
        asr=FakeASR(),
        diarizer=FakeDiarizer(),
        translator=FakeTranslator(),
        work_root=tmp_path / "work",
        export_root=tmp_path / "exports",
    )
    result = pipeline.run(job_id="job-1", url="https://example.test/video")

    assert len(result.artifacts) == 8
    assert [segment.speaker for segment in result.transcript.segments] == [
        "Speaker 1",
        "Speaker 2",
    ]
    assert [segment.source_text for segment in result.transcript.segments] == [
        "Привет.",
        "Локальная обработка.",
    ]
    assert [segment.translated_text for segment in result.transcript.segments] == [
        "你好。",
        "本地处理。",
    ]
    assert (tmp_path / "work" / "job-1" / "transcript.normalized.json").is_file()
