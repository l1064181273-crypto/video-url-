import hashlib
import json
import os
import sqlite3
import wave
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import srt
import webvtt

from lvt.core.jobs import ErrorCode, JobStatus
from lvt.core.models import DEFAULT_ASR_MODEL
from lvt.core.processes import CancellationToken, ProcessCancelledError
from lvt.db.repository import (
    ArtifactCompletionResult,
    ArtifactRegistrationResult,
    ArtifactSpec,
    AutomaticRequeueResult,
    JobRepository,
)
from lvt.engines.base import (
    ASRResult,
    ASRSegment,
    DownloadedMedia,
    MediaInfo,
    SpeakerInterval,
    TranslationResult,
)
from lvt.exporters.files import export_transcript
from lvt.pipeline.checkpoints import (
    CHECKPOINT_STAGE_ORDER,
    CheckpointStage,
    stable_fingerprint,
)
from lvt.pipeline.runner import Pipeline
from lvt.pipeline.segmenter import assign_speakers
from lvt.workers.progress import ProgressReporter


class CountingDownloader:
    def __init__(
        self,
        calls: Counter[str],
        *,
        downloader_version: str = "fake-downloader-1",
        normalizer_version: str = "fake-normalizer-1",
    ) -> None:
        self.calls = calls
        self.downloader_version = downloader_version
        self.normalizer_version = normalizer_version
        self.version = f"{downloader_version};{normalizer_version}"

    def download_media(
        self, url: str, work_dir: Path, _cancellation: Any = None
    ) -> DownloadedMedia:
        self.calls["downloaded_media"] += 1
        path = work_dir / "download.bin"
        path.write_bytes(b"downloaded-media")
        return DownloadedMedia(media_path=path, title="Checkpoint Sample")

    def normalize_audio(
        self,
        media: DownloadedMedia,
        work_dir: Path,
        _cancellation: Any = None,
    ) -> MediaInfo:
        self.calls["normalized_audio"] += 1
        assert media.media_path.is_file()
        assert media.media_path.read_bytes() == b"downloaded-media"
        assert CheckpointStage.DOWNLOADED_MEDIA.value in media.media_path.parts
        path = work_dir / "audio.normalized.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(1_000)
            output.writeframes(b"\0\0" * 6_000)
        return MediaInfo(audio_path=path, title=media.title, duration_ms=6_000)

    def download(self, url: str, work_dir: Path, _cancellation: Any = None) -> MediaInfo:
        return self.normalize_audio(
            self.download_media(url, work_dir, _cancellation),
            work_dir,
            _cancellation,
        )


class CountingASR:
    def __init__(self, calls: Counter[str], version: str = "fake-asr-1") -> None:
        self.calls = calls
        self.version = version

    def transcribe(self, audio_path: Path) -> ASRResult:
        return self.transcribe_with_model(audio_path, "legacy-default")

    def transcribe_with_model(self, audio_path: Path, model: str) -> ASRResult:
        self.calls["asr_result"] += 1
        self.calls[f"asr_model:{model}"] += 1
        assert audio_path.is_file()
        assert CheckpointStage.NORMALIZED_AUDIO.value in audio_path.parts
        with wave.open(str(audio_path), "rb") as input_audio:
            assert input_audio.getnframes() == 6_000
        return ASRResult(
            language="en",
            segments=[
                ASRSegment(0, 2_500, "Hello world."),
                ASRSegment(2_600, 5_900, "Local processing in 2026."),
            ],
        )


class CountingDiarizer:
    def __init__(self, calls: Counter[str], version: str = "fake-diarizer-1") -> None:
        self.calls = calls
        self.version = version

    def diarize(self, audio_path: Path) -> list[SpeakerInterval]:
        self.calls["diarization_result"] += 1
        return [
            SpeakerInterval(0, 2_500, "A"),
            SpeakerInterval(2_600, 5_900, "B"),
        ]


class CountingTranslator:
    def __init__(self, calls: Counter[str], version: str = "fake-translator-1") -> None:
        self.calls = calls
        self.version = version

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        self.calls["translated_transcript"] += 1
        return TranslationResult(
            texts={1: "你好，世界。", 2: "在 2026 年进行本地处理。"},
            engine_version=self.version,
            warnings=[],
        )


