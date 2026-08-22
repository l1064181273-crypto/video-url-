from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lvt.core.models import Transcript, apply_translations
from lvt.engines.base import ASREngine, DiarizationEngine, Downloader, TranslationEngine
from lvt.exporters.files import export_transcript
from lvt.pipeline.segmenter import assign_speakers
from lvt.security.paths import ensure_within_root
from lvt.security.urls import validate_public_media_url


@dataclass(frozen=True)
class PipelineResult:
    transcript: Transcript
    artifacts: list[Path]


class Pipeline:
    def __init__(
        self,
        *,
        downloader: Downloader,
        asr: ASREngine,
        diarizer: DiarizationEngine,
        translator: TranslationEngine,
        work_root: Path,
        export_root: Path,
    ) -> None:
        self.downloader = downloader
        self.asr = asr
        self.diarizer = diarizer
        self.translator = translator
        self.work_root = work_root
        self.export_root = export_root

    def run(self, *, job_id: str, url: str) -> PipelineResult:
        validated_url = validate_public_media_url(url)
        work_dir = ensure_within_root(self.work_root / job_id, self.work_root)
        work_dir.mkdir(parents=True, exist_ok=True)

        media = self.downloader.download(validated_url, work_dir)
        asr_result = self.asr.transcribe(media.audio_path)
        intervals = self.diarizer.diarize(media.audio_path)
        source_segments = assign_speakers(
            asr_result.segments,
            intervals,
            source_language=asr_result.language,
        )
        translation_result = self.translator.translate(
            {segment.id: segment.source_text for segment in source_segments},
            asr_result.language,
        )
        translated_segments = apply_translations(source_segments, translation_result.texts)
        transcript = Transcript(
            job_id=job_id,
            source_url=validated_url,
            title=media.title,
            duration_ms=media.duration_ms,
            detected_language=asr_result.language,
            engine_versions={
                "downloader": self.downloader.version,
                "asr": self.asr.version,
                "diarization": self.diarizer.version,
                "translation": translation_result.engine_version,
            },
            processing_options={"diarization": True, "translate_to": "zh-CN"},
            segments=translated_segments,
            warnings=translation_result.warnings,
        )
        (work_dir / "transcript.normalized.json").write_text(
            json.dumps(transcript.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        artifacts = export_transcript(transcript, self.export_root)
        return PipelineResult(transcript=transcript, artifacts=artifacts)
