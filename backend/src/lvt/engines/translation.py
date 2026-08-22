from __future__ import annotations

import re
from enum import StrEnum

from lvt.engines.base import TranslationEngine, TranslationResult


class TextDisposition(StrEnum):
    URL = "url"
    TIMECODE = "timecode"
    NUMBER = "number"
    SPEAKER_LABEL = "speaker_label"
    PROPER_TOKEN = "proper_token"
    TRANSLATE = "translate"


URL_PATTERN = re.compile(r"https?://[^\s`<>{}\[\]()\"，。！？]+", re.I)
TIMECODE_PATTERN = re.compile(r"(?<!\d)(?:\d{1,2}:)?[0-5]\d:[0-5]\d(?:[.,]\d{1,3})?(?!\d)")
NUMBER_PATTERN = re.compile(r"(?<![\w-])[+-]?\d+(?:[.,]\d+)*(?![\w-])")
SPEAKER_PATTERN = re.compile(r"(?:speaker|spk|说话人)\s*[_#:-]?\s*\d+", re.I)
ACRONYM_PATTERN = re.compile(r"\b[A-Z]{2,}(?:[-_.][A-Z0-9]+)*\b")
CAMEL_PRODUCT_PATTERN = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9.-]*\b")
TITLE_NAME_PATTERN = re.compile(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}")
FULL_NUMBER_PATTERN = re.compile(r"[+-]?\d+(?:[.,]\d+)*")


def classify_text(text: str) -> TextDisposition:
    stripped = text.strip()
    if URL_PATTERN.fullmatch(stripped):
        return TextDisposition.URL
    if TIMECODE_PATTERN.fullmatch(stripped):
        return TextDisposition.TIMECODE
    if FULL_NUMBER_PATTERN.fullmatch(stripped):
        return TextDisposition.NUMBER
    if SPEAKER_PATTERN.fullmatch(stripped):
        return TextDisposition.SPEAKER_LABEL
    if (
        ACRONYM_PATTERN.fullmatch(stripped)
        or CAMEL_PRODUCT_PATTERN.fullmatch(stripped)
        or TITLE_NAME_PATTERN.fullmatch(stripped)
    ):
        return TextDisposition.PROPER_TOKEN
    return TextDisposition.TRANSLATE


def protected_tokens(text: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    for pattern in (
        URL_PATTERN,
        TIMECODE_PATTERN,
        SPEAKER_PATTERN,
        ACRONYM_PATTERN,
        CAMEL_PRODUCT_PATTERN,
        NUMBER_PATTERN,
    ):
        matches.extend((match.start(), match.group(0)) for match in pattern.finditer(text))
    seen: set[str] = set()
    ordered: list[str] = []
    for _position, token in sorted(matches):
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


class FilteringTranslationEngine:
    def __init__(self, delegate: TranslationEngine) -> None:
        self.delegate = delegate
        self.version = f"filtering:{delegate.version}"

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        passthrough: dict[int, str] = {}
        translatable: dict[int, str] = {}
        for segment_id, text in texts.items():
            if classify_text(text) is TextDisposition.TRANSLATE:
                translatable[segment_id] = text
            else:
                passthrough[segment_id] = text

        if not translatable:
            return TranslationResult(
                texts={segment_id: texts[segment_id] for segment_id in texts},
                engine_version="passthrough:no-translation-required",
                warnings=[],
            )

        translated = self.delegate.translate(translatable, source_language)
        if set(translated.texts) != set(translatable):
            raise ValueError("translation delegate returned a mismatched id set")
        merged = {
            segment_id: (
                passthrough[segment_id]
                if segment_id in passthrough
                else translated.texts[segment_id]
            )
            for segment_id in texts
        }
        if set(merged) != set(texts):
            raise RuntimeError("translation merge changed the complete id set")
        return TranslationResult(
            texts=merged,
            engine_version=translated.engine_version,
            warnings=translated.warnings,
        )
