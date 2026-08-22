from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from lvt.engines.base import TranslationResult
from lvt.engines.translation import protect_texts, restore_protected_text

RequestFn = Callable[[str, dict[str, Any], float], dict[str, Any]]
LANGUAGE_NAMES = {
    "zh": "Chinese",
    "en": "English",
    "fr": "French",
    "pt": "Portuguese",
    "es": "Spanish",
    "ja": "Japanese",
    "tr": "Turkish",
    "ru": "Russian",
    "ar": "Arabic",
    "ko": "Korean",
    "th": "Thai",
    "it": "Italian",
    "de": "German",
    "vi": "Vietnamese",
    "ms": "Malay",
    "id": "Indonesian",
    "tl": "Filipino",
    "hi": "Hindi",
    "zh-hant": "Traditional Chinese",
    "pl": "Polish",
    "cs": "Czech",
    "nl": "Dutch",
    "km": "Khmer",
    "my": "Burmese",
    "fa": "Persian",
    "gu": "Gujarati",
    "ur": "Urdu",
    "te": "Telugu",
    "mr": "Marathi",
    "he": "Hebrew",
    "bn": "Bengali",
    "ta": "Tamil",
    "uk": "Ukrainian",
    "bo": "Tibetan",
    "kk": "Kazakh",
    "mn": "Mongolian",
    "ug": "Uyghur",
    "yue": "Cantonese",
}


def _default_request(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result: dict[str, Any] = json.load(response)
        return result


class TranslationEngineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def resolve_language_name(code: str) -> str:
    normalized = code.strip().lower().replace("_", "-")
    try:
        return LANGUAGE_NAMES[normalized]
    except KeyError as exc:
        raise TranslationEngineError(
            "UNSUPPORTED_SOURCE_LANGUAGE",
            f"Hy-MT2 does not support language code: {code}",
        ) from exc


def validate_translation_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("translation contains an empty or non-string value")
    cleaned = value.strip()
    if "\n" in cleaned or "{" in cleaned or "}" in cleaned:
        raise ValueError("translation contains wrapper text or multiple lines")
    if re.search(r"(?:注[:：]|备注[:：]|note[:：]|speaker\s*\d|-->)", cleaned, re.I):
        raise ValueError("translation contains notes, labels, or timestamps")
    if not re.search(r"[\u3400-\u9fff]", cleaned):
        raise ValueError("Simplified Chinese translation contains no CJK text")
    return cleaned


class OllamaTranslationEngine:
    version = "ollama-api-v1"

    def __init__(
        self,
        *,
        model: str = "hy-mt2:1.8b-q4km-fixed",
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 180,
        max_attempts: int = 3,
        request_fn: RequestFn = _default_request,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.request_fn = request_fn
        self.version = f"ollama:{model}"

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        if not texts:
            return TranslationResult({}, self.version, [])
        source_language_name = resolve_language_name(source_language)
        protected_texts, placeholder_manifest = protect_texts(texts)
        source_data = {str(key): value for key, value in protected_texts.items()}
        manifest_data = {
            str(key): [token.placeholder for token in tokens]
            for key, tokens in placeholder_manifest.items()
        }
        prompt = (
            "### Task\n"
            f"Translate the user-facing text values from {source_language_name} into "
            "Simplified Chinese.\n\n"
            "### Strict Rules\n"
            "1. Return ONLY one valid JSON object with the exact same keys.\n"
            "2. Translate ONLY values. Never change, add, remove, merge, or reorder keys.\n"
            "3. Values contain translated text only, with no notes or labels.\n"
            "4. Copy every LVT_TOKEN placeholder exactly once, in the listed order.\n"
            "5. Never modify, remove, duplicate, or reorder placeholders.\n\n"
            "### Protected Placeholders By ID\n"
            f"{json.dumps(manifest_data, ensure_ascii=False)}\n\n"
            "### Source Data\n"
            f"{json.dumps(source_data, ensure_ascii=False)}"
        )
        last_error = "unknown translation failure"
        for _attempt in range(1, self.max_attempts + 1):
            try:
                response = self.request_fn(
                    f"{self.base_url}/api/chat",
                    {
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "format": "json",
                        "options": {
                            "temperature": 0,
                            "top_p": 0.6,
                            "top_k": 20,
                            "repeat_penalty": 1.05,
                            "seed": 42,
                            "num_predict": 1024,
                        },
                    },
                    self.timeout,
                )
                content = response["message"]["content"]
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("translation response must be a JSON object")
                result = {int(key): value for key, value in parsed.items()}
                if set(result) != set(texts):
                    raise ValueError("translation id set mismatch")
                validated = {
                    key: restore_protected_text(
                        validate_translation_text(value),
                        placeholder_manifest[key],
                    )
                    for key, value in result.items()
                }
                return TranslationResult(
                    texts=validated,
                    engine_version=self.version,
                    warnings=[],
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:
                last_error = str(exc)
        raise TranslationEngineError(
            "TRANSLATION_FAILED",
            f"{self.model} failed after {self.max_attempts} attempts: {last_error}",
        )


class FallbackTranslationEngine:
    def __init__(
        self,
        *,
        primary: OllamaTranslationEngine,
        fallback: OllamaTranslationEngine,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.version = f"primary={primary.version};fallback={fallback.version}"

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        try:
            return self.primary.translate(texts, source_language)
        except TranslationEngineError as primary_error:
            if primary_error.code == "UNSUPPORTED_SOURCE_LANGUAGE":
                raise
            try:
                result = self.fallback.translate(texts, source_language)
            except TranslationEngineError as fallback_error:
                raise TranslationEngineError(
                    "TRANSLATION_ALL_MODELS_FAILED",
                    f"primary failed ({primary_error}); fallback failed ({fallback_error})",
                ) from fallback_error
            warning = (
                f"Translation fallback used: {self.primary.model} failed; "
                f"used {self.fallback.model}. Primary error: {primary_error}"
            )
            return TranslationResult(
                texts=result.texts,
                engine_version=result.engine_version,
                warnings=[*result.warnings, warning],
            )
