from lvt.engines.base import ASRSegment, SpeakerInterval
from lvt.pipeline.segmenter import assign_speakers


def test_no_diarization_result_maps_all_segments_to_speaker_one() -> None:
    result = assign_speakers(
        [
            ASRSegment(0, 1000, "First."),
            ASRSegment(1100, 2000, "Second."),
        ],
        [],
        source_language="en",
    )
    assert [item.speaker for item in result] == ["Speaker 1", "Speaker 1"]


def test_maximum_overlap_and_first_appearance_control_speaker_mapping() -> None:
    result = assign_speakers(
        [
            ASRSegment(0, 1000, "First."),
            ASRSegment(1000, 2000, "Second."),
            ASRSegment(2000, 3000, "Third."),
        ],
        [
            SpeakerInterval(0, 900, "raw-b"),
            SpeakerInterval(900, 2100, "raw-a"),
            SpeakerInterval(2100, 3000, "raw-b"),
        ],
        source_language="en",
    )
    assert [item.speaker for item in result] == [
        "Speaker 1",
        "Speaker 2",
        "Speaker 1",
    ]
