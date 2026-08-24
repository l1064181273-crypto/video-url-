# Third-Party Notices

Local Video Transcriber is licensed under the MIT License. Dependencies and models
remain subject to their own licenses. Runtime artifacts are downloaded after
installation and are not included in the release ZIP.

## Python runtime dependencies

- `python:fastapi` - MIT
- `python:mlx-whisper` - MIT
- `python:numpy` - BSD-3-Clause
- `python:pydantic` - MIT
- `python:sherpa-onnx` - Apache-2.0
- `python:srt` - MIT
- `python:static-ffmpeg` - MIT
- `python:uvicorn` - BSD-3-Clause
- `python:webvtt-py` - MIT
- `python:yt-dlp` - Unlicense

All transitive Python distributions are fixed, including artifact hashes, by
`backend/uv.lock`. The source gate installs that lock in isolation before license
inspection.

## Chrome extension development dependencies

- `npm:@eslint/js` - MIT
- `npm:@playwright/test` - Apache-2.0
- `npm:@types/chrome` - MIT
- `npm:@types/node` - MIT
- `npm:eslint` - MIT
- `npm:eslint-config-prettier` - MIT
- `npm:globals` - MIT
- `npm:prettier` - MIT
- `npm:typescript` - Apache-2.0
- `npm:typescript-eslint` - MIT
- `npm:vite` - MIT
- `npm:vitest` - MIT

The complete transitive npm inventory and its declared license fields are fixed
by `extension/package-lock.json` and checked by the source verification gate.

## Tools and models

- `external:uv` - Apache-2.0 OR MIT
- `external:python` - PSF-2.0
- `external:ffmpeg` - GPL-3.0-or-later binary build
- `external:ollama` - MIT
- `external:asr-whisper-small-mlx-config` - MIT
- `external:asr-whisper-small-mlx-weights` - MIT
- `external:diarization-segmentation` - MIT
- `external:diarization-embedding` - Apache-2.0 (TitaNet-S / NeMo)
- `external:hy-mt2` - Apache-2.0
- `external:qwen2.5-1.5b` - Apache-2.0

Exact source URLs, versions, architecture, SHA-256 values, sizes, media types,
expected installed paths, and Ollama blob digests are in
`packaging/dependencies.json`. Runtime code must never replace those trusted
digests with values observed after download.

## License references

- MIT: `docs/LICENSES/MIT.txt`
- Apache-2.0: `docs/LICENSES/Apache-2.0.txt`
- GPL-3.0-or-later: `docs/LICENSES/GPL-3.0-or-later.txt`
- PSF-2.0: `docs/LICENSES/PSF-2.0.txt`
