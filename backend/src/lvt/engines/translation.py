from __future__ import annotations

import re
from dataclasses import dataclass
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
EXPLICIT_PROPER_TOKENS = frozenset({"NASA", "OpenAI"})
EXPLICIT_PROPER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(token) for token in sorted(EXPLICIT_PROPER_TOKENS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])"
)
STRONG_PRODUCT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Z]{2,}[A-Z0-9]*(?:-[A-Z0-9-]*\d[A-Z0-9-]*)"
    r"(?![A-Za-z0-9_])"
)
FULL_NUMBER_PATTERN = re.compile(r"[+-]?\d+(?:[.,]\d+)*")
PLACEHOLDER_PATTERN = re.compile(r"LVT_TOKEN_\d{4,}")


@dataclass(frozen=True)
class ProtectedOccurrence:
    start: int
    end: int
    value: str


@dataclass(frozen=True)
class PlaceholderToken:
    placeholder: str
    original: str


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
    if stripped in EXPLICIT_PROPER_TOKENS or STRONG_PRODUCT_PATTERN.fullmatch(stripped):
        return TextDisposition.PROPER_TOKEN
    return TextDisposition.TRANSLATE


def protected_occurrences(text: str) -> list[ProtectedOccurrence]:
    candidates: list[tuple[int, int, int, str]] = []
    for priority, pattern in enumerate(
        (
            URL_PATTERN,
            TIMECODE_PATTERN,
            SPEAKER_PATTERN,
            EXPLICIT_PROPER_PATTERN,
            STRONG_PRODUCT_PATTERN,
            NUMBER_PATTERN,
        )
    ):
        candidates.extend(
            (match.start(), match.end(), priority, match.group(0))
            for match in pattern.finditer(text)
        )
    selected: list[ProtectedOccurrence] = []
    for start, end, _priority, value in sorted(
        candidates,
        key=lambda item: (item[0], item[2], -(item[1] - item[0])),
    ):
        if any(start < existing.end and end > existing.start for existing in selected):
            continue
        selected.append(ProtectedOccurrence(start, end, value))
    return sorted(selected, key=lambda item: item.start)


def protected_tokens(text: str) -> list[str]:
    return [occurrence.value for occurrence in protected_occurrences(text)]


def protect_texts(
    texts: dict[int, str],
) -> tuple[dict[int, str], dict[int, list[PlaceholderToken]]]:
    next_token_id = 1
    protected_texts: dict[int, str] = {}
    manifests: dict[int, list[PlaceholderToken]] = {}
    for segment_id, text in texts.items():
        cursor = 0
        parts: list[str] = []
        tokens: list[PlaceholderToken] = []
        for occurrence in protected_occurrences(text):
            placeholder = f"LVT_TOKEN_{next_token_id:04d}"
            next_token_id += 1
            parts.extend((text[cursor : occurrence.start], placeholder))
            tokens.append(PlaceholderToken(placeholder, occurrence.value))
            cursor = occurrence.end
        parts.append(text[cursor:])
        protected_texts[segment_id] = "".join(parts)
        manifests[segment_id] = tokens
    return protected_texts, manifests


def restore_protected_text(text: str, tokens: list[PlaceholderToken]) -> str:
    expected = [token.placeholder for token in tokens]
    actual_matches = list(PLACEHOLDER_PATTERN.finditer(text))
    actual = [match.group(0) for match in actual_matches]
    if actual != expected:
        raise ValueError(f"placeholder sequence mismatch: expected={expected}, actual={actual}")
    without_valid_placeholders = PLACEHOLDER_PATTERN.sub("", text)
    if "LVT_TOKEN_" in without_valid_placeholders:
        raise ValueError("placeholder is malformed or modified")
    for match in actual_matches:
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if (before and re.match(r"[A-Za-z0-9_]", before)) or (
            after and re.match(r"[A-Za-z0-9_]", after)
        ):
            raise ValueError(f"placeholder boundary changed: {match.group(0)}")
    restored = text
    for token in tokens:
        restored = restored.replace(token.placeholder, token.original, 1)
    return restored


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
