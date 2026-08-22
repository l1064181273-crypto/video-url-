# TEST_REPORT — Local Video Transcriber v0.1

## Environment

- Date: 2026-08-22
- Machine: Apple M5, arm64, 16 GB RAM
- macOS: 26.5.2 (25F84)
- Python: 3.11.15
- Ollama: 0.32.15
- mlx-whisper: 0.4.3
- sherpa-onnx: 1.13.6
- yt-dlp: 2026.8.19
- static-ffmpeg: 3.0 (FFmpeg darwin-arm64)
- ASR smoke model: `mlx-community/whisper-tiny`
- Translation primary: `hy-mt2:1.8b-q4km-fixed`
- Translation fallback: `qwen2.5:1.5b`

## Commands And Results

### Static and deterministic tests

```bash
cd backend
../.venv-smoke/bin/python -m pytest
# 37 passed, 1 third-party StarletteDeprecationWarning

../.venv-smoke/bin/python -m ruff check src tests ../scripts
# All checks passed

../.venv-smoke/bin/python -m ruff format --check src tests ../scripts
# 40 files already formatted

../.venv-smoke/bin/python -m mypy src/lvt
# Success: no issues found in 26 source files
```

### Reproducible media fixtures

```bash
FFMPEG="$PWD/.venv-smoke/lib/python3.11/site-packages/static_ffmpeg/bin/darwin_arm64/ffmpeg"
bash scripts/make-test-assets.sh "$FFMPEG"
```

Generated ignored test media under `test-assets/generated/`: English single speaker,
Russian single speaker, alternating two-speaker video, Chinese/space filename, silence,
tone, and two same-title videos in different directories.

### Local HTTP real-engine pipeline

```bash
python -m http.server 8891 --bind 127.0.0.1 --directory test-assets/generated

.venv-smoke/bin/python scripts/run-real-e2e.py \
  --base-url http://127.0.0.1:8891 \
  --output-root "$PWD/.tmp-real-e2e-v2"

.venv-smoke/bin/python scripts/verify-real-e2e.py \
  .tmp-real-e2e-v2/real-e2e-report.json
# verified 5 samples and 40 artifacts
```

All tasks used the real `YtDlpFFmpegDownloader`, `MLXWhisperASREngine`,
`SherpaOnnxDiarizationEngine`, and Ollama translation engines. No Fake Engine was used
by these commands.

| Sample | Duration | Language | Segments | Speakers | Translation | Output directory |
|---|---:|---|---:|---:|---|---|
| English single | 14.016s | en | 5 | 1 | Hy-MT2 | `.tmp-real-e2e-v2/exports/English Single--english-e31b` |
| Russian single | 11.328s | ru | 4 | 1 | qwen fallback | `.tmp-real-e2e-v2/exports/Русский single--russian-ca95` |
| Chinese filename / two speakers | 16.256s | en | 7 | 2 | Hy-MT2 | `.tmp-real-e2e-v2/exports/中文 双人 video--two_speakers` |
| Same Title A | 14.016s | en | 5 | 1 | Hy-MT2 | `.tmp-real-e2e-v2/exports/Same Title--same_title_a` |
| Same Title B | 14.016s | en | 5 | 1 | Hy-MT2 | `.tmp-real-e2e-v2/exports/Same Title--same_title_b` |

The two `Same Title` jobs have separate directories and did not overwrite each other.

### Translation fallback observed

The first real Russian export exposed a semantic validation gap: Hy-MT2 returned a
JSON string containing an explanatory note, newline, and braces. A regression test was
added before the final run. On the final run:

1. Hy-MT2 returned semantically invalid wrapped text three times.
2. Retries stopped at the configured limit.
3. qwen2.5:1.5b produced valid translations.
4. `Transcript.warnings` records the fallback and primary error.
5. `engine_versions.translation` records `ollama:qwen2.5:1.5b`.

The other four local tasks did not use fallback.

### Public network smoke test

Source: Wikimedia Commons public WebM, no login:

```text
https://upload.wikimedia.org/wikipedia/commons/0/0d/%22We_should_do_it_ourselves%22_Francis_K%C3%A9r%C3%A9.webm
```

```bash
.venv-smoke/bin/python scripts/run-public-smoke.py \
  'https://upload.wikimedia.org/wikipedia/commons/0/0d/%22We_should_do_it_ourselves%22_Francis_K%C3%A9r%C3%A9.webm' \
  --output-root "$PWD/.tmp-public-smoke"

jq '{samples:[.]}' .tmp-public-smoke/public-smoke-report.json \
  > /tmp/lvt-public-verify.json
.venv-smoke/bin/python scripts/verify-real-e2e.py /tmp/lvt-public-verify.json
# verified 1 samples and 8 artifacts
```

Result: 47.488s, detected `en`, 6 segments, 1 speaker, Hy-MT2 primary used,
no fallback warning. Output:
`.tmp-public-smoke/exports/_We_should_do_it_ourselves__Francis_Kéré--public-0677a`.

## Artifact Invariants Verified

For all six successful real-media tasks (48 exported files):

- Exactly `source` and `zh-CN` TXT/SRT/VTT/JSON files exist and are non-empty.
- Source and Chinese cue counts, IDs, timestamps, speakers, and order match.
- `source_text` is preserved in both JSON files.
- `source.json` has empty `translated_text`; `zh-CN.json` stores Chinese only in
  `translated_text`.
- SRT and VTT files parse successfully and preserve cue timing.
- Segment timestamps remain within media duration.
- Speaker names are normalized by first appearance.

## Known Limitations

- Real Phase 1 tests use Whisper tiny to keep the repeatable test fast; Russian source
  text is less accurate than a production small/medium model.
- The synthetic fixtures are 11–16 seconds, not the later v0.1 requirement for
  30–120 second fixture coverage or a 20+ minute stress test.
- Only one public network URL was tested in this phase; remote availability may change.
- The FastAPI `TestClient` emits one upstream deprecation warning about `httpx`; it does
  not affect test results.
- Queue, cancellation, restart recovery, Chrome extension, installer, and package are
  intentionally outside Phase 1 and were not implemented or claimed as passed.
