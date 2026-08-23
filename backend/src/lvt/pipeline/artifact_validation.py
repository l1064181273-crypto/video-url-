from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import srt  # type: ignore[import-untyped]
import webvtt  # type: ignore[import-untyped]

from lvt.core.models import IMMUTABLE_TRANSLATION_FIELDS, Segment, Transcript
from lvt.db.repository import REQUIRED_ARTIFACT_KINDS
from lvt.exporters.files import format_txt_timestamp


def validate_export_artifacts(transcript: Transcript, paths: list[Path]) -> None:
    by_name = {path.name: path for path in paths}
    if len(paths) != len(REQUIRED_ARTIFACT_KINDS) or set(by_name) != REQUIRED_ARTIFACT_KINDS:
        raise ValueError("export must contain the exact eight artifact files")
    if len({path.parent for path in paths}) != 1:
        raise ValueError("all artifacts must share one export directory")

    source_payload = json.loads(by_name["source.json"].read_text(encoding="utf-8"))
    translated_payload = json.loads(by_name["zh-CN.json"].read_text(encoding="utf-8"))
    source = Transcript.model_validate(source_payload)
    translated = Transcript.model_validate(translated_payload)
    if len(source.segments) != len(transcript.segments) or len(translated.segments) != len(
        transcript.segments
    ):
        raise ValueError("JSON segment count mismatch")

    immutable = set(IMMUTABLE_TRANSLATION_FIELDS) | {"metadata"}
    for expected, source_segment, translated_segment in zip(
        transcript.segments,
        source.segments,
        translated.segments,
        strict=True,
    ):
        expected_values = expected.model_dump(include=immutable)
        if source_segment.model_dump(include=immutable) != expected_values:
            raise ValueError("source JSON changed immutable segment fields")
        if translated_segment.model_dump(include=immutable) != expected_values:
            raise ValueError("translated JSON changed immutable segment fields")
        if source_segment.translated_text != "":
            raise ValueError("source JSON translated_text must be empty")
        if translated_segment.translated_text != expected.translated_text:
            raise ValueError("translated JSON changed translated_text")

    _validate_srt(by_name["source.srt"], transcript.segments, translated=False)
    _validate_srt(by_name["zh-CN.srt"], transcript.segments, translated=True)
    _validate_vtt(by_name["source.vtt"], transcript.segments, translated=False)
    _validate_vtt(by_name["zh-CN.vtt"], transcript.segments, translated=True)
    _validate_txt(by_name["source.txt"], transcript.segments, translated=False)
    _validate_txt(by_name["zh-CN.txt"], transcript.segments, translated=True)


def _expected_text(segment: Segment, translated: bool) -> str:
    text = segment.translated_text if translated else segment.source_text
    return f"{segment.speaker}: {text.strip()}"


def _validate_srt(path: Path, segments: list[Segment], *, translated: bool) -> None:
    cues = list(srt.parse(path.read_text(encoding="utf-8")))
    if len(cues) != len(segments):
        raise ValueError("SRT cue count mismatch")
    for cue, segment in zip(cues, segments, strict=True):
        if (
            cue.index != segment.id
            or _milliseconds(cue.start) != segment.start_ms
            or _milliseconds(cue.end) != segment.end_ms
            or cue.content != _expected_text(segment, translated)
        ):
            raise ValueError("SRT cue semantics mismatch")


def _validate_vtt(path: Path, segments: list[Segment], *, translated: bool) -> None:
    cues = webvtt.read(str(path)).captions
    if len(cues) != len(segments):
        raise ValueError("VTT cue count mismatch")
    for cue, segment in zip(cues, segments, strict=True):
        if (
            _parse_vtt_milliseconds(cue.start) != segment.start_ms
            or _parse_vtt_milliseconds(cue.end) != segment.end_ms
            or cue.text != _expected_text(segment, translated)
        ):
            raise ValueError("VTT cue semantics mismatch")


def _validate_txt(path: Path, segments: list[Segment], *, translated: bool) -> None:
    expected = [
        (
            f"[{format_txt_timestamp(segment.start_ms)} --> "
            f"{format_txt_timestamp(segment.end_ms)}] "
            f"{_expected_text(segment, translated)}"
        )
        for segment in segments
    ]
    if path.read_text(encoding="utf-8").splitlines() != expected:
        raise ValueError("TXT segment semantics mismatch")


def _milliseconds(value: timedelta) -> int:
    return round(value.total_seconds() * 1000)


def _parse_vtt_milliseconds(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    whole_seconds, milliseconds = seconds.split(".")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(whole_seconds) * 1_000
        + int(milliseconds)
    )
