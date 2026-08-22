#!/usr/bin/env python3
"""Run the Phase 1 pipeline with only real media/model engines."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from urllib.parse import quote

from lvt.pipeline.factory import RealPipelineConfig, create_real_pipeline

FIXTURES = [
    ("english", "English Single.mp4"),
    ("russian", "Русский single.mp4"),
    ("two_speakers", "中文 双人 video.mp4"),
    ("same_title_a", "same-a/Same Title.mp4"),
    ("same_title_b", "same-b/Same Title.mp4"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
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
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    pipeline = create_real_pipeline(
        RealPipelineConfig(
            work_root=args.output_root / "work",
            export_root=args.output_root / "exports",
            segmentation_model=args.segmentation_model.resolve(),
            embedding_model=args.embedding_model.resolve(),
            asr_model=args.asr_model,
            ollama_url=args.ollama_url,
        )
    )

    samples: list[dict[str, object]] = []
    for label, relative_path in FIXTURES:
        encoded_path = "/".join(quote(part) for part in relative_path.split("/"))
        url = f"{args.base_url.rstrip('/')}/{encoded_path}"
        job_id = f"{label}-{uuid.uuid4().hex[:12]}"
        result = pipeline.run(job_id=job_id, url=url)
        samples.append(
            {
                "label": label,
                "job_id": job_id,
                "url": url,
                "title": result.transcript.title,
                "duration_ms": result.transcript.duration_ms,
                "language": result.transcript.detected_language,
                "segment_count": len(result.transcript.segments),
                "speakers": sorted({segment.speaker for segment in result.transcript.segments}),
                "engine_versions": result.transcript.engine_versions,
                "warnings": result.transcript.warnings,
                "output_dir": str(result.artifacts[0].parent.resolve()),
                "artifacts": [str(path.resolve()) for path in result.artifacts],
            }
        )
    report = {"schema_version": 1, "samples": samples}
    report_path = args.output_root / "real-e2e-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
