#!/usr/bin/env bash
# Generate small, reproducible test-media assets using macOS `say`.
# No copyrighted content. Requires ffmpeg on PATH (add_paths from static-ffmpeg,
# or Homebrew ffmpeg). Usage: bash scripts/make-test-assets.sh [ffmpeg_path]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/test-assets/generated"
FFMPEG="${1:-ffmpeg}"
mkdir -p "$OUT"

echo "==> output dir: $OUT"
echo "==> ffmpeg: $FFMPEG"

# 1) Single-speaker English 30-60s
say -v Samantha -o "$OUT/en_single.aiff" \
  "Welcome to the local video transcriber test. This is a single speaker English clip. \
It should be transcribed accurately with monotonic timestamps. \
The quick brown fox jumps over the lazy dog. Local processing keeps your data private."

# 2) Single-speaker Russian 30-60s
say -v Milena -o "$OUT/ru_single.aiff" \
  "Это тестовая запись на русском языке. Один говорящий. \
Система должна автоматически определить язык и выполнить транскрипцию. \
Обработка выполняется локально на вашем компьютере."

# 3) Two clearly different voices alternating (diarization) 60-120s
say -v Daniel   -o "$OUT/two_a.aiff" "Hello, I am the first speaker. I will ask a few questions today."
say -v Samantha -o "$OUT/two_b.aiff" "And I am the second speaker. I will answer each of your questions clearly."
say -v Daniel   -o "$OUT/two_c.aiff" "Great. What is the main benefit of running everything locally?"
say -v Samantha -o "$OUT/two_d.aiff" "Privacy. No audio or text is ever uploaded to any third party service."

# 4) Short silence + tone (pure music / non-speech) sample
"$FFMPEG" -y -f lavfi -i anullsrc=r=16000:cl=mono -t 3 "$OUT/silence.wav" -loglevel error
"$FFMPEG" -y -f lavfi -i "sine=frequency=440:sample_rate=16000:duration=4" "$OUT/tone.wav" -loglevel error

# Convert aiff -> wav (mono 16k) and concatenate the two-speaker clips
for f in en_single ru_single; do
  "$FFMPEG" -y -i "$OUT/$f.aiff" -ac 1 -ar 16000 "$OUT/$f.wav" -loglevel error
done

# Concatenate two-speaker parts into one alternating conversation
cat > "$OUT/concat.txt" <<EOF
file '$OUT/two_a.aiff'
file '$OUT/two_b.aiff'
file '$OUT/two_c.aiff'
file '$OUT/two_d.aiff'
EOF
"$FFMPEG" -y -f concat -safe 0 -i "$OUT/concat.txt" -ac 1 -ar 16000 "$OUT/two_speakers.wav" -loglevel error

# 5) Filename with Chinese + spaces
cp "$OUT/en_single.wav" "$OUT/中文 名字 test.wav"

echo "==> generated:"
ls -la "$OUT"/*.wav
