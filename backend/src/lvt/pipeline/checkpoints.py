from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
import wave
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import srt  # type: ignore[import-untyped]
import webvtt  # type: ignore[import-untyped]

from lvt.core.jobs import JobStatus

CHECKPOINT_SCHEMA_VERSION = 1


class CheckpointStage(StrEnum):
    DOWNLOADED_MEDIA = "downloaded_media"
    NORMALIZED_AUDIO = "normalized_audio"
    ASR_RESULT = "asr_result"
    DIARIZATION_RESULT = "diarization_result"
    SOURCE_TRANSCRIPT = "source_transcript"
    TRANSLATED_TRANSCRIPT = "translated_transcript"
    EXPORT_MANIFEST = "export_manifest"


CHECKPOINT_STAGE_ORDER = (
    CheckpointStage.DOWNLOADED_MEDIA,
    CheckpointStage.NORMALIZED_AUDIO,
    CheckpointStage.ASR_RESULT,
    CheckpointStage.DIARIZATION_RESULT,
    CheckpointStage.SOURCE_TRANSCRIPT,
    CheckpointStage.TRANSLATED_TRANSCRIPT,
    CheckpointStage.EXPORT_MANIFEST,
)

STAGE_JOB_STATUS = {
    CheckpointStage.DOWNLOADED_MEDIA: JobStatus.DOWNLOADING,
    CheckpointStage.NORMALIZED_AUDIO: JobStatus.EXTRACTING,
    CheckpointStage.ASR_RESULT: JobStatus.TRANSCRIBING,
    CheckpointStage.DIARIZATION_RESULT: JobStatus.DIARIZING,
    CheckpointStage.SOURCE_TRANSCRIPT: JobStatus.SEGMENTING,
    CheckpointStage.TRANSLATED_TRANSCRIPT: JobStatus.TRANSLATING,
    CheckpointStage.EXPORT_MANIFEST: JobStatus.EXPORTING,
}


@dataclass(frozen=True)
class CheckpointOutput:
    relative_path: str
    kind: str
    byte_size: int
    sha256: str
    record_count: int


@dataclass(frozen=True)
class CheckpointManifest:
    schema_version: int
    job_id: str
    stage: CheckpointStage
    run_id: str
    created_at: str
    source_url_sha256: str
    job_options: dict[str, Any]
    options_fingerprint: str
    engine_names: dict[str, str]
    engine_versions: dict[str, str]
    engine_fingerprint: str
    input_checkpoint_fingerprints: dict[str, str]
    previous_manifest: str | None
    media_duration_ms: int | None
    transcript_schema_version: str | None
    outputs: tuple[CheckpointOutput, ...]
    manifest_fingerprint: str
    relative_manifest_path: str = field(compare=False, repr=False)


@dataclass(frozen=True)
class StageRequirement:
    options_fingerprint: str
    engine_names: dict[str, str]
    engine_versions: dict[str, str]
    engine_fingerprint: str


@dataclass(frozen=True)
class CheckpointResolution:
    manifests: dict[CheckpointStage, CheckpointManifest]
    first_required_stage: JobStatus


@dataclass(frozen=True)
class StageWorkspace:
    job_id: str
    run_id: str
    stage: CheckpointStage
    temporary_dir: Path
    final_dir: Path


@dataclass(frozen=True)
class PendingOutput:
    path: Path
    kind: str
    record_count: int


