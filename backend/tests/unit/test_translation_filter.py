from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from lvt.engines.base import TranslationResult
from lvt.engines.translation import (
    FilteringTranslationEngine,
    TextDisposition,
    classify_text,
    protect_texts,
    protected_tokens,
    restore_protected_text,
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
                "Good Morning": "早上好",
                "Thank You": "谢谢",
                "This Is Fine": "这很好",
                "Hello World": "你好，世界",
                "STOP": "停止",
                "HELLO": "你好",
                "Visit https://example.com for details": "访问 https://example.com 查看详情",
                "Visit https://example.com.": "请访问 https://example.com。",
                "NASA launched the mission in 2026": "NASA 在 2026 年发射了该任务",
                "NASA launched in 2026.": "NASA 于 2026 年发射。",
                "Speaker 1 said hello.": "Speaker 1 说你好。",
                "Visit https://example.com,then continue": "访问 https://example.com，然后继续",
                "Visit https://example.com。Continue": "访问 https://example.com。继续",
                "访问https://example.com。继续": "访问https://example.com。继续",
                "https://example.com。继续": "https://example.com。继续",
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
        ("Elon Musk", TextDisposition.TRANSLATE),
        ("New York", TextDisposition.TRANSLATE),
        ("Good Morning", TextDisposition.TRANSLATE),
        ("Thank You", TextDisposition.TRANSLATE),
        ("This Is Fine", TextDisposition.TRANSLATE),
        ("Hello World", TextDisposition.TRANSLATE),
        ("STOP", TextDisposition.TRANSLATE),
        ("HELLO", TextDisposition.TRANSLATE),
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
    ],
)
def test_complete_passthrough_text_never_calls_model(text: str) -> None:
    delegate = RecordingTranslationEngine()
    engine = FilteringTranslationEngine(delegate)

    result = engine.translate({1: text}, "en")

    assert delegate.calls == []
    assert result.texts == {1: text}
    assert result.engine_version == "passthrough:no-translation-required"


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com.",
        "https://example.com,",
        "(https://example.com)",
        "NASA.",
        "OpenAI!",
        "GPT-5,",
        "2026.",
        "2026-2027",
        "  2026-2027.  ",
        "Speaker 1:",
        '  [ "NASA" ]?!  ',
        "（OpenAI！）",
    ],
)
def test_atomic_passthrough_with_outer_punctuation_preserves_exact_text(
    text: str,
) -> None:
    delegate = RecordingTranslationEngine()
    result = FilteringTranslationEngine(delegate).translate({1: text}, "en")

    assert delegate.calls == []
    assert result.texts == {1: text}


def test_mixed_text_around_protected_tokens_still_calls_model() -> None:
    source = {
        1: "Visit https://example.com.",
        2: "NASA launched in 2026.",
        3: "Speaker 1 said hello.",
    }
    delegate = RecordingTranslationEngine()

    result = FilteringTranslationEngine(delegate).translate(source, "en")

    assert delegate.calls == [source]
    assert result.texts == {
        1: "请访问 https://example.com。",
        2: "NASA 于 2026 年发射。",
        3: "Speaker 1 说你好。",
    }


def test_ordinary_title_case_and_uppercase_text_calls_model() -> None:
    delegate = RecordingTranslationEngine()
    engine = FilteringTranslationEngine(delegate)
    source = {
        1: "Good Morning",
        2: "Thank You",
        3: "This Is Fine",
        4: "Hello World",
        5: "STOP",
        6: "HELLO",
    }

    result = engine.translate(source, "en")

    assert delegate.calls == [source]
    assert result.texts == {
        1: "早上好",
        2: "谢谢",
        3: "这很好",
        4: "你好，世界",
        5: "停止",
        6: "你好",
    }


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


@pytest.mark.parametrize(
    "text",
    [
        "发布于2026年",
        "2026年发布",
        "2026年",
        "В2026году",
        "2026г.",
        "2026년",
    ],
)
def test_unicode_adjacent_number_is_protected(text: str) -> None:
    assert "2026" in protected_tokens(text)


@pytest.mark.parametrize("text", ["abc2026", "GPT5", "version2"])
def test_ascii_identifier_number_is_not_split(text: str) -> None:
    assert protected_tokens(text) == []


def test_nonce_collision_retries_across_all_segments() -> None:
    candidates = iter(["COLLIDE", "SAFE"])
    source = {
        1: "Literal LVT_COLLIDE_TOKEN_0001 and NASA",
        2: "The year is 2026",
    }

    protected, manifests = protect_texts(
        source,
        nonce_factory=lambda: next(candidates),
    )

    assert "LVT_SAFE_TOKEN_0001" in protected[1]
    assert "LVT_SAFE_TOKEN_0002" in protected[1]
    assert "LVT_SAFE_TOKEN_0003" in protected[2]
    assert all(
        token.placeholder.startswith("LVT_SAFE_TOKEN_")
        for tokens in manifests.values()
        for token in tokens
    )


