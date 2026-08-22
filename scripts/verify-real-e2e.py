#!/usr/bin/env python3
"""Independently verify Phase 1 real end-to-end artifacts on disk."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import srt
import webvtt

IMMUTABLE_FIELDS = ("id", "start_ms", "end_ms", "speaker", "source_language", "source_text")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    output_dirs: set[str] = set()

    for sample in report["samples"]:
        output_dir = Path(sample["output_dir"])
        output_dirs.add(str(output_dir))
        expected_files = {
            "source.txt",
            "source.srt",
            "source.vtt",
            "source.json",
            "zh-CN.txt",
            "zh-CN.srt",
            "zh-CN.vtt",
            "zh-CN.json",
        }
        assert {path.name for path in output_dir.iterdir()} == expected_files
        assert all((output_dir / name).stat().st_size > 0 for name in expected_files)

        source = json.loads((output_dir / "source.json").read_text(encoding="utf-8"))
        translated = json.loads((output_dir / "zh-CN.json").read_text(encoding="utf-8"))
        assert len(source["segments"]) == len(translated["segments"]) > 0
        for source_segment, translated_segment in zip(
            source["segments"], translated["segments"], strict=True
        ):
            assert all(
                source_segment[field] == translated_segment[field] for field in IMMUTABLE_FIELDS
            )
            assert source_segment["translated_text"] == ""
            assert translated_segment["translated_text"].strip()

        source_srt = list(srt.parse((output_dir / "source.srt").read_text(encoding="utf-8")))
        translated_srt = list(srt.parse((output_dir / "zh-CN.srt").read_text(encoding="utf-8")))
        assert len(source_srt) == len(translated_srt) == len(source["segments"])
        assert [(item.index, item.start, item.end) for item in source_srt] == [
            (item.index, item.start, item.end) for item in translated_srt
        ]
        assert [item.content.split(":", 1)[0] for item in source_srt] == [
            item.content.split(":", 1)[0] for item in translated_srt
        ]

        source_vtt = webvtt.read(str(output_dir / "source.vtt"))
        translated_vtt = webvtt.read(str(output_dir / "zh-CN.vtt"))
        assert len(source_vtt.captions) == len(translated_vtt.captions) == len(source["segments"])
        assert [(item.start, item.end) for item in source_vtt.captions] == [
            (item.start, item.end) for item in translated_vtt.captions
        ]

    assert len(output_dirs) == len(report["samples"])
    same_title_samples = [
        sample
        for sample in report["samples"]
        if str(sample.get("label", "")).startswith("same_title")
    ]
    same_title_dirs = {sample["output_dir"] for sample in same_title_samples}
    if same_title_samples:
        assert len(same_title_dirs) == len(same_title_samples) == 2
    print(f"verified {len(report['samples'])} samples and {len(report['samples']) * 8} artifacts")


if __name__ == "__main__":
    main()
