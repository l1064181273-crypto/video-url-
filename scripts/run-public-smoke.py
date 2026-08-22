#!/usr/bin/env python3
"""Run one public URL through the production real-engine pipeline."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from lvt.pipeline.factory import RealPipelineConfig, create_real_pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--segmentation-model",
        type=Path,
        default=Path("vendor/diarization-models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx"),
    )
    parser.add_argument(
        "--embedding-model",
        type=Path,
        default=Path("vendor/diarization-models/embed.onnx"),
    )
    parser.add_argument("--asr-model", default="mlx-community/whisper-tiny")
    args = parser.parse_args()

    pipeline = create_real_pipeline(
        RealPipelineConfig(
            work_root=args.output_root / "work",
            export_root=args.output_root / "exports",
            segmentation_model=args.segmentation_model.resolve(),
            embedding_model=args.embedding_model.resolve(),
            asr_model=args.asr_model,
        )
    )
    result = pipeline.run(job_id=f"public-{uuid.uuid4().hex[:12]}", url=args.url)
    report = {
        "url": args.url,
        "title": result.transcript.title,
        "duration_ms": result.transcript.duration_ms,
        "language": result.transcript.detected_language,
        "segment_count": len(result.transcript.segments),
        "speakers": sorted({item.speaker for item in result.transcript.segments}),
        "engine_versions": result.transcript.engine_versions,
        "warnings": result.transcript.warnings,
        "output_dir": str(result.artifacts[0].parent.resolve()),
        "artifacts": [str(path.resolve()) for path in result.artifacts],
    }
    report_path = args.output_root / "public-smoke-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
