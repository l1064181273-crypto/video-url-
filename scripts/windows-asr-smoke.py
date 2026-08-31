#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

AUDIO_URL = (
    "https://raw.githubusercontent.com/openai/whisper/"
    "8cf36f3508c9acd341a45eb2364239a3d81458b9/tests/jfk.flac"
)
AUDIO_SIZE = 1_152_693
AUDIO_SHA256 = "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715"


def _download_audio(destination: Path) -> None:
    request = urllib.request.Request(
        AUDIO_URL,
        headers={"User-Agent": "LocalVideoTranscriber-Windows-Validation/1"},
    )
    digest = hashlib.sha256()
    written = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.username is not None or final.password is not None:
            raise RuntimeError("ASR fixture redirect violated HTTPS policy")
        with destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > AUDIO_SIZE:
                    raise RuntimeError("ASR fixture exceeded pinned size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    if written != AUDIO_SIZE or digest.hexdigest() != AUDIO_SHA256:
        raise RuntimeError("ASR fixture integrity check failed")


def run_smoke(model_directory: Path) -> dict[str, object]:
    required = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
    if (
        not model_directory.is_absolute()
        or model_directory.is_symlink()
        or not model_directory.is_dir()
        or any(
            path.is_symlink() or not path.is_file() or path.stat().st_size == 0
            for path in (model_directory / name for name in required)
        )
    ):
        raise RuntimeError("installed faster-whisper model is unavailable")

    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory(prefix="lvt-asr-smoke-") as temporary:
        audio = Path(temporary) / "jfk.flac"
        _download_audio(audio)
        model = WhisperModel(
            os.fspath(model_directory),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
        segments, info = model.transcribe(
            os.fspath(audio),
            beam_size=1,
            vad_filter=False,
            word_timestamps=False,
        )
        texts = [segment.text.strip() for segment in segments if segment.text.strip()]
    language = str(getattr(info, "language", "") or "").strip().lower()
    if not language or not texts:
        raise RuntimeError("faster-whisper CPU inference returned no transcript")
    transcript = " ".join(texts).encode("utf-8")
    return {
        "schema_version": 1,
        "status": "passed",
        "backend": "faster-whisper",
        "device": "cpu",
        "compute_type": "int8",
        "local_files_only": True,
        "language": language,
        "segment_count": len(texts),
        "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pinned Windows CPU ASR validation")
    parser.add_argument("--model-directory", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        report = run_smoke(arguments.model_directory)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": " ".join(str(exc).split())[:512],
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