def stable_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CheckpointStore:
    def __init__(self, work_root: Path) -> None:
        # Canonicalize the trusted root once so macOS /var and /private/var aliases agree.
        self.work_root = work_root.resolve(strict=False)
        self.work_root.mkdir(parents=True, exist_ok=True)

    def run_root(self, job_id: str, run_id: str) -> Path:
        self._require_component(job_id, "job_id")
        self._require_component(run_id, "run_id")
        return self._contained_no_follow(
            self.work_root / job_id / "runs" / run_id,
            allow_missing=True,
        )

    def begin_stage(self, job_id: str, run_id: str, stage: CheckpointStage) -> StageWorkspace:
        run_root = self.run_root(job_id, run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        final_dir = self._contained_no_follow(run_root / stage.value, allow_missing=True)
        if final_dir.exists():
            marker = final_dir / ".published"
            if marker.is_symlink():
                raise ValueError("published marker cannot be a symlink")
            if marker.exists():
                raise FileExistsError(f"published checkpoint already exists: {final_dir}")
            self._assert_tree_has_no_symlinks(final_dir)
            shutil.rmtree(final_dir)
        temporary_dir = self._contained_no_follow(
            run_root / f".{stage.value}.tmp-{uuid.uuid4().hex}",
            allow_missing=True,
        )
        temporary_dir.mkdir()
        return StageWorkspace(job_id, run_id, stage, temporary_dir, final_dir)

    def write_json(self, workspace: StageWorkspace, name: str, value: object) -> Path:
        if Path(name).name != name:
            raise ValueError("checkpoint output name must be one path component")
        target = self._contained_no_follow(
            workspace.temporary_dir / name,
            allow_missing=True,
        )
        temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        self._fsync_directory(workspace.temporary_dir)
        return target

    def publish(
        self,
        workspace: StageWorkspace,
        *,
        source_url: str,
        job_options: dict[str, Any],
        requirement: StageRequirement,
        previous: CheckpointManifest | None,
        outputs: Sequence[PendingOutput],
        media_duration_ms: int | None = None,
        transcript_schema_version: str | None = None,
    ) -> CheckpointManifest:
        if not outputs:
            raise ValueError("checkpoint requires at least one output")
        for output in outputs:
            self._require_lexical_descendant(output.path, workspace.temporary_dir)
            path = self._contained_no_follow(output.path, allow_missing=False)
            if not path.is_file():
                raise ValueError("checkpoint output must be a regular non-symlink file")
            self._fsync_file(path)
        self._fsync_directory(workspace.temporary_dir)
        os.replace(workspace.temporary_dir, workspace.final_dir)
        self._fsync_directory(workspace.final_dir.parent)

        published_outputs = tuple(
            self._describe_output(
                workspace.final_dir / output.path.relative_to(workspace.temporary_dir),
                output.kind,
                output.record_count,
            )
            for output in outputs
        )
        previous_path = previous.relative_manifest_path if previous else None
        input_fingerprints = (
            {previous.stage.value: previous.manifest_fingerprint} if previous else {}
        )
        payload: dict[str, Any] = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "job_id": workspace.job_id,
            "stage": workspace.stage.value,
            "run_id": workspace.run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "source_url_sha256": hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
            "job_options": job_options,
            "options_fingerprint": requirement.options_fingerprint,
            "engine_names": requirement.engine_names,
            "engine_versions": requirement.engine_versions,
            "engine_fingerprint": requirement.engine_fingerprint,
            "input_checkpoint_fingerprints": input_fingerprints,
            "previous_manifest": previous_path,
            "media_duration_ms": media_duration_ms,
            "transcript_schema_version": transcript_schema_version,
            "outputs": [asdict(item) for item in published_outputs],
        }
        payload["manifest_fingerprint"] = stable_fingerprint(payload)
        manifest_path = workspace.final_dir / "manifest.json"
        temporary_manifest = manifest_path.with_name(f".manifest.tmp-{uuid.uuid4().hex}.json")
        with temporary_manifest.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_manifest, manifest_path)
        self._fsync_directory(workspace.final_dir)
        return self._manifest_from_payload(
            payload,
            manifest_path.relative_to(self.work_root).as_posix(),
        )

    def mark_published(self, manifest: CheckpointManifest) -> None:
        directory = self.manifest_path(manifest).parent
        self._contained_no_follow(directory, allow_missing=False)
        marker = directory / ".published"
        if marker.is_symlink():
            raise ValueError("published marker cannot be a symlink")
        temporary = directory / f".published.tmp-{uuid.uuid4().hex}"
        with temporary.open("wb") as handle:
            handle.write(manifest.manifest_fingerprint.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        self._fsync_directory(directory)

    def resolve(
        self,
        *,
        job_id: str,
        checkpoint_pointer: str | None,
        source_url: str,
        requirements: Mapping[CheckpointStage, StageRequirement],
    ) -> CheckpointResolution:
        candidate_paths = set(self.work_root.glob(f"{job_id}/runs/*/*/manifest.json"))
        candidate_paths = {
            path
            for path in candidate_paths
            if self._is_regular_no_follow(path.parent / ".published")
        }
        if checkpoint_pointer:
            with suppress(ValueError):
                candidate_paths.add(self._safe_relative_path(checkpoint_pointer))

        best: dict[CheckpointStage, CheckpointManifest] = {}
        for candidate in candidate_paths:
            valid = self._valid_prefix_from_candidate(
                candidate,
                job_id=job_id,
                source_url=source_url,
                requirements=requirements,
            )
            if len(valid) > len(best):
                best = valid
        first_index = len(best)
        if first_index >= len(CHECKPOINT_STAGE_ORDER):
            first_stage = JobStatus.EXPORTING
        else:
            first_stage = STAGE_JOB_STATUS[CHECKPOINT_STAGE_ORDER[first_index]]
        return CheckpointResolution(manifests=best, first_required_stage=first_stage)

    def manifest_path(self, manifest: CheckpointManifest) -> Path:
        return self._safe_relative_path(manifest.relative_manifest_path)

    def resolve_output_path(self, output: CheckpointOutput) -> Path:
        return self._safe_relative_path(output.relative_path)

    def cleanup_unpublished_run(self, job_id: str, run_id: str) -> None:
        run_root = self.run_root(job_id, run_id)
        if not run_root.exists():
            return
        self._assert_tree_has_no_symlinks(run_root)
        if any(run_root.glob("*/.published")):
            for candidate in run_root.iterdir():
                if candidate.is_dir() and (
                    candidate.name.startswith(".")
                    or not self._is_regular_no_follow(candidate / ".published")
                ):
                    self._assert_tree_has_no_symlinks(candidate)
                    shutil.rmtree(candidate)
            return
        shutil.rmtree(run_root)

    def discard_unpublished(self, manifest: CheckpointManifest) -> None:
        directory = self.manifest_path(manifest).parent
        self._assert_tree_has_no_symlinks(directory)
        if not (directory / ".published").exists():
            shutil.rmtree(directory)

    def _valid_prefix_from_candidate(
        self,
        candidate: Path,
        *,
        job_id: str,
        source_url: str,
        requirements: Mapping[CheckpointStage, StageRequirement],
    ) -> dict[CheckpointStage, CheckpointManifest]:
        chain: list[CheckpointManifest] = []
        current: Path | None = candidate
        visited: set[Path] = set()
        while current is not None and current not in visited:
            visited.add(current)
            manifest = self._load_manifest(current)
            if manifest is None:
                break
            chain.append(manifest)
            if manifest.previous_manifest:
                try:
                    current = self._safe_relative_path(manifest.previous_manifest)
                except ValueError:
                    break
            else:
                current = None
        chain.reverse()

        valid: dict[CheckpointStage, CheckpointManifest] = {}
        previous: CheckpointManifest | None = None
        for expected_stage, manifest in zip(CHECKPOINT_STAGE_ORDER, chain, strict=False):
            if manifest.stage is not expected_stage:
                break
            requirement = requirements[expected_stage]
            if not self._validate_manifest(
                manifest,
                job_id=job_id,
                source_url=source_url,
                requirement=requirement,
                previous=previous,
            ):
                break
            valid[expected_stage] = manifest
            previous = manifest
        return valid

    def _validate_manifest(
        self,
        manifest: CheckpointManifest,
        *,
        job_id: str,
        source_url: str,
        requirement: StageRequirement,
        previous: CheckpointManifest | None,
    ) -> bool:
        if manifest.schema_version != CHECKPOINT_SCHEMA_VERSION or manifest.job_id != job_id:
            return False
        if manifest.source_url_sha256 != hashlib.sha256(source_url.encode("utf-8")).hexdigest():
            return False
        if (
            manifest.options_fingerprint != requirement.options_fingerprint
            or manifest.engine_fingerprint != requirement.engine_fingerprint
            or manifest.engine_names != requirement.engine_names
            or manifest.engine_versions != requirement.engine_versions
        ):
            return False
        expected_inputs = {previous.stage.value: previous.manifest_fingerprint} if previous else {}
        if manifest.input_checkpoint_fingerprints != expected_inputs:
            return False
        if manifest.previous_manifest != (previous.relative_manifest_path if previous else None):
            return False
        if manifest.stage is CheckpointStage.DOWNLOADED_MEDIA:
            if (
                manifest.media_duration_ms is not None
                or manifest.transcript_schema_version is not None
            ):
                return False
        else:
            if manifest.media_duration_ms is None or manifest.media_duration_ms <= 0:
                return False
            if (
                previous is not None
                and previous.media_duration_ms is not None
                and manifest.media_duration_ms != previous.media_duration_ms
            ):
                return False
        if manifest.stage in {
            CheckpointStage.TRANSLATED_TRANSCRIPT,
            CheckpointStage.EXPORT_MANIFEST,
        }:
            if not manifest.transcript_schema_version:
                return False
        elif manifest.transcript_schema_version is not None:
            return False
        if (
            manifest.stage is CheckpointStage.EXPORT_MANIFEST
            and previous is not None
            and manifest.transcript_schema_version != previous.transcript_schema_version
        ):
            return False
        payload = self._payload_without_runtime_path(manifest)
        fingerprint = payload.pop("manifest_fingerprint")
        if stable_fingerprint(payload) != fingerprint:
            return False
        for output in manifest.outputs:
            try:
                path = self.resolve_output_path(output)
            except ValueError:
                return False
            if not path.is_file() or path.stat().st_size != output.byte_size:
                return False
            if self._sha256(path) != output.sha256:
                return False
            if self._record_count(path, output.kind) != output.record_count:
                return False
            if output.kind == "normalized_audio":
                try:
                    if self._wav_duration_ms(path) != manifest.media_duration_ms:
                        return False
                except (OSError, ValueError, wave.Error, ZeroDivisionError):
                    return False
        return True

    def _load_manifest(self, path: Path) -> CheckpointManifest | None:
        try:
            safe_path = self._contained_no_follow(path, allow_missing=False)
            marker = safe_path.parent / ".published"
            if marker.is_symlink() or not safe_path.is_file():
                return None
            payload = json.loads(safe_path.read_text(encoding="utf-8"))
            return self._manifest_from_payload(
                payload,
                safe_path.relative_to(self.work_root).as_posix(),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _manifest_from_payload(
        payload: dict[str, Any], relative_manifest_path: str
    ) -> CheckpointManifest:
        return CheckpointManifest(
            schema_version=int(payload["schema_version"]),
            job_id=str(payload["job_id"]),
            stage=CheckpointStage(payload["stage"]),
            run_id=str(payload["run_id"]),
            created_at=str(payload["created_at"]),
            source_url_sha256=str(payload["source_url_sha256"]),
            job_options=dict(payload["job_options"]),
            options_fingerprint=str(payload["options_fingerprint"]),
            engine_names=dict(payload["engine_names"]),
            engine_versions=dict(payload["engine_versions"]),
            engine_fingerprint=str(payload["engine_fingerprint"]),
            input_checkpoint_fingerprints=dict(payload["input_checkpoint_fingerprints"]),
            previous_manifest=(
                str(payload["previous_manifest"]) if payload["previous_manifest"] else None
            ),
            media_duration_ms=(
                int(payload["media_duration_ms"])
                if payload.get("media_duration_ms") is not None
                else None
            ),
            transcript_schema_version=(
                str(payload["transcript_schema_version"])
                if payload.get("transcript_schema_version")
                else None
            ),
            outputs=tuple(CheckpointOutput(**item) for item in payload["outputs"]),
            manifest_fingerprint=str(payload["manifest_fingerprint"]),
            relative_manifest_path=relative_manifest_path,
        )

    @staticmethod
    def _payload_without_runtime_path(manifest: CheckpointManifest) -> dict[str, Any]:
        payload = asdict(manifest)
        payload.pop("relative_manifest_path")
        payload["stage"] = manifest.stage.value
        payload["outputs"] = [asdict(item) for item in manifest.outputs]
        return payload

    def _describe_output(self, path: Path, kind: str, record_count: int) -> CheckpointOutput:
        return CheckpointOutput(
            relative_path=path.relative_to(self.work_root).as_posix(),
            kind=kind,
            byte_size=path.stat().st_size,
            sha256=self._sha256(path),
            record_count=record_count,
        )

    def _safe_relative_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("checkpoint path must be relative and contained")
        return self._contained_no_follow(
            self.work_root / candidate,
            allow_missing=False,
        )

    def _contained_no_follow(self, path: Path, *, allow_missing: bool) -> Path:
        candidate = path.absolute()
        try:
            relative = candidate.relative_to(self.work_root)
        except ValueError as exc:
            raise ValueError("checkpoint path escapes work root") from exc
        current = self.work_root
        for part in relative.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if allow_missing:
                    continue
                raise ValueError(f"checkpoint path does not exist: {current}") from None
            if stat.S_ISLNK(mode):
                raise ValueError(f"checkpoint path contains symlink: {current}")
        return candidate

    @staticmethod
    def _require_lexical_descendant(path: Path, parent: Path) -> None:
        try:
            path.absolute().relative_to(parent.absolute())
        except ValueError as exc:
            raise ValueError("checkpoint output escapes stage directory") from exc

    @staticmethod
    def _require_component(value: str, field_name: str) -> None:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError(f"{field_name} must be one safe path component")

    def _is_regular_no_follow(self, path: Path) -> bool:
        try:
            safe = self._contained_no_follow(path, allow_missing=False)
            return stat.S_ISREG(safe.lstat().st_mode)
        except (OSError, ValueError):
            return False

    def _assert_tree_has_no_symlinks(self, root: Path) -> None:
        safe_root = self._contained_no_follow(root, allow_missing=False)
        for current, directories, files in os.walk(safe_root, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                child = current_path / name
                if stat.S_ISLNK(child.lstat().st_mode):
                    raise ValueError(f"checkpoint tree contains symlink: {child}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _record_count(path: Path, kind: str) -> int:
        if kind in {"downloaded_media", "normalized_audio"}:
            return 1
        if kind.endswith(".srt"):
            return len(list(srt.parse(path.read_text(encoding="utf-8"))))
        if kind.endswith(".vtt"):
            return len(webvtt.read(str(path)).captions)
        if kind.endswith(".txt"):
            return len([line for line in path.read_text(encoding="utf-8").splitlines() if line])
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "segments" in payload:
            return len(payload["segments"])
        if "intervals" in payload:
            return len(payload["intervals"])
        return 1

    @staticmethod
    def _wav_duration_ms(path: Path) -> int:
        with wave.open(str(path), "rb") as audio:
            return round(audio.getnframes() / audio.getframerate() * 1000)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
