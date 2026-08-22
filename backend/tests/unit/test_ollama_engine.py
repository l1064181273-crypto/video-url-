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


def test_both_translation_models_fail() -> None:
    def request(_url: str, _payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        return {"message": {"content": "{}"}}

    engine = FallbackTranslationEngine(
        primary=OllamaTranslationEngine(max_attempts=1, request_fn=request),
        fallback=OllamaTranslationEngine(model="qwen2.5:1.5b", max_attempts=1, request_fn=request),
    )
    with pytest.raises(TranslationEngineError, match="TRANSLATION_ALL_MODELS_FAILED"):
        engine.translate({1: "Hello."}, "en")
