import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import srt
import webvtt

from lvt.core.jobs import ErrorCode, JobStatus
from lvt.db.repository import AutomaticRequeueResult, JobRepository
from lvt.engines.base import (
    ASRResult,
    ASRSegment,
    DownloadedMedia,
    MediaInfo,
    SpeakerInterval,
    TranslationResult,
)
from lvt.exporters.files import export_transcript
from lvt.pipeline.checkpoints import CHECKPOINT_STAGE_ORDER, CheckpointStage
from lvt.pipeline.runner import Pipeline
from lvt.pipeline.segmenter import assign_speakers


class CountingDownloader:
    version = "fake-downloader-1"

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def download_media(self, url: str, work_dir: Path) -> DownloadedMedia:
        self.calls["downloaded_media"] += 1
        path = work_dir / "download.bin"
        path.write_bytes(b"downloaded-media")
        return DownloadedMedia(media_path=path, title="Checkpoint Sample")

    def normalize_audio(self, media: DownloadedMedia, work_dir: Path) -> MediaInfo:
        self.calls["normalized_audio"] += 1
        path = work_dir / "audio.normalized.wav"
        path.write_bytes(b"normalized-audio")
        return MediaInfo(audio_path=path, title=media.title, duration_ms=6_000)

    def download(self, url: str, work_dir: Path) -> MediaInfo:
        return self.normalize_audio(self.download_media(url, work_dir), work_dir)


class CountingASR:
    def __init__(self, calls: Counter[str], version: str = "fake-asr-1") -> None:
        self.calls = calls
        self.version = version

    def transcribe(self, audio_path: Path) -> ASRResult:
        return self.transcribe_with_model(audio_path, "legacy-default")

    def transcribe_with_model(self, audio_path: Path, model: str) -> ASRResult:
        self.calls["asr_result"] += 1
        self.calls[f"asr_model:{model}"] += 1
        return ASRResult(
            language="en",
            segments=[
                ASRSegment(0, 2_500, "Hello world."),
                ASRSegment(2_600, 5_900, "Local processing in 2026."),
            ],
        )


class CountingDiarizer:
    version = "fake-diarizer-1"

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def diarize(self, audio_path: Path) -> list[SpeakerInterval]:
        self.calls["diarization_result"] += 1
        return [
            SpeakerInterval(0, 2_500, "A"),
            SpeakerInterval(2_600, 5_900, "B"),
        ]


class CountingTranslator:
    version = "fake-translator-1"

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        self.calls["translated_transcript"] += 1
        return TranslationResult(
            texts={1: "你好，世界。", 2: "在 2026 年进行本地处理。"},
            engine_version=self.version,
            warnings=[],
        )


class CountingSegmenter:
    version = "fake-segmenter-1"

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

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
    version = "fake-exporter-1"

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def __call__(self, transcript: Any, export_root: Path) -> list[Path]:
        self.calls["export_manifest"] += 1
        return export_transcript(transcript, export_root)


def _build_pipeline(
    tmp_path: Path,
    repository: JobRepository,
    calls: Counter[str],
    *,
    asr_version: str = "fake-asr-1",
) -> Pipeline:
    return Pipeline(
        downloader=CountingDownloader(calls),
        asr=CountingASR(calls, asr_version),
        diarizer=CountingDiarizer(calls),
        translator=CountingTranslator(calls),
        segmenter=CountingSegmenter(calls),
        exporter=CountingExporter(calls),
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


def _claim_and_run(pipeline: Pipeline, repository: JobRepository, job_id: str) -> Any:
    first_stage = pipeline.resolve_first_required_stage(job_id)
    claimed = repository.claim_next(
        expected_job_id=job_id,
        first_required_stage=first_stage,
    )
    assert claimed is not None
    return pipeline.run_claimed(job_id=job_id, run_id=str(claimed["active_run_id"]))


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

    result = _claim_and_run(pipeline, repository, job_id)

    assert calls["diarization_result"] == 0
    assert calls["asr_model:persisted-asr"] == 1
    assert result.transcript.processing_options == {
        "asr_model": "persisted-asr",
        "diarization": False,
        "translate_to": "zh-CN",
    }
    assert [segment.speaker for segment in result.transcript.segments] == [
        "Speaker 1",
        "Speaker 1",
    ]
    assert len(result.artifacts) == 8
    assert repository.get(job_id)["status"] == JobStatus.COMPLETED.value  # type: ignore[index]
    assert len(repository.list_artifacts(job_id)) == 8
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
        if previous is None:
            assert manifest.input_checkpoint_fingerprints == {}
        else:
            assert manifest.input_checkpoint_fingerprints == {
                previous.stage.value: previous.manifest_fingerprint
            }
        previous = manifest


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


def test_engine_version_change_invalidates_asr_and_downstream(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "lvt.sqlite3")
    repository.initialize()
    calls: Counter[str] = Counter()
    pipeline = _build_pipeline(tmp_path, repository, calls)
    job_id = _create_job(repository)
    _prime_failed_completion(pipeline, repository, job_id)

    changed = _build_pipeline(tmp_path, repository, calls, asr_version="fake-asr-2")

    assert changed.resolve_checkpoints(job_id).first_required_stage is JobStatus.TRANSCRIBING


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
    pipeline.checkpoints.cleanup_unpublished_run(job_id, first_run)

    assert not stale_dir.exists()
    assert current_file.read_text(encoding="utf-8") == "current"