def test_literal_old_placeholder_and_url_restore_in_one_pass() -> None:
    source = {
        1: "Keep LVT_TOKEN_0001 unchanged",
        2: "https://example.com/LVT_TOKEN_0002 and NASA",
    }
    protected, manifests = protect_texts(source, nonce_factory=lambda: "BATCH")

    restored_first = restore_protected_text(
        f"保留 {manifests[1][0].placeholder} 不变",
        manifests[1],
    )
    restored_second = restore_protected_text(
        f"{manifests[2][0].placeholder} 和 {manifests[2][1].placeholder}",
        manifests[2],
    )

    assert restored_first == "保留 LVT_TOKEN_0001 不变"
    assert restored_second == "https://example.com/LVT_TOKEN_0002 和 NASA"
    assert "LVT_BATCH_TOKEN_" not in restored_first + restored_second


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "https://en.wikipedia.org/wiki/Foo_(bar)",
            ["https://en.wikipedia.org/wiki/Foo_(bar)"],
        ),
        (
            "https://example.com/a_(b)?x=2026#part",
            ["https://example.com/a_(b)?x=2026#part"],
        ),
        ("Visit https://example.com.", ["https://example.com"]),
        ("Visit https://example.com, then continue.", ["https://example.com"]),
        (
            "访问 https://例子.测试/路径_(一)?年份=2026#部分。",
            ["https://例子.测试/路径_(一)?年份=2026#部分"],
        ),
    ],
)
def test_complete_url_extraction(text: str, expected: list[str]) -> None:
    assert protected_tokens(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Visit https://example.com,then continue",
        "Visit https://example.com。Continue",
        "访问https://example.com。继续",
        "https://example.com。继续",
    ],
)
def test_url_followed_by_unspaced_body_is_not_atomic_passthrough(text: str) -> None:
    assert classify_text(text) is TextDisposition.TRANSLATE
    assert protected_tokens(text) == ["https://example.com"]

    protected, _manifests = protect_texts({1: text}, nonce_factory=lambda: "URLBODY")

    assert "https://example.com" not in protected[1]
    assert "LVT_URLBODY_TOKEN_0001" in protected[1]
    assert any(body in protected[1] for body in (",then continue", "。Continue", "。继续"))


def test_url_followed_by_unspaced_body_is_sent_to_translation_delegate() -> None:
    source = {
        1: "Visit https://example.com,then continue",
        2: "Visit https://example.com。Continue",
        3: "访问https://example.com。继续",
        4: "https://example.com。继续",
    }
    delegate = RecordingTranslationEngine()

    result = FilteringTranslationEngine(delegate).translate(source, "en")

    assert delegate.calls == [source]
    assert list(result.texts) == list(source)
    assert len(result.texts) == len(source)


@pytest.mark.parametrize(
    ("text", "disposition"),
    [
        ("  ([“NASA”])?!  ", TextDisposition.PROPER_TOKEN),
        ("([NASA)]", TextDisposition.TRANSLATE),
        ("[NASA", TextDisposition.TRANSLATE),
        (")NASA(", TextDisposition.TRANSLATE),
        ("”NASA“", TextDisposition.TRANSLATE),
        ("‘NASA‘", TextDisposition.TRANSLATE),
    ],
)
def test_atomic_wrapping_requires_ordered_balanced_delimiters(
    text: str,
    disposition: TextDisposition,
) -> None:
    assert classify_text(text) is disposition


def test_valid_nested_atomic_wrapping_preserves_every_character() -> None:
    text = "  ([“NASA”])?!  "
    delegate = RecordingTranslationEngine()

    result = FilteringTranslationEngine(delegate).translate({1: text}, "en")

    assert delegate.calls == []
    assert result.texts == {1: text}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-2027", "2026-2027"),
        ("10-20", "10-20"),
        ("123-456", "123-456"),
        ("2026–2027", "2026–2027"),
        ("2026—2027", "2026—2027"),
        ("计划为2026-2027年", "2026-2027"),
        ("기간은2026-2027년", "2026-2027"),
    ],
)
def test_numeric_range_is_one_protected_token(text: str, expected: str) -> None:
    assert protected_tokens(text) == [expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-08-22", "2026-08-22"),
        ("010-1234-5678", "010-1234-5678"),
        ("2026–08–22", "2026–08–22"),
        ("010—1234—5678", "010—1234—5678"),
        ("2026/08/22", "2026/08/22"),
        ("计划为2026-08-22发布", "2026-08-22"),
        ("날짜는2026-08-22입니다", "2026-08-22"),
        ("Дата2026-08-22года", "2026-08-22"),
    ],
)
def test_multi_part_numeric_sequence_is_one_protected_token(
    text: str,
    expected: str,
) -> None:
    assert protected_tokens(text) == [expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GPT-5", ["GPT-5"]),
        ("abc-2026", []),
        ("version-2", []),
        ("GPT5", []),
    ],
)
def test_numeric_range_does_not_split_ascii_identifiers(
    text: str,
    expected: list[str],
) -> None:
    assert protected_tokens(text) == expected


def test_repeated_numeric_ranges_use_distinct_placeholders_and_restore_in_order() -> None:
    source = {1: "From 2026-2027 to 2026-2027"}
    protected, manifests = protect_texts(source, nonce_factory=lambda: "RANGE")

    assert [token.original for token in manifests[1]] == [
        "2026-2027",
        "2026-2027",
    ]
    assert manifests[1][0].placeholder != manifests[1][1].placeholder
    restored = restore_protected_text(
        f"从 {manifests[1][0].placeholder} 到 {manifests[1][1].placeholder}",
        manifests[1],
    )
    assert restored == "从 2026-2027 到 2026-2027"


def test_repeated_multi_part_numeric_sequences_restore_in_order() -> None:
    source = {1: "Call 010-1234-5678 on 2026-08-22, then 010-1234-5678"}
    protected, manifests = protect_texts(source, nonce_factory=lambda: "MULTIPART")

    assert [token.original for token in manifests[1]] == [
        "010-1234-5678",
        "2026-08-22",
        "010-1234-5678",
    ]
    assert len({token.placeholder for token in manifests[1]}) == 3
    restored = restore_protected_text(
        f"请拨打 {manifests[1][0].placeholder}，日期 {manifests[1][1].placeholder}，"
        f"不要重复 {manifests[1][2].placeholder}",
        manifests[1],
    )
    assert restored == "请拨打 010-1234-5678，日期 2026-08-22，不要重复 010-1234-5678"
