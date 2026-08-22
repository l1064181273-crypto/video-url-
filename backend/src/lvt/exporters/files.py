from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import srt  # type: ignore[import-untyped]
import webvtt  # type: ignore[import-untyped]

from lvt.core.models import Segment, Transcript
from lvt.security.paths import ensure_within_root, safe_filename


def format_txt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def format_vtt_timestamp(milliseconds: int) -> str:
    return format_txt_timestamp(milliseconds)


def _text(segment: Segment, translated: bool) -> str:
    text = segment.translated_text if translated else segment.source_text
    if not text.strip():
        raise ValueError(f"segment {segment.id} has empty export text")
    return text.strip()


def _subtitle_text(segment: Segment, translated: bool) -> str:
    return f"{segment.speaker}: {_text(segment, translated)}"


def _write_txt(path: Path, segments: list[Segment], translated: bool) -> None:
    lines = [
        (
            f"[{format_txt_timestamp(segment.start_ms)} --> "
            f"{format_txt_timestamp(segment.end_ms)}] "
            f"{_subtitle_text(segment, translated)}"
        )
        for segment in segments
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_srt(path: Path, segments: list[Segment], translated: bool) -> None:
    subtitles = [
        srt.Subtitle(
            index=segment.id,
            start=timedelta(milliseconds=segment.start_ms),
            end=timedelta(milliseconds=segment.end_ms),
            content=_subtitle_text(segment, translated),
        )
        for segment in segments
    ]
    path.write_text(srt.compose(subtitles, reindex=False), encoding="utf-8")
    parsed = list(srt.parse(path.read_text(encoding="utf-8")))
    if len(parsed) != len(segments):
        raise ValueError("SRT readback cue count mismatch")


def _write_vtt(path: Path, segments: list[Segment], translated: bool) -> None:
    output = webvtt.WebVTT()
    for segment in segments:
        output.captions.append(
            webvtt.Caption(
                format_vtt_timestamp(segment.start_ms),
                format_vtt_timestamp(segment.end_ms),
                _subtitle_text(segment, translated),
            )
        )
    output.save(str(path))
    if len(webvtt.read(str(path)).captions) != len(segments):
        raise ValueError("VTT readback cue count mismatch")


def _write_json(path: Path, transcript: Transcript) -> None:
    path.write_text(
        json.dumps(transcript.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def export_transcript(transcript: Transcript, export_root: Path) -> list[Path]:
    safe_title = safe_filename(transcript.title, fallback="untitled")
    job_suffix = safe_filename(transcript.job_id, fallback="job")[:12]
    target = ensure_within_root(
        export_root / f"{safe_title}--{job_suffix}",
        export_root,
    )
    target.mkdir(parents=True, exist_ok=True)

    source_json = transcript.model_copy(
        deep=True,
        update={
            "segments": [
                segment.model_copy(update={"translated_text": ""})
                for segment in transcript.segments
            ]
        },
    )
    artifacts: list[Path] = []
    for prefix, translated, json_model in (
        ("source", False, source_json),
        ("zh-CN", True, transcript),
    ):
        txt_path = target / f"{prefix}.txt"
        srt_path = target / f"{prefix}.srt"
        vtt_path = target / f"{prefix}.vtt"
        json_path = target / f"{prefix}.json"
        _write_txt(txt_path, transcript.segments, translated)
        _write_srt(srt_path, transcript.segments, translated)
        _write_vtt(vtt_path, transcript.segments, translated)
        _write_json(json_path, json_model)
        artifacts.extend((txt_path, srt_path, vtt_path, json_path))
    return artifacts
