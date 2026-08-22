from __future__ import annotations

from lvt.core.models import Segment
from lvt.engines.base import ASRSegment, SpeakerInterval


def overlap_ms(asr: ASRSegment, speaker: SpeakerInterval) -> int:
    return max(0, min(asr.end_ms, speaker.end_ms) - max(asr.start_ms, speaker.start_ms))


def _nearest_speaker(
    asr: ASRSegment,
    intervals: list[SpeakerInterval],
    *,
    max_distance_ms: int,
) -> str:
    center = (asr.start_ms + asr.end_ms) // 2
    nearest = min(
        intervals,
        key=lambda item: abs(center - ((item.start_ms + item.end_ms) // 2)),
    )
    nearest_center = (nearest.start_ms + nearest.end_ms) // 2
    if abs(center - nearest_center) > max_distance_ms:
        return "unknown"
    return nearest.raw_speaker


def assign_speakers(
    asr_segments: list[ASRSegment],
    speaker_intervals: list[SpeakerInterval],
    *,
    source_language: str,
    nearest_max_distance_ms: int = 2000,
) -> list[Segment]:
    if not asr_segments:
        return []
    if not speaker_intervals:
        raw_speakers = ["single-speaker"] * len(asr_segments)
    else:
        raw_speakers = []
        for asr in asr_segments:
            best = max(speaker_intervals, key=lambda item: overlap_ms(asr, item))
            raw_speakers.append(
                best.raw_speaker
                if overlap_ms(asr, best) > 0
                else _nearest_speaker(
                    asr, speaker_intervals, max_distance_ms=nearest_max_distance_ms
                )
            )

    speaker_names: dict[str, str] = {}
    output: list[Segment] = []
    for index, (asr, raw_speaker) in enumerate(
        zip(asr_segments, raw_speakers, strict=True), start=1
    ):
        if raw_speaker not in speaker_names:
            speaker_names[raw_speaker] = f"Speaker {len(speaker_names) + 1}"
        output.append(
            Segment(
                id=index,
                start_ms=asr.start_ms,
                end_ms=asr.end_ms,
                speaker=speaker_names[raw_speaker],
                source_language=source_language,
                source_text=asr.text.strip(),
            )
        )
    return output
