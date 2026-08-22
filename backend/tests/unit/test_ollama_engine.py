import json
from typing import Any

import pytest

from lvt.engines.ollama import (
    LANGUAGE_NAMES,
    FallbackTranslationEngine,
    OllamaTranslationEngine,
    TranslationEngineError,
    resolve_language_name,
)


def test_ollama_translation_retries_invalid_json_then_succeeds() -> None:
    responses = iter(
        [
            {"message": {"content": "not json"}},
            {"message": {"content": json.dumps({"1": "你好。"}, ensure_ascii=False)}},
            {
                "message": {
                    "content": json.dumps({"1": "你好。", "2": "本地处理。"}, ensure_ascii=False)
                }
            },
        ]
    )
    calls: list[dict[str, Any]] = []

    def request(_url: str, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        calls.append(payload)
        return next(responses)

    engine = OllamaTranslationEngine(request_fn=request)
    result = engine.translate({1: "Hello.", 2: "Local processing."}, "en")

    assert result.texts == {1: "你好。", 2: "本地处理。"}
    assert result.engine_version == "ollama:hy-mt2:1.8b-q4km-fixed"
    assert result.warnings == []
    assert len(calls) == 3
    assert all(call["format"] == "json" for call in calls)
    assert all(call["options"]["num_predict"] == 1024 for call in calls)


def test_ollama_translation_fails_after_bounded_retries() -> None:
    def request(_url: str, _payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        return {"message": {"content": "{}"}}

    engine = OllamaTranslationEngine(request_fn=request, max_attempts=2)
    with pytest.raises(TranslationEngineError, match="after 2 attempts"):
        engine.translate({1: "Hello."}, "en")


def test_language_mapping_covers_official_hy_mt2_languages() -> None:
    assert len(LANGUAGE_NAMES) == 38
    assert resolve_language_name("uk") == "Ukrainian"
    assert resolve_language_name("kk") == "Kazakh"
    assert resolve_language_name("fa") == "Persian"
    assert resolve_language_name("zh_Hant") == "Traditional Chinese"


def test_unknown_language_is_rejected_without_request() -> None:
    def request(_url: str, _payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        raise AssertionError("request must not run")

    engine = OllamaTranslationEngine(request_fn=request)
    with pytest.raises(TranslationEngineError, match="UNSUPPORTED_SOURCE_LANGUAGE"):
        engine.translate({1: "Saluton."}, "eo")


def test_primary_failure_uses_fallback_with_visible_warning() -> None:
    def request(_url: str, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        if payload["model"] == "hy-mt2:1.8b-q4km-fixed":
            return {"message": {"content": "{}"}}
        return {"message": {"content": '{"1":"你好。"}'}}

    engine = FallbackTranslationEngine(
        primary=OllamaTranslationEngine(
            model="hy-mt2:1.8b-q4km-fixed", max_attempts=2, request_fn=request
        ),
        fallback=OllamaTranslationEngine(model="qwen2.5:1.5b", max_attempts=2, request_fn=request),
    )
    result = engine.translate({1: "Hello."}, "en")

    assert result.texts == {1: "你好。"}
    assert result.engine_version == "ollama:qwen2.5:1.5b"
    assert "fallback used" in result.warnings[0].lower()
    assert "hy-mt2:1.8b-q4km-fixed" in result.warnings[0]


def test_semantically_invalid_translation_retries_then_falls_back() -> None:
    primary_calls = 0

    def request(_url: str, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        nonlocal primary_calls
        if payload["model"] == "hy-mt2:1.8b-q4km-fixed":
            primary_calls += 1
            return {
                "message": {
                    "content": json.dumps(
                        {"1": "译文。\\n\\n（注：额外解释） {}"},
                        ensure_ascii=False,
                    )
                }
            }
        return {"message": {"content": '{"1":"有效译文。"}'}}

    engine = FallbackTranslationEngine(
        primary=OllamaTranslationEngine(max_attempts=3, request_fn=request),
        fallback=OllamaTranslationEngine(model="qwen2.5:1.5b", max_attempts=2, request_fn=request),
    )
    result = engine.translate({1: "Source."}, "en")

    assert primary_calls == 3
    assert result.texts == {1: "有效译文。"}
    assert result.warnings


def test_ordinary_english_without_cjk_is_rejected() -> None:
    def request(_url: str, _payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        return {"message": {"content": '{"1":"Still English."}'}}

    engine = OllamaTranslationEngine(max_attempts=1, request_fn=request)
    with pytest.raises(TranslationEngineError, match="contains no CJK"):
        engine.translate({1: "Ordinary English sentence."}, "en")


def test_mixed_translation_must_preserve_protected_tokens() -> None:
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            {"message": {"content": '{"1":"请访问网站查看详情。"}'}},
            {
                "message": {
                    "content": ('{"1":"请访问 LVT_TOKEN_0001 查看 LVT_TOKEN_0002 年详情。"}')
                }
            },
        ]
    )

    def request(_url: str, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        calls.append(payload)
        return next(responses)

    engine = OllamaTranslationEngine(
        max_attempts=2,
        request_fn=request,
    )
    result = engine.translate(
        {1: "Visit https://example.com for 2026 details."},
        "en",
    )

    assert result.texts == {1: "请访问 https://example.com 查看 2026 年详情。"}
    prompt = calls[0]["messages"][0]["content"]
    assert "LVT_TOKEN_0001" in prompt
    assert "LVT_TOKEN_0002" in prompt
    assert "Protected Placeholders" in prompt


def test_repeated_nasa_must_be_returned_once_per_occurrence() -> None:
    engine = OllamaTranslationEngine(
        max_attempts=1,
        request_fn=lambda _url, _payload, _timeout: {
            "message": {"content": ('{"1":"LVT_TOKEN_0001 在 LVT_TOKEN_0003 年发射了任务。"}')}
        },
    )

    with pytest.raises(TranslationEngineError, match="placeholder sequence mismatch"):
        engine.translate({1: "NASA and NASA launched in 2026"}, "en")


def test_repeated_url_must_be_returned_once_per_occurrence() -> None:
    engine = OllamaTranslationEngine(
        max_attempts=1,
        request_fn=lambda _url, _payload, _timeout: {
            "message": {"content": '{"1":"访问 LVT_TOKEN_0001。"}'}
        },
    )

    with pytest.raises(TranslationEngineError, match="placeholder sequence mismatch"):
        engine.translate(
            {1: "Visit https://example.com and https://example.com"},
            "en",
        )


def test_number_placeholder_requires_strict_boundaries() -> None:
    engine = OllamaTranslationEngine(
        max_attempts=1,
        request_fn=lambda _url, _payload, _timeout: {
            "message": {"content": '{"1":"任务于 1LVT_TOKEN_0001 年启动。"}'}
        },
    )

    with pytest.raises(TranslationEngineError, match="placeholder boundary"):
        engine.translate({1: "The mission started in 2026"}, "en")


def test_protected_token_order_must_not_change() -> None:
    engine = OllamaTranslationEngine(
        max_attempts=1,
        request_fn=lambda _url, _payload, _timeout: {
            "message": {
                "content": ('{"1":"LVT_TOKEN_0002 由 LVT_TOKEN_0001 于 LVT_TOKEN_0003 年发布。"}')
            }
        },
    )

    with pytest.raises(TranslationEngineError, match="placeholder sequence mismatch"):
        engine.translate({1: "NASA launched GPT-5 in 2026"}, "en")


@pytest.mark.parametrize(
    "bad_translation",
    [
        "任务已完成。",
        "LVT_TOKEN_0001 和 LVT_TOKEN_9999 完成任务。",
        "LVT_TOKEN_001 完成任务。",
        "LVT_TOKEN_0001 和 LVT_TOKEN_0001 完成任务。",
    ],
)
def test_deleted_added_modified_or_duplicated_placeholder_is_rejected(
    bad_translation: str,
) -> None:
    response = json.dumps({"1": bad_translation}, ensure_ascii=False)
    engine = OllamaTranslationEngine(
        max_attempts=1,
        request_fn=lambda _url, _payload, _timeout: {"message": {"content": response}},
    )

    with pytest.raises(TranslationEngineError, match="placeholder"):
        engine.translate({1: "NASA completed the mission in 2026"}, "en")


def test_repeated_tokens_restore_exact_count_boundaries_and_order() -> None:
    engine = OllamaTranslationEngine(
        max_attempts=1,
        request_fn=lambda _url, _payload, _timeout: {
            "message": {
                "content": ('{"1":"LVT_TOKEN_0001 和 LVT_TOKEN_0002 于 LVT_TOKEN_0003 年发射。"}')
            }
        },
    )

    result = engine.translate({1: "NASA and NASA launched in 2026"}, "en")

    assert result.texts == {1: "NASA 和 NASA 于 2026 年发射。"}
    assert result.texts[1].count("NASA") == 2
    assert "12026" not in result.texts[1]


def test_both_translation_models_fail() -> None:
    def request(_url: str, _payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        return {"message": {"content": "{}"}}

    engine = FallbackTranslationEngine(
        primary=OllamaTranslationEngine(max_attempts=1, request_fn=request),
        fallback=OllamaTranslationEngine(model="qwen2.5:1.5b", max_attempts=1, request_fn=request),
    )
    with pytest.raises(TranslationEngineError, match="TRANSLATION_ALL_MODELS_FAILED"):
        engine.translate({1: "Hello."}, "en")