class CountingSegmenter:
    def __init__(self, calls: Counter[str], version: str = "fake-segmenter-1") -> None:
        self.calls = calls
        self.version = version

    def __call__(
        self,
        asr_segments: list[ASRSegment],
        intervals: list[SpeakerInterval],
        *,
        source_language: str,
    ) -> list[Any]:
        self.calls["source_transcript"] += 1
        return assign_speakers(
            asr_segments,
            intervals,
            source_language=source_language,
        )


class CountingExporter:
    def __init__(self, calls: Counter[str], version: str = "fake-exporter-1") -> None:
        self.calls = calls
        self.version = version

    def __call__(self, transcript: Any, export_root: Path) -> list[Path]:
        self.calls["export_manifest"] += 1
        return export_transcript(transcript, export_root)


def _build_pipeline(
    tmp_path: Path,
    repository: JobRepository,
    calls: Counter[str],
    *,
    asr_version: str = "fake-asr-1",
    downloader_version: str = "fake-downloader-1",
    normalizer_version: str = "fake-normalizer-1",
    diarizer_version: str = "fake-diarizer-1",
    segmenter_version: str = "fake-segmenter-1",
    translator_version: str = "fake-translator-1",
    exporter_version: str = "fake-exporter-1",
    downloader: Any | None = None,
    exporter: Any | None = None,
) -> Pipeline:
    return Pipeline(
        downloader=downloader
        or CountingDownloader(
            calls,
            downloader_version=downloader_version,
            normalizer_version=normalizer_version,
        ),
        asr=CountingASR(calls, asr_version),
        diarizer=CountingDiarizer(calls, diarizer_version),
        translator=CountingTranslator(calls, translator_version),
        segmenter=CountingSegmenter(calls, segmenter_version),
        exporter=exporter or CountingExporter(calls, exporter_version),
        work_root=tmp_path / "work",
        export_root=tmp_path / "legacy-exports",
        repository=repository,
    )


def _create_job(
    repository: JobRepository,
    *,
    asr_model: str = "fake-asr-model",
    diarization: bool = True,
    translate_to: str = "zh-CN",
) -> str:
    job = repository.create(
        "https://example.test/video",
        {
            "asr_model": asr_model,
            "diarization": diarization,
            "translate_to": translate_to,
        },
    )
    return str(job["uuid"])


def _claim_and_run(
    pipeline: Pipeline,
    repository: JobRepository,
    job_id: str,
    *,
    progress_callback: Any = None,
) -> Any:
    first_stage = pipeline.resolve_first_required_stage(job_id)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=first_stage,
    )
    assert claimed is not None
    return pipeline.run_claimed(
        job_id=job_id,
        run_id=str(claimed["active_run_id"]),
        progress_callback=progress_callback,
    )


def _prime_failed_completion(
    pipeline: Pipeline,
    repository: JobRepository,
    job_id: str,
) -> None:
    first_stage = pipeline.resolve_first_required_stage(job_id)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=first_stage,
    )
    assert claimed is not None
    run_id = str(claimed["active_run_id"])
    with repository._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_pipeline_completed_event
            BEFORE INSERT ON job_events
            WHEN NEW.status = 'completed'
            BEGIN
                SELECT RAISE(FAIL, 'injected pipeline completion failure');
            END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected pipeline completion failure"):
        pipeline.run_claimed(job_id=job_id, run_id=run_id)
    with repository._connect() as connection:
        connection.execute("DROP TRIGGER fail_pipeline_completed_event")
    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.EXPORTING.value
    assert persisted["checkpoint_pointer"]
    assert repository.list_artifacts(job_id) == []
    assert repository.fail_job(
        job_id,
        run_id,
        JobStatus.EXPORTING,
        ErrorCode.EXPORT_FAILED,
        "injected completion failure",
    )
    assert repository.manual_retry(job_id, JobStatus.FAILED)


