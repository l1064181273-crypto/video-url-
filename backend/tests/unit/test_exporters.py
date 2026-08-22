import json
from pathlib import Path

import srt
import webvtt

from lvt.core.models import Segment, Transcript
from lvt.exporters.files import export_transcript


def make_transcript() -> Transcript:
    return Transcript(
        job_id="job-1",
        source_url="https://example.test/video",
        title="中文 标题 / test",
        duration_ms=5000,
        detected_language="en",
        engine_versions={"asr": "fake-1", "translation": "fake-1"},
        segments=[
            Segment(
                id=1,
                start_ms=125,
                end_ms=2100,
                speaker="Speaker 1",
                source_language="en",
                source_text="Hello.",
                translated_text="你好。",
            ),
            Segment(
                id=2,
                start_ms=2200,
                end_ms=4999,
                speaker="Speaker 2",
                source_language="en",
                source_text="Private local processing.",
                translated_text="私密的本地处理。",
            ),
        ],
    )


def test_export_transcript_generates_eight_parseable_files(tmp_path: Path) -> None:
    transcript = make_transcript()
    artifacts = export_transcript(transcript, tmp_path)

    assert len(artifacts) == 8
    assert {path.name for path in artifacts} == {
        "source.txt",
        "source.srt",
        "source.vtt",
        "source.json",
        "zh-CN.txt",
        "zh-CN.srt",
        "zh-CN.vtt",
        "zh-CN.json",
    }
    assert all(path.is_file() and path.stat().st_size > 0 for path in artifacts)

    output_dir = artifacts[0].parent
    source_srt = list(srt.parse((output_dir / "source.srt").read_text(encoding="utf-8")))
    translated_srt = list(srt.parse((output_dir / "zh-CN.srt").read_text(encoding="utf-8")))
    assert [(cue.start, cue.end) for cue in source_srt] == [
        (cue.start, cue.end) for cue in translated_srt
    ]
    assert [cue.content.split(":", 1)[0] for cue in source_srt] == [
        cue.content.split(":", 1)[0] for cue in translated_srt
    ]
    assert len(webvtt.read(str(output_dir / "source.vtt")).captions) == 2
    assert len(webvtt.read(str(output_dir / "zh-CN.vtt")).captions) == 2

    source_json = json.loads((output_dir / "source.json").read_text(encoding="utf-8"))
    translated_json = json.loads((output_dir / "zh-CN.json").read_text(encoding="utf-8"))
    assert [item["source_text"] for item in source_json["segments"]] == [
        "Hello.",
        "Private local processing.",
    ]
    assert [item["translated_text"] for item in source_json["segments"]] == ["", ""]
    assert [item["source_text"] for item in translated_json["segments"]] == [
        "Hello.",
        "Private local processing.",
    ]
    assert [item["translated_text"] for item in translated_json["segments"]] == [
        "你好。",
        "私密的本地处理。",
    ]


def test_same_title_jobs_use_distinct_export_directories(tmp_path: Path) -> None:
    first = make_transcript().model_copy(update={"job_id": "job-111111111111"})
    second = make_transcript().model_copy(update={"job_id": "job-222222222222"})

    first_artifacts = export_transcript(first, tmp_path)
    second_artifacts = export_transcript(second, tmp_path)

    assert first_artifacts[0].parent != second_artifacts[0].parent
    assert first_artifacts[0].parent.name.endswith("--job-11111111")
    assert second_artifacts[0].parent.name.endswith("--job-22222222")
    assert len(list(first_artifacts[0].parent.iterdir())) == 8
    assert len(list(second_artifacts[0].parent.iterdir())) == 8
