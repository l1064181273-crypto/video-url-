# Third-Party Notices

Local Video Transcriber is licensed under the MIT License with the owner-approved
copyright line `Copyright (c) 2026 Leoy`. Dependencies and models remain subject
to their own licenses. Runtime artifacts are downloaded after installation and
are not included in the release ZIP.

## Complete Python inventory

`docs/LICENSES/python-runtime.json` is the normative package-level inventory for
the macOS arm64 Python 3.11 environment. It contains all 77 frozen distributions:

- 67 runtime distributions selected by `uv export --frozen --no-dev`;
- 10 development-only distributions added by the frozen `dev` extra.

Each entry records normalized package name, exact version, license, immutable
versioned PyPI source, license metadata source, scope, and an `uv.lock` trace.
This includes the known non-blocking `static-ffmpeg` → `twine` runtime closure.

## Complete Chrome extension inventory

`docs/LICENSES/npm-all.json` is the normative package-level inventory for all
166 package paths in `extension/package-lock.json`. All 166 development/build-only
packages are excluded from the extension bundle. Each entry
records package, package-lock path, exact version, license, resolved tarball,
integrity, scope, and lock trace.

The source gate compares both inventories as exact sets against their lockfiles.
A missing package, changed version, unknown license, changed source, changed
integrity, or stale lock fingerprint is fatal.

## Tools and models

- `external:uv` - Apache-2.0 OR MIT
- `external:python` - PSF-2.0
- `external:ffmpeg` - GPL-3.0-or-later binary build
- `external:ollama` - MIT; fixed evidence `docs/LICENSES/Ollama-MIT.txt`,
  preserving `Copyright (c) Ollama`
- `external:asr-whisper-small-mlx-config` and
  `external:asr-whisper-small-mlx-weights` - MIT; fixed evidence
  `docs/LICENSES/Whisper-MIT.txt`, preserving `Copyright (c) 2022 OpenAI`
- `external:diarization-segmentation` - MIT; fixed evidence
  `docs/LICENSES/Pyannote-Segmentation-MIT.txt`, preserving
  `Copyright (c) 2023 CNRS`
- `external:diarization-embedding` - Apache-2.0 (TitaNet-S / NeMo)
- `external:hy-mt2` - Apache-2.0; fixed base-model evidence
  `docs/LICENSES/Hy-MT2-GGUF-README.md` maps the pinned GGUF revision to
  `tencent/Hy-MT2-1.8B`, whose fixed license is
  `docs/LICENSES/Hy-MT2-Apache-2.0.txt`
- `external:qwen2.5-1.5b` - Apache-2.0; content-addressed license blob is
  preserved verbatim as `docs/LICENSES/Qwen2.5-Apache-2.0.txt`

Exact source URLs, versions, architecture, SHA-256 values, sizes, media types,
expected installed paths, and Ollama blob digests are in
`packaging/dependencies.json`. Runtime code must never replace those trusted
digests with values observed after download.

## License references

- MIT: `docs/LICENSES/MIT.txt`
- Apache-2.0: `docs/LICENSES/Apache-2.0.txt`
- GPL-3.0-or-later: `docs/LICENSES/GPL-3.0-or-later.txt`
- PSF-2.0: `docs/LICENSES/PSF-2.0.txt`
- Ollama MIT: `docs/LICENSES/Ollama-MIT.txt`
- Whisper MIT: `docs/LICENSES/Whisper-MIT.txt`
- pyannote segmentation MIT: `docs/LICENSES/Pyannote-Segmentation-MIT.txt`
- Hy-MT2 base-model Apache-2.0: `docs/LICENSES/Hy-MT2-Apache-2.0.txt`
- Qwen2.5 Apache-2.0: `docs/LICENSES/Qwen2.5-Apache-2.0.txt`
