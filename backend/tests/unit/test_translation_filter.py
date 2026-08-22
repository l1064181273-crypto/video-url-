from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lvt.engines.base import TranslationResult
from lvt.engines.translation import (
    FilteringTranslationEngine,
    TextDisposition,
    classify_text,
)


@dataclass
class RecordingTranslationEngine:
    version: str = "recording-1"
    calls: list[dict[int, str]] = field(default_factory=list)

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        self.calls.append(texts)
        translated = {
            key: {
                "Hello world.": "你好，世界。",
                "Visit https://example.com for details": "访问 https://example.com 查看详情",
                "NASA launched the mission in 2026": "NASA 在 2026 年发射了该任务",
            }[value]
            for key, value in texts.items()
        }
        return TranslationResult(translated, self.version, [])


@pytest.mark.parametrize(
    ("text", "disposition"),
    [
        ("https://example.com", TextDisposition.URL),
        ("00:03:21", TextDisposition.TIMECODE),
        ("2026", TextDisposition.NUMBER),
        ("Speaker 2", TextDisposition.SPEAKER_LABEL),
        ("NASA", TextDisposition.PROPER_TOKEN),
        ("GPT-5", TextDisposition.PROPER_TOKEN),
        ("OpenAI", TextDisposition.PROPER_TOKEN),
        ("Elon Musk", TextDisposition.PROPER_TOKEN),
        ("New York", TextDisposition.PROPER_TOKEN),
        ("Hello world.", TextDisposition.TRANSLATE),
        ("Visit https://example.com for details", TextDisposition.TRANSLATE),
        ("NASA launched the mission in 2026", TextDisposition.TRANSLATE),
    ],
)
def test_text_classification(text: str, disposition: TextDisposition) -> None:
    assert classify_text(text) is disposition


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com",
        "00:03:21",
        "2026",
        "Speaker 2",
        "NASA",
        "GPT-5",
        "OpenAI",
        "Elon Musk",
        "New York",
    ],
)
def test_complete_passthrough_text_never_calls_model(text: str) -> None:
    delegate = RecordingTranslationEngine()
    engine = FilteringTranslationEngine(delegate)

    result = engine.translate({1: text}, "en")

    assert delegate.calls == []
    assert result.texts == {1: text}
    assert result.engine_version == "passthrough:no-translation-required"


def test_mixed_batch_only_sends_translatable_ids_and_merges_in_order() -> None:
    delegate = RecordingTranslationEngine()
    engine = FilteringTranslationEngine(delegate)
    source = {
        1: "https://example.com",
        2: "Hello world.",
        3: "2026",
        4: "NASA launched the mission in 2026",
    }

    result = engine.translate(source, "en")

    assert delegate.calls == [
        {
            2: "Hello world.",
            4: "NASA launched the mission in 2026",
        }
    ]
    assert list(result.texts) == [1, 2, 3, 4]
    assert result.texts == {
        1: "https://example.com",
        2: "你好，世界。",
        3: "2026",
        4: "NASA 在 2026 年发射了该任务",
    }
    assert len(result.texts) == len(source)


def test_mixed_sentence_keeps_url_number_and_proper_token() -> None:
    delegate = RecordingTranslationEngine()
    result = FilteringTranslationEngine(delegate).translate(
        {
            1: "Visit https://example.com for details",
            2: "NASA launched the mission in 2026",
        },
        "en",
    )

    assert "https://example.com" in result.texts[1]
    assert "NASA" in result.texts[2]
    assert "2026" in result.texts[2]