def test_checkpoint_pipeline_reads_persisted_options_and_skips_diarization(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(
        repository,
        asr_model="persisted-asr",
        diarization=False,
        translate_to="zh-CN",
    )

    progress: list[tuple[JobStatus, int]] = []
    result = _claim_and_run(
        pipeline,
        repository,
        job_id,
        progress_callback=lambda status, value: progress.append((status, value)),
    )

    assert calls["diarization_result"] == 0
    assert calls["asr_model:persisted-asr"] == 1
    assert result.transcript.processing_options == {
        "asr_model": "persisted-asr",
        "diarization": False,
        "translate_to": "zh-CN",
    }
    assert result.transcript.engine_versions["asr"].endswith("model=persisted-asr")
    assert [segment.speaker for segment in result.transcript.segments] == [
        "Speaker 1",
        "Speaker 1",
    ]
    assert len(result.artifacts) == 8
    assert repository.get(job_id)["status"] == JobStatus.COMPLETED.value  # type: ignore[index]
    assert len(repository.list_artifacts(job_id)) == 8
    assert progress == [
        item
        for status in (
            JobStatus.DOWNLOADING,
            JobStatus.EXTRACTING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.SEGMENTING,
            JobStatus.TRANSLATING,
            JobStatus.EXPORTING,
        )
        for item in ((status, 0), (status, 100))
    ]
    output_dir = result.artifacts[0].parent
    source = json.loads((output_dir / "source.json").read_text(encoding="utf-8"))
    translated = json.loads((output_dir / "zh-CN.json").read_text(encoding="utf-8"))
    immutable_fields = (
        "id",
        "start_ms",
        "end_ms",
        "speaker",
        "source_language",
        "source_text",
        "metadata",
    )
    assert len(source["segments"]) == len(translated["segments"]) == 2
    for source_segment, translated_segment in zip(
        source["segments"], translated["segments"], strict=True
    ):
        assert {field: source_segment[field] for field in immutable_fields} == {
            field: translated_segment[field] for field in immutable_fields
        }
        assert source_segment["translated_text"] == ""
        assert translated_segment["translated_text"]
    assert len(list(srt.parse((output_dir / "source.srt").read_text(encoding="utf-8")))) == 2
    assert len(list(srt.parse((output_dir / "zh-CN.srt").read_text(encoding="utf-8")))) == 2
    assert len(webvtt.read(str(output_dir / "source.vtt")).captions) == 2
    assert len(webvtt.read(str(output_dir / "zh-CN.vtt")).captions) == 2
    resolution = pipeline.resolve_checkpoints(job_id)
    assert tuple(resolution.manifests) == CHECKPOINT_STAGE_ORDER
    previous = None
    for stage, manifest in resolution.manifests.items():
        assert manifest.schema_version == 1
        assert manifest.job_id == job_id
        assert manifest.stage is stage
        assert manifest.run_id
        assert manifest.created_at.endswith("+00:00")
        assert len(manifest.source_url_sha256) == 64
        assert manifest.job_options == {
            "asr_model": "persisted-asr",
            "diarization": False,
            "translate_to": "zh-CN",
        }
        assert manifest.options_fingerprint
        assert manifest.engine_names
        assert manifest.engine_versions
        assert manifest.engine_fingerprint
        assert all(not Path(output.relative_path).is_absolute() for output in manifest.outputs)
        if stage is CheckpointStage.DOWNLOADED_MEDIA:
            assert manifest.media_duration_ms is None
        else:
            assert manifest.media_duration_ms == 6_000
        if stage in {
            CheckpointStage.TRANSLATED_TRANSCRIPT,
            CheckpointStage.EXPORT_MANIFEST,
        }:
            assert manifest.transcript_schema_version == "1.0"
        else:
            assert manifest.transcript_schema_version is None
        if previous is None:
            assert manifest.input_checkpoint_fingerprints == {}
        else:
            assert manifest.input_checkpoint_fingerprints == {
                previous.stage.value: previous.manifest_fingerprint
            }
        previous = manifest


def test_api_default_model_reaches_configurable_asr_adapter(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job = repository.create(
        "https://example.test/default-model",
        {"diarization": False, "translate_to": "zh-CN", "asr_model": "default"},
    )

    result = _claim_and_run(pipeline, repository, str(job["uuid"]))

    assert calls[f"asr_model:{DEFAULT_ASR_MODEL}"] == 1
    assert result.transcript.processing_options["asr_model"] == DEFAULT_ASR_MODEL
    assert result.transcript.engine_versions["asr"].endswith(f"model={DEFAULT_ASR_MODEL}")


def test_valid_checkpoint_chain_reuses_every_engine_and_exporter(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    before = calls.copy()

    resolution = pipeline.resolve_checkpoints(job_id)
    assert resolution.first_required_stage is JobStatus.EXPORTING
    result = _claim_and_run(pipeline, repository, job_id)

    assert calls == before
    assert len(result.artifacts) == 8
    assert repository.get(job_id)["status"] == JobStatus.COMPLETED.value  # type: ignore[index]


@pytest.mark.parametrize(
    ("crash_stage", "resume_status"),
    [
        (CheckpointStage.DOWNLOADED_MEDIA, JobStatus.EXTRACTING),
        (CheckpointStage.NORMALIZED_AUDIO, JobStatus.TRANSCRIBING),
        (CheckpointStage.ASR_RESULT, JobStatus.DIARIZING),
        (CheckpointStage.DIARIZATION_RESULT, JobStatus.SEGMENTING),
        (CheckpointStage.SOURCE_TRANSCRIPT, JobStatus.TRANSLATING),
        (CheckpointStage.TRANSLATED_TRANSCRIPT, JobStatus.EXPORTING),
        (CheckpointStage.EXPORT_MANIFEST, JobStatus.EXPORTING),
    ],
)
def test_startup_recovery_resumes_each_published_checkpoint_with_new_run(
    tmp_path: Path,
    crash_stage: CheckpointStage,
    resume_status: JobStatus,
) -> None:
    repository = JobRepository(tmp_path / crash_stage.value / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path / crash_stage.value, repository, calls)
    job_id = _create_job(repository)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claimed is not None
    old_run_id = str(claimed["active_run_id"])
    reporter = ProgressReporter(repository, job_id, old_run_id, high_water=0)

    def crash_after_publish(status: JobStatus, progress: int) -> None:
        reporter(status, progress)
        if status is pipeline.status_for_stage(crash_stage) and progress == 100:
            raise RuntimeError(f"simulated crash after {crash_stage.value}")

    with pytest.raises(RuntimeError, match="simulated crash"):
        pipeline.run_claimed(
            job_id=job_id,
            run_id=old_run_id,
            progress_callback=crash_after_publish,
        )
    before_resume = calls.copy()
    interrupted = repository.get(job_id)
    assert interrupted is not None
    previous_high_water = int(interrupted["overall_progress"])
    assert interrupted["status"] == pipeline.status_for_stage(crash_stage).value

    summary = repository.recover_startup()

    assert summary.interrupted_requeued == 1
    recovered = repository.get(job_id)
    assert recovered is not None
    assert recovered["status"] == JobStatus.QUEUED.value
    assert recovered["active_run_id"] is None
    assert recovered["overall_progress"] == previous_high_water
    assert pipeline.resolve_first_required_stage(job_id) is resume_status

    second_claim = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=resume_status,
    )
    assert second_claim is not None
    new_run_id = str(second_claim["active_run_id"])
    assert new_run_id != old_run_id
    result = pipeline.run_claimed(job_id=job_id, run_id=new_run_id)

    crash_index = CHECKPOINT_STAGE_ORDER.index(crash_stage)
    for index, stage in enumerate(CHECKPOINT_STAGE_ORDER):
        expected_increment = 1 if index > crash_index else 0
        assert calls[stage.value] == before_resume[stage.value] + expected_increment
    assert len(result.artifacts) == 8
    assert len(repository.list_artifacts(job_id)) == 8
    assert repository.get(job_id)["status"] == JobStatus.COMPLETED.value  # type: ignore[index]
    artifact_paths = {path.name: path for path in result.artifacts}
    source_payload = json.loads(artifact_paths["source.json"].read_text(encoding="utf-8"))
    translated_payload = json.loads(artifact_paths["zh-CN.json"].read_text(encoding="utf-8"))
    immutable_fields = (
        "id",
        "start_ms",
        "end_ms",
        "speaker",
        "source_language",
        "source_text",
        "metadata",
    )
    for source_segment, translated_segment in zip(
        source_payload["segments"],
        translated_payload["segments"],
        strict=True,
    ):
        assert {field: source_segment[field] for field in immutable_fields} == {
            field: translated_segment[field] for field in immutable_fields
        }
        assert source_segment["translated_text"] == ""
        assert translated_segment["translated_text"]

    assert not repository.update_progress(
        job_id,
        old_run_id,
        pipeline.status_for_stage(crash_stage),
        stage_progress=100,
        overall_progress=100,
    )
    assert not repository.update_worker_metadata(
        job_id,
        old_run_id,
        pipeline.status_for_stage(crash_stage),
        title="stale",
    )
    assert not repository.fail_job(
        job_id,
        old_run_id,
        pipeline.status_for_stage(crash_stage),
        ErrorCode.INTERNAL_ERROR,
        "stale",
    )
    assert (
        repository.register_artifact(
            job_id=job_id,
            run_id=old_run_id,
            expected_status=pipeline.status_for_stage(crash_stage),
            artifact_id="stale-artifact",
            kind="source.txt",
            path="stale/source.txt",
        )
        is ArtifactRegistrationResult.STALE
    )
    completed_artifacts = [
        ArtifactSpec(
            artifact_id=str(item["id"]),
            kind=str(item["kind"]),
            path=str(item["path"]),
        )
        for item in repository.list_artifacts(job_id)
    ]
    assert (
        repository.complete_job_with_artifacts(
            job_id=job_id,
            run_id=old_run_id,
            artifacts=completed_artifacts,
        )
        is ArtifactCompletionResult.STALE
    )
    pipeline.checkpoints.cleanup_unpublished_run(job_id, old_run_id)
    assert all(path.is_file() for path in result.artifacts)


@pytest.mark.parametrize("stage", CHECKPOINT_STAGE_ORDER)
def test_corrupt_stage_reruns_only_that_stage_and_downstream(
    tmp_path: Path, stage: CheckpointStage
) -> None:
    repository = JobRepository(tmp_path / stage.value / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path / stage.value, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    before = calls.copy()
    resolution = pipeline.resolve_checkpoints(job_id)
    manifest = resolution.manifests[stage]
    output = pipeline.checkpoints.resolve_output_path(manifest.outputs[0])
    output.write_bytes(output.read_bytes() + b"corrupt")

    invalidated = pipeline.resolve_checkpoints(job_id)
    expected_status = pipeline.status_for_stage(stage)
    assert invalidated.first_required_stage is expected_status
    _claim_and_run(pipeline, repository, job_id)
    repaired = pipeline.resolve_checkpoints(job_id).manifests[stage]

    stage_index = CHECKPOINT_STAGE_ORDER.index(stage)
    for index, candidate in enumerate(CHECKPOINT_STAGE_ORDER):
        expected_increment = 1 if index >= stage_index else 0
        assert calls[candidate.value] == before[candidate.value] + expected_increment
    assert repaired.run_id != manifest.run_id
    assert repaired.outputs[0].relative_path != manifest.outputs[0].relative_path


@pytest.mark.parametrize(
    ("options_update", "expected_stage"),
    [
        ({"asr_model": "changed-asr"}, CheckpointStage.ASR_RESULT),
        ({"diarization": False}, CheckpointStage.DIARIZATION_RESULT),
        ({"translate_to": "zh-Hans"}, CheckpointStage.TRANSLATED_TRANSCRIPT),
    ],
)
def test_option_fingerprint_change_invalidates_from_affected_stage(
    tmp_path: Path,
    options_update: dict[str, object],
    expected_stage: CheckpointStage,
) -> None:
    repository = JobRepository(tmp_path / expected_stage.value / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path / expected_stage.value, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    persisted = repository.get(job_id)
    assert persisted is not None
    options = dict(persisted["options"])
    options.update(options_update)
    with repository._connect() as connection:
        connection.execute(
            "UPDATE jobs SET options_json = ? WHERE uuid = ?",
            (json.dumps(options, sort_keys=True), job_id),
        )

    resolution = pipeline.resolve_checkpoints(job_id)

    assert resolution.first_required_stage is pipeline.status_for_stage(expected_stage)


@pytest.mark.parametrize(
    ("version_update", "expected_status"),
    [
        ({"downloader_version": "fake-downloader-2"}, JobStatus.DOWNLOADING),
        ({"normalizer_version": "fake-normalizer-2"}, JobStatus.EXTRACTING),
        ({"asr_version": "fake-asr-2"}, JobStatus.TRANSCRIBING),
        ({"diarizer_version": "fake-diarizer-2"}, JobStatus.DIARIZING),
        ({"segmenter_version": "fake-segmenter-2"}, JobStatus.SEGMENTING),
        ({"translator_version": "fake-translator-2"}, JobStatus.TRANSLATING),
        ({"exporter_version": "fake-exporter-2"}, JobStatus.EXPORTING),
    ],
)
def test_each_engine_version_invalidates_from_its_own_stage(
    tmp_path: Path,
    version_update: dict[str, str],
    expected_status: JobStatus,
) -> None:
    repository = JobRepository(tmp_path / expected_status.value / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path / expected_status.value, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)

    changed = _build_pipeline(
        tmp_path / expected_status.value,
        repository,
        calls,
        **version_update,
    )

    assert changed.resolve_checkpoints(job_id).first_required_stage is expected_status


def test_truncated_manifest_record_count_and_path_traversal_are_rejected(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    resolution = pipeline.resolve_checkpoints(job_id)

    translated = resolution.manifests[CheckpointStage.TRANSLATED_TRANSCRIPT]
    translated_path = pipeline.checkpoints.manifest_path(translated)
    translated_payload = json.loads(translated_path.read_text(encoding="utf-8"))
    translated_payload["outputs"][0]["record_count"] += 1
    translated_path.write_text(json.dumps(translated_payload), encoding="utf-8")
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.TRANSLATING

    source = resolution.manifests[CheckpointStage.SOURCE_TRANSCRIPT]
    source_path = pipeline.checkpoints.manifest_path(source)
    source_payload = json.loads(source_path.read_text(encoding="utf-8"))
    source_payload["outputs"][0]["relative_path"] = "../../escape.json"
    source_path.write_text(json.dumps(source_payload), encoding="utf-8")
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.SEGMENTING

    asr = resolution.manifests[CheckpointStage.ASR_RESULT]
    pipeline.checkpoints.manifest_path(asr).write_text("{", encoding="utf-8")
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.TRANSCRIBING


def test_normalized_audio_is_actually_probed_during_recovery(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    normalized = pipeline.resolve_checkpoints(job_id).manifests[CheckpointStage.NORMALIZED_AUDIO]
    audio_output = next(
        output for output in normalized.outputs if output.kind == "normalized_audio"
    )
    audio_path = pipeline.checkpoints.resolve_output_path(audio_output)
    audio_path.write_bytes(b"not-a-wave-file")
    manifest_path = pipeline.checkpoints.manifest_path(normalized)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_payload = next(item for item in payload["outputs"] if item["kind"] == "normalized_audio")
    output_payload["byte_size"] = audio_path.stat().st_size
    output_payload["sha256"] = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    payload.pop("manifest_fingerprint")
    payload["manifest_fingerprint"] = stable_fingerprint(payload)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.EXTRACTING


def test_symlink_output_is_rejected(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    resolution = pipeline.resolve_checkpoints(job_id)
    manifest = resolution.manifests[CheckpointStage.ASR_RESULT]
    output = pipeline.checkpoints.resolve_output_path(manifest.outputs[0])
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    output.unlink()
    os.symlink(outside, output)

    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.TRANSCRIBING


def test_internal_output_manifest_and_marker_symlinks_are_rejected(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)
    resolution = pipeline.resolve_checkpoints(job_id)

    asr = resolution.manifests[CheckpointStage.ASR_RESULT]
    asr_output = pipeline.checkpoints.resolve_output_path(asr.outputs[0])
    internal_target = pipeline.checkpoints.resolve_output_path(
        resolution.manifests[CheckpointStage.SOURCE_TRANSCRIPT].outputs[0]
    )
    asr_output.unlink()
    os.symlink(internal_target, asr_output)
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.TRANSCRIBING

    normalized = resolution.manifests[CheckpointStage.NORMALIZED_AUDIO]
    normalized_manifest = pipeline.checkpoints.manifest_path(normalized)
    normalized_manifest.unlink()
    os.symlink(pipeline.checkpoints.manifest_path(asr), normalized_manifest)
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.EXTRACTING

    downloaded = resolution.manifests[CheckpointStage.DOWNLOADED_MEDIA]
    marker = pipeline.checkpoints.manifest_path(downloaded).parent / ".published"
    marker.unlink()
    os.symlink(internal_target, marker)
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.DOWNLOADING


def test_run_directories_are_isolated_and_stale_cleanup_cannot_touch_current_run(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    first = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert first is not None
    first_run = str(first["active_run_id"])
    now = datetime.now(UTC)
    assert (
        repository.automatic_requeue(
            job_id=job_id,
            run_id=first_run,
            expected_status=JobStatus.DOWNLOADING,
            error_code=ErrorCode.DOWNLOAD_FAILED,
            error_message="retry",
            next_attempt_at=now,
        )
        is AutomaticRequeueResult.REQUEUED
    )
    second = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
        now=now,
    )
    assert second is not None
    second_run = str(second["active_run_id"])
    stale_dir = pipeline.checkpoints.run_root(job_id, first_run)
    current_dir = pipeline.checkpoints.run_root(job_id, second_run)
    stale_dir.mkdir(parents=True)
    current_dir.mkdir(parents=True)
    (stale_dir / "unpublished.tmp").write_text("stale", encoding="utf-8")
    current_file = current_dir / "current.tmp"
    current_file.write_text("current", encoding="utf-8")

    assert not repository.update_worker_metadata(
        job_id,
        first_run,
        JobStatus.DOWNLOADING,
        checkpoint_pointer="stale/manifest.json",
    )
    cancellation = CancellationToken()
    cancellation.cancel()
    with pytest.raises(ProcessCancelledError):
        pipeline.run_claimed(
            job_id=job_id,
            run_id=first_run,
            cancellation=cancellation,
        )

    assert not stale_dir.exists()
    assert current_file.read_text(encoding="utf-8") == "current"


def test_stale_cleanup_rejects_symlink_to_current_run(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    stale_run = "stale-run"
    current_run = "current-run"
    stale_dir = pipeline.checkpoints.run_root(job_id, stale_run)
    current_dir = pipeline.checkpoints.run_root(job_id, current_run)
    current_dir.mkdir(parents=True)
    current_file = current_dir / "current.txt"
    current_file.write_text("current", encoding="utf-8")
    stale_dir.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(current_dir, stale_dir)

    with pytest.raises(ValueError, match="symlink"):
        pipeline.checkpoints.cleanup_unpublished_run(job_id, stale_run)

    assert current_file.read_text(encoding="utf-8") == "current"


class CancellingDownloader(CountingDownloader):
    def __init__(
        self,
        calls: Counter[str],
        token: CancellationToken,
        cancel_stage: CheckpointStage,
    ) -> None:
        super().__init__(calls)
        self.token = token
        self.cancel_stage = cancel_stage

    def download_media(
        self,
        url: str,
        work_dir: Path,
        _cancellation: Any = None,
    ) -> DownloadedMedia:
        media = super().download_media(url, work_dir, _cancellation)
        if self.cancel_stage is CheckpointStage.DOWNLOADED_MEDIA:
            self.token.cancel()
            self.token.raise_if_cancelled()
        return media

    def normalize_audio(
        self,
        media: DownloadedMedia,
        work_dir: Path,
        _cancellation: Any = None,
    ) -> MediaInfo:
        normalized = super().normalize_audio(media, work_dir, _cancellation)
        if self.cancel_stage is CheckpointStage.NORMALIZED_AUDIO:
            self.token.cancel()
            self.token.raise_if_cancelled()
        return normalized


def test_download_cancellation_removes_temporary_stage_without_publishing(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    token = CancellationToken()
    pipeline = _build_pipeline(
        tmp_path,
        repository,
        calls,
        downloader=CancellingDownloader(calls, token, CheckpointStage.DOWNLOADED_MEDIA),
    )
    job_id = _create_job(repository)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claimed is not None
    run_id = str(claimed["active_run_id"])

    with pytest.raises(ProcessCancelledError):
        pipeline.run_claimed(job_id=job_id, run_id=run_id, cancellation=token)

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.DOWNLOADING.value
    assert persisted["checkpoint_pointer"] is None
    assert not pipeline.checkpoints.run_root(job_id, run_id).exists()


def test_normalize_cancellation_keeps_download_cache_and_retry_reuses_it(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    token = CancellationToken()
    pipeline = _build_pipeline(
        tmp_path,
        repository,
        calls,
        downloader=CancellingDownloader(calls, token, CheckpointStage.NORMALIZED_AUDIO),
    )
    job_id = _create_job(repository)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=JobStatus.DOWNLOADING,
    )
    assert claimed is not None
    run_id = str(claimed["active_run_id"])

    with pytest.raises(ProcessCancelledError):
        pipeline.run_claimed(job_id=job_id, run_id=run_id, cancellation=token)

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.EXTRACTING.value
    assert CheckpointStage.DOWNLOADED_MEDIA.value in str(persisted["checkpoint_pointer"])
    assert pipeline.resolve_checkpoints(job_id).first_required_stage is JobStatus.EXTRACTING
    assert calls["downloaded_media"] == 1
    assert calls["normalized_audio"] == 1

    retry_pipeline = _build_pipeline(tmp_path, repository, calls)
    result = retry_pipeline.run_claimed(job_id=job_id, run_id=run_id)

    assert calls["downloaded_media"] == 1
    assert calls["normalized_audio"] == 2
    assert len(result.artifacts) == 8


class CorruptingExporter:
    version = "corrupting-exporter-1"

    def __init__(self, mutation: str) -> None:
        self.mutation = mutation

    def __call__(self, transcript: Any, export_root: Path) -> list[Path]:
        paths = export_transcript(transcript, export_root)
        output_dir = paths[0].parent
        if self.mutation in {"speaker", "timestamp", "id", "order", "source_text"}:
            path = output_dir / "zh-CN.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            if self.mutation == "speaker":
                payload["segments"][0]["speaker"] = "Speaker 9"
            elif self.mutation == "timestamp":
                payload["segments"][0]["start_ms"] += 1
            elif self.mutation == "id":
                payload["segments"][0]["id"] = 9
            elif self.mutation == "order":
                payload["segments"].reverse()
            else:
                payload["segments"][0]["source_text"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
        elif self.mutation == "srt_time":
            path = output_dir / "zh-CN.srt"
            cues = list(srt.parse(path.read_text(encoding="utf-8")))
            cues[0].start += timedelta(milliseconds=1)
            path.write_text(srt.compose(cues, reindex=False), encoding="utf-8")
        else:
            path = output_dir / "zh-CN.vtt"
            document = webvtt.read(str(path))
            document.captions[0].start = "00:00:00.001"
            document.save(str(path))
        return paths


@pytest.mark.parametrize(
    "mutation",
    ["speaker", "timestamp", "id", "order", "source_text", "srt_time", "vtt_time"],
)
def test_invalid_export_semantics_never_complete_job(tmp_path: Path, mutation: str) -> None:
    repository = JobRepository(tmp_path / mutation / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(
        tmp_path / mutation,
        repository,
        calls,
        exporter=CorruptingExporter(mutation),
    )
    job_id = _create_job(repository)
    first_stage = pipeline.resolve_first_required_stage(job_id)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=first_stage,
    )
    assert claimed is not None

    with pytest.raises(ValueError):
        pipeline.run_claimed(job_id=job_id, run_id=str(claimed["active_run_id"]))

    persisted = repository.get(job_id)
    assert persisted is not None
    assert persisted["status"] == JobStatus.EXPORTING.value
    assert CheckpointStage.TRANSLATED_TRANSCRIPT.value in str(persisted["checkpoint_pointer"])
    assert repository.list_artifacts(job_id) == []
    assert all(
        event["status"] != JobStatus.COMPLETED.value for event in repository.list_events(job_id)
    )
