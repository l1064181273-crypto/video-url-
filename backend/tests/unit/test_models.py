import pytest

from lvt.core.models import Segment, Transcript, apply_translations


def make_segments() -> list[Segment]:
    return [
        Segment(
            id=1,
            start_ms=0,
            end_ms=1200,
            speaker="Speaker 1",
            source_language="ru",
            source_text="Привет.",
        ),
        Segment(
            id=2,
            start_ms=1300,
            end_ms=2500,
            speaker="Speaker 2",
            source_language="ru",
            source_text="Здравствуйте.",
        ),
    ]


def test_apply_translations_only_sets_translated_text() -> None:
    original = make_segments()
    translated = apply_translations(original, {1: "你好。", 2: "您好。"})

    assert [item.source_text for item in translated] == ["Привет.", "Здравствуйте."]
    assert [item.translated_text for item in translated] == ["你好。", "您好。"]
    assert [(item.start_ms, item.end_ms, item.speaker) for item in translated] == [
        (0, 1200, "Speaker 1"),
        (1300, 2500, "Speaker 2"),
    ]
    assert all(item.translated_text == "" for item in original)


@pytest.mark.parametrize(
    "translations",
    [
        {1: "你好。"},
        {1: "你好。", 2: "您好。", 3: "多余。"},
        {1: "你好。", 2: ""},
    ],
)
def test_apply_translations_rejects_invalid_mapping(
    translations: dict[int, str],
) -> None:
    with pytest.raises(ValueError):
        apply_translations(make_segments(), translations)


def test_transcript_rejects_non_continuous_ids() -> None:
    segments = make_segments()
    segments[1] = segments[1].model_copy(update={"id": 3})
    with pytest.raises(ValueError, match="continuous"):
        Transcript(
            job_id="job-1",
            source_url="https://example.test/video",
            title="Test",
            duration_ms=3000,
            detected_language="ru",
            segments=segments,
        )
