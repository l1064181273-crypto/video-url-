from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from lvt.core.jobs import JobStatus
from lvt.core.models import JobOptions, Segment, Transcript, apply_translations
from lvt.core.processes import CancellationToken
from lvt.db.repository import (
    ArtifactCompletionResult,
    ArtifactSpec,
    JobRepository,
)
from lvt.engines.base import (
    ASREngine,
    ASRResult,
    ASRSegment,
    ConfigurableASREngine,
    DiarizationEngine,
    DownloadedMedia,
    Downloader,
    MediaInfo,
    SpeakerInterval,
    StagedDownloader,
    TranslationEngine,
)
from lvt.exporters.files import export_transcript
from lvt.pipeline.artifact_validation import validate_export_artifacts
from lvt.pipeline.checkpoints import (
    CHECKPOINT_STAGE_ORDER,
    STAGE_JOB_STATUS,
    CheckpointManifest,
    CheckpointResolution,
    CheckpointStage,
    CheckpointStore,
    PendingOutput,
    StageRequirement,
    stable_fingerprint,
)
from lvt.pipeline.segmenter import assign_speakers
from lvt.security.paths import ensure_within_root
from lvt.security.urls import validate_public_media_url

Segmenter = Callable[..., list[Segment]]
Exporter = Callable[[Transcript, Path], list[Path]]
IN_PROCESS_CANCELLATION_LIMITATION = (
    "MLX, sherpa-onnx and Ollama calls are checked immediately before and after "
    "each call; worst-case cancellation latency is the remaining call duration."
)


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
        segmenter: Segmenter = assign_speakers,
        exporter: Exporter = export_transcript,
        repository: JobRepository | None = None,
    ) -> None:
        self.downloader = downloader
        self.asr = asr
        self.diarizer = diarizer
        self.translator = translator
        self.segmenter = segmenter
        self.exporter = exporter
        self.work_root = work_root
        self.export_root = export_root
        self.repository = repository
        self.checkpoints = CheckpointStore(work_root)

    def run(self, *, job_id: str, url: str) -> PipelineResult:
        validated_url = validate_public_media_url(url)
        work_dir = ensure_within_root(self.work_root / job_id, self.work_root)
        work_dir.mkdir(parents=True, exist_ok=True)

        media = self.downloader.download(validated_url, work_dir)
        asr_result = self.asr.transcribe(media.audio_path)
        intervals = self.diarizer.diarize(media.audio_path)
        source_segments = self.segmenter(
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
        artifacts = self.exporter(transcript, self.export_root)
        return PipelineResult(transcript=transcript, artifacts=artifacts)

    def resolve_checkpoints(self, job_id: str) -> CheckpointResolution:
        repository = self._repository()
        job = repository.get(job_id)
        if job is None:
            raise KeyError(f"job does not exist: {job_id}")
        options = JobOptions.model_validate(job["options"])
        url = validate_public_media_url(str(job["original_url"]))
        return self.checkpoints.resolve(
            job_id=job_id,
            checkpoint_pointer=(
                str(job["checkpoint_pointer"]) if job["checkpoint_pointer"] else None
            ),
            source_url=url,
            requirements=self._requirements(options),
        )

    def resolve_first_required_stage(self, job_id: str) -> JobStatus:
        return self.resolve_checkpoints(job_id).first_required_stage

    @staticmethod
    def status_for_stage(stage: CheckpointStage) -> JobStatus:
        return STAGE_JOB_STATUS[stage]

    def run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken | None = None,
    ) -> PipelineResult:
        token = cancellation or CancellationToken()
        try:
            return self._run_claimed(job_id=job_id, run_id=run_id, cancellation=token)
        except BaseException:
            self.checkpoints.cleanup_unpublished_run(job_id, run_id)
            raise

    def _run_claimed(
        self,
        *,
        job_id: str,
        run_id: str,
        cancellation: CancellationToken,
    ) -> PipelineResult:
        repository = self._repository()
        cancellation.raise_if_cancelled()
        job = repository.get(job_id)
        if job is None:
            raise KeyError(f"job does not exist: {job_id}")
        options = JobOptions.model_validate(job["options"])
        url = validate_public_media_url(str(job["original_url"]))
        resolution = self.resolve_checkpoints(job_id)
        if job["active_run_id"] != run_id:
            raise RuntimeError("run_id does not own the job")
        if job["status"] != resolution.first_required_stage.value:
            raise RuntimeError("job status does not match first required checkpoint stage")

        manifests = dict(resolution.manifests)
        requirements = self._requirements(options)
        downloaded = self._load_downloaded(manifests.get(CheckpointStage.DOWNLOADED_MEDIA))
        media = self._load_media(manifests.get(CheckpointStage.NORMALIZED_AUDIO))
        asr_result = self._load_asr(manifests.get(CheckpointStage.ASR_RESULT))
        intervals = self._load_diarization(manifests.get(CheckpointStage.DIARIZATION_RESULT))
        source_segments = self._load_segments(manifests.get(CheckpointStage.SOURCE_TRANSCRIPT))
        transcript = self._load_transcript(manifests.get(CheckpointStage.TRANSLATED_TRANSCRIPT))

        start_index = len(manifests)
        for stage in CHECKPOINT_STAGE_ORDER[start_index:]:
            cancellation.raise_if_cancelled()
            previous = (
                manifests[CHECKPOINT_STAGE_ORDER[CHECKPOINT_STAGE_ORDER.index(stage) - 1]]
                if CHECKPOINT_STAGE_ORDER.index(stage) > 0
                else None
            )
            workspace = self.checkpoints.begin_stage(job_id, run_id, stage)
            if stage is CheckpointStage.DOWNLOADED_MEDIA:
                staged = self._staged_downloader()
                downloaded = staged.download_media(url, workspace.temporary_dir, cancellation)
                metadata_path = self.checkpoints.write_json(
                    workspace,
                    "downloaded-media.json",
                    {"title": downloaded.title, "filename": downloaded.media_path.name},
                )
                outputs = [
                    PendingOutput(downloaded.media_path, "downloaded_media", 1),
                    PendingOutput(metadata_path, "downloaded_metadata", 1),
                ]
            elif stage is CheckpointStage.NORMALIZED_AUDIO:
                if downloaded is None:
                    raise RuntimeError("downloaded media checkpoint is missing")
                media = self._staged_downloader().normalize_audio(
                    downloaded, workspace.temporary_dir, cancellation
                )
                metadata_path = self.checkpoints.write_json(
                    workspace,
                    "normalized-media.json",
                    {
                        "title": media.title,
                        "duration_ms": media.duration_ms,
                        "filename": media.audio_path.name,
                    },
                )
                outputs = [
                    PendingOutput(media.audio_path, "normalized_audio", 1),
                    PendingOutput(metadata_path, "normalized_metadata", 1),
                ]
            elif stage is CheckpointStage.ASR_RESULT:
                if media is None:
                    raise RuntimeError("normalized media checkpoint is missing")
                asr_result = self._transcribe(media.audio_path, options.asr_model)
                cancellation.raise_if_cancelled()
                output = self.checkpoints.write_json(
                    workspace,
                    "asr-result.json",
                    {
                        "language": asr_result.language,
                        "segments": [asdict(item) for item in asr_result.segments],
                    },
                )
                outputs = [PendingOutput(output, "asr_result", len(asr_result.segments))]
            elif stage is CheckpointStage.DIARIZATION_RESULT:
                if media is None:
                    raise RuntimeError("normalized media checkpoint is missing")
                if options.diarization:
                    intervals = self.diarizer.diarize(media.audio_path)
                    cancellation.raise_if_cancelled()
                    skipped = False
                else:
                    intervals = []
                    skipped = True
                output = self.checkpoints.write_json(
                    workspace,
                    "diarization-result.json",
                    {
                        "skipped": skipped,
                        "intervals": [asdict(item) for item in intervals],
                    },
                )
                outputs = [PendingOutput(output, "diarization_result", len(intervals))]
            elif stage is CheckpointStage.SOURCE_TRANSCRIPT:
                if asr_result is None or intervals is None:
                    raise RuntimeError("source transcript inputs are missing")
                source_segments = self.segmenter(
                    asr_result.segments,
                    intervals,
                    source_language=asr_result.language,
                )
                cancellation.raise_if_cancelled()
                output = self.checkpoints.write_json(
                    workspace,
                    "source-transcript.json",
                    {"segments": [item.model_dump(mode="json") for item in source_segments]},
                )
                outputs = [PendingOutput(output, "source_transcript", len(source_segments))]
            elif stage is CheckpointStage.TRANSLATED_TRANSCRIPT:
                if media is None or asr_result is None or source_segments is None:
                    raise RuntimeError("translation inputs are missing")
                translation = self.translator.translate(
                    {item.id: item.source_text for item in source_segments},
                    asr_result.language,
                )
                cancellation.raise_if_cancelled()
                translated_segments = apply_translations(source_segments, translation.texts)
                transcript = Transcript(
                    job_id=job_id,
                    source_url=url,
                    title=media.title,
                    duration_ms=media.duration_ms,
                    detected_language=asr_result.language,
                    engine_versions={
                        "downloader": self.downloader.version,
                        "asr": self._asr_version(options.asr_model),
                        "diarization": (
                            self.diarizer.version if options.diarization else "disabled"
                        ),
                        "translation": translation.engine_version,
                    },
                    processing_options=options.model_dump(mode="json"),
                    segments=translated_segments,
                    warnings=translation.warnings,
                )
                output = self.checkpoints.write_json(
                    workspace,
                    "translated-transcript.json",
                    transcript.model_dump(mode="json"),
                )
                outputs = [PendingOutput(output, "translated_transcript", len(transcript.segments))]
            else:
                if transcript is None:
                    raise RuntimeError("translated transcript checkpoint is missing")
                artifact_paths = self.exporter(transcript, workspace.temporary_dir)
                cancellation.raise_if_cancelled()
                validate_export_artifacts(transcript, artifact_paths)
                outputs = [
                    PendingOutput(path, path.name, len(transcript.segments))
                    for path in artifact_paths
                ]

            cancellation.raise_if_cancelled()
            manifest = self.checkpoints.publish(
                workspace,
                source_url=url,
                job_options=options.model_dump(mode="json"),
                requirement=requirements[stage],
                previous=previous,
                outputs=outputs,
                media_duration_ms=(
                    media.duration_ms
                    if stage is not CheckpointStage.DOWNLOADED_MEDIA and media is not None
                    else None
                ),
                transcript_schema_version=(
                    transcript.schema_version
                    if stage
                    in {
                        CheckpointStage.TRANSLATED_TRANSCRIPT,
                        CheckpointStage.EXPORT_MANIFEST,
                    }
                    and transcript is not None
                    else None
                ),
            )
            if stage is CheckpointStage.DOWNLOADED_MEDIA:
                downloaded = self._load_downloaded(manifest)
            elif stage is CheckpointStage.NORMALIZED_AUDIO:
                media = self._load_media(manifest)
            elif stage is CheckpointStage.ASR_RESULT:
                asr_result = self._load_asr(manifest)
            elif stage is CheckpointStage.DIARIZATION_RESULT:
                intervals = self._load_diarization(manifest)
            elif stage is CheckpointStage.SOURCE_TRANSCRIPT:
                source_segments = self._load_segments(manifest)
            elif stage is CheckpointStage.TRANSLATED_TRANSCRIPT:
                transcript = self._load_transcript(manifest)
            worker_title: str | None = None
            worker_duration_ms: int | None = None
            worker_language: str | None = None
            if stage is CheckpointStage.DOWNLOADED_MEDIA and downloaded is not None:
                worker_title = downloaded.title
            if stage is CheckpointStage.NORMALIZED_AUDIO and media is not None:
                worker_title = media.title
                worker_duration_ms = media.duration_ms
            if stage is CheckpointStage.ASR_RESULT and asr_result is not None:
                worker_language = asr_result.language
            expected_status = STAGE_JOB_STATUS[stage]
            if not repository.update_worker_metadata(
                job_id,
                run_id,
                expected_status,
                title=worker_title,
                duration_ms=worker_duration_ms,
                detected_language=worker_language,
                checkpoint_pointer=manifest.relative_manifest_path,
            ):
                self.checkpoints.discard_unpublished(manifest)
                raise RuntimeError("stale run cannot publish checkpoint")
            self.checkpoints.mark_published(manifest)
            manifests[stage] = manifest
            if stage is not CheckpointStage.EXPORT_MANIFEST:
                next_stage = CHECKPOINT_STAGE_ORDER[CHECKPOINT_STAGE_ORDER.index(stage) + 1]
                if not repository.advance_stage(
                    job_id,
                    run_id,
                    expected_status,
                    STAGE_JOB_STATUS[next_stage],
                ):
                    raise RuntimeError("stale run cannot advance checkpoint stage")

        if transcript is None:
            transcript = self._load_transcript(manifests.get(CheckpointStage.TRANSLATED_TRANSCRIPT))
        export_manifest = manifests.get(CheckpointStage.EXPORT_MANIFEST)
        if transcript is None or export_manifest is None:
            raise RuntimeError("completed checkpoint chain is incomplete")
        artifact_paths = [
            self.checkpoints.resolve_output_path(output) for output in export_manifest.outputs
        ]
        cancellation.raise_if_cancelled()
        validate_export_artifacts(transcript, artifact_paths)
        artifact_specs = [
            ArtifactSpec(
                artifact_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"lvt:{job_id}:{path.name}")),
                kind=path.name,
                path=path.relative_to(self.checkpoints.work_root).as_posix(),
            )
            for path in artifact_paths
        ]
        completion = repository.complete_job_with_artifacts(
            job_id=job_id,
            run_id=run_id,
            artifacts=artifact_specs,
        )
        if completion is not ArtifactCompletionResult.COMPLETED:
            raise RuntimeError(f"pipeline completion failed: {completion.value}")
        return PipelineResult(transcript=transcript, artifacts=artifact_paths)

    def _requirements(self, options: JobOptions) -> Mapping[CheckpointStage, StageRequirement]:
        definitions: dict[CheckpointStage, tuple[dict[str, object], dict[str, str]]] = {
            CheckpointStage.DOWNLOADED_MEDIA: (
                {},
                {"downloader": self._downloader_version()},
            ),
            CheckpointStage.NORMALIZED_AUDIO: (
                {"audio": "mono-16khz-pcm-s16le"},
                {"normalizer": self._normalizer_version()},
            ),
            CheckpointStage.ASR_RESULT: (
                {"asr_model": options.asr_model},
                {"asr": self._asr_version(options.asr_model)},
            ),
            CheckpointStage.DIARIZATION_RESULT: (
                {"diarization": options.diarization},
                {"diarization": (self.diarizer.version if options.diarization else "disabled")},
            ),
            CheckpointStage.SOURCE_TRANSCRIPT: (
                {"diarization": options.diarization},
                {"segmenter": str(getattr(self.segmenter, "version", "segmenter-v1"))},
            ),
            CheckpointStage.TRANSLATED_TRANSCRIPT: (
                {"translate_to": options.translate_to},
                {"translation": self.translator.version},
            ),
            CheckpointStage.EXPORT_MANIFEST: (
                {"translate_to": options.translate_to},
                {"exporter": str(getattr(self.exporter, "version", "exporter-v1"))},
            ),
        }
        return {
            stage: StageRequirement(
                options_fingerprint=stable_fingerprint(stage_options),
                engine_names={name: name for name in versions},
                engine_versions=versions,
                engine_fingerprint=stable_fingerprint(versions),
            )
            for stage, (stage_options, versions) in definitions.items()
        }

    def _staged_downloader(self) -> StagedDownloader:
        if not hasattr(self.downloader, "download_media") or not hasattr(
            self.downloader, "normalize_audio"
        ):
            raise TypeError("checkpoint Pipeline requires a staged downloader")
        return cast(StagedDownloader, self.downloader)

    def _transcribe(self, audio_path: Path, model: str) -> ASRResult:
        if hasattr(self.asr, "transcribe_with_model"):
            return cast(ConfigurableASREngine, self.asr).transcribe_with_model(audio_path, model)
        return self.asr.transcribe(audio_path)

    def _asr_version(self, model: str) -> str:
        if hasattr(self.asr, "version_for_model"):
            return cast(ConfigurableASREngine, self.asr).version_for_model(model)
        return f"{self.asr.version};model={model}"

    def _downloader_version(self) -> str:
        return str(getattr(self.downloader, "downloader_version", self.downloader.version))

    def _normalizer_version(self) -> str:
        return str(getattr(self.downloader, "normalizer_version", self.downloader.version))

    def _repository(self) -> JobRepository:
        if self.repository is None:
            raise RuntimeError("checkpoint Pipeline requires a JobRepository")
        return self.repository

    def _load_downloaded(self, manifest: CheckpointManifest | None) -> DownloadedMedia | None:
        if manifest is None:
            return None
        media_path = self._output_by_kind(manifest, "downloaded_media")
        metadata = self._read_json(self._output_by_kind(manifest, "downloaded_metadata"))
        return DownloadedMedia(media_path=media_path, title=str(metadata["title"]))

    def _load_media(self, manifest: CheckpointManifest | None) -> MediaInfo | None:
        if manifest is None:
            return None
        audio_path = self._output_by_kind(manifest, "normalized_audio")
        metadata = self._read_json(self._output_by_kind(manifest, "normalized_metadata"))
        return MediaInfo(
            audio_path=audio_path,
            title=str(metadata["title"]),
            duration_ms=int(metadata["duration_ms"]),
        )

    def _load_asr(self, manifest: CheckpointManifest | None) -> ASRResult | None:
        if manifest is None:
            return None
        payload = self._read_json(self._output_by_kind(manifest, "asr_result"))
        return ASRResult(
            language=str(payload["language"]),
            segments=[ASRSegment(**item) for item in payload["segments"]],
        )

    def _load_diarization(
        self, manifest: CheckpointManifest | None
    ) -> list[SpeakerInterval] | None:
        if manifest is None:
            return None
        payload = self._read_json(self._output_by_kind(manifest, "diarization_result"))
        return [SpeakerInterval(**item) for item in payload["intervals"]]

    def _load_segments(self, manifest: CheckpointManifest | None) -> list[Segment] | None:
        if manifest is None:
            return None
        payload = self._read_json(self._output_by_kind(manifest, "source_transcript"))
        return [Segment.model_validate(item) for item in payload["segments"]]

    def _load_transcript(self, manifest: CheckpointManifest | None) -> Transcript | None:
        if manifest is None:
            return None
        return Transcript.model_validate(
            self._read_json(self._output_by_kind(manifest, "translated_transcript"))
        )

    def _output_by_kind(self, manifest: CheckpointManifest, kind: str) -> Path:
        output = next(item for item in manifest.outputs if item.kind == kind)
        return self.checkpoints.resolve_output_path(output)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return value
