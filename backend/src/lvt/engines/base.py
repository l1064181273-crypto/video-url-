from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class DownloadedMedia:
    media_path: Path
    title: str


@dataclass(frozen=True)
class MediaInfo:
    audio_path: Path
    title: str
    duration_ms: int


@dataclass(frozen=True)
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class ASRResult:
    language: str
    segments: list[ASRSegment]


@dataclass(frozen=True)
class SpeakerInterval:
    start_ms: int
    end_ms: int
    raw_speaker: str


@dataclass(frozen=True)
class TranslationResult:
    texts: dict[int, str]
    engine_version: str
    warnings: list[str]


class Downloader(Protocol):
    version: str

    def download(self, url: str, work_dir: Path) -> MediaInfo: ...


class StagedDownloader(Downloader, Protocol):
    def download_media(self, url: str, work_dir: Path) -> DownloadedMedia: ...

    def normalize_audio(self, media: DownloadedMedia, work_dir: Path) -> MediaInfo: ...


class ASREngine(Protocol):
    version: str

    def transcribe(self, audio_path: Path) -> ASRResult: ...


class ConfigurableASREngine(ASREngine, Protocol):
    def transcribe_with_model(self, audio_path: Path, model: str) -> ASRResult: ...

    def version_for_model(self, model: str) -> str: ...


class DiarizationEngine(Protocol):
    version: str

    def diarize(self, audio_path: Path) -> list[SpeakerInterval]: ...


class TranslationEngine(Protocol):
    version: str

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult: ...
