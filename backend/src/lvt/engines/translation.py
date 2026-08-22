from __future__ import annotations

import re
import secrets
from collections.abc import Callable
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


URL_START_PATTERN = re.compile(r"https?://", re.I)
TIMECODE_PATTERN = re.compile(r"(?<!\d)(?:\d{1,2}:)?[0-5]\d:[0-5]\d(?:[.,]\d{1,3})?(?!\d)")
NUMERIC_SEQUENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[+-]?\d+(?:[.,]\d+)*"
    r"(?:[ \t]*[-–—/][ \t]*[+-]?\d+(?:[.,]\d+)*)+"
    r"(?![A-Za-z0-9_-])"
)
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])[+-]?\d+(?:[.,]\d+)*(?![A-Za-z0-9_-])")
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
RESERVED_PREFIX_PATTERN = re.compile(r"LVT_[A-Za-z0-9_]+")
NONCE_PATTERN = re.compile(r"[A-Z0-9]{4,64}")
URL_STOP_CHARACTERS = frozenset(" \t\r\n`<>{}[]\"'“”‘’")
URL_INLINE_BOUNDARY_CHARACTERS = frozenset(",，。！？；：")
URL_TRAILING_PUNCTUATION = frozenset(".,;:!?，。！？；：")
ATOMIC_PUNCTUATION = frozenset(".,:;?!，。；：？！")
ATOMIC_BRACKET_PAIRS = {
    "(": ")",
    "[": "]",
    "（": "）",
    "［": "］",
}
ATOMIC_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "“": "”",
    "‘": "’",
}
NonceFactory = Callable[[], str]


@dataclass(frozen=True)
class ProtectedOccurrence:
    start: int
    end: int
    value: str


@dataclass(frozen=True)
class PlaceholderToken:
    placeholder: str
    original: str


def _classify_token_value(value: str) -> TextDisposition:
    url_occurrences = _url_occurrences(value)
    if (
        len(url_occurrences) == 1
        and url_occurrences[0].start == 0
        and url_occurrences[0].end == len(value)
    ):
        return TextDisposition.URL
    if TIMECODE_PATTERN.fullmatch(value):
        return TextDisposition.TIMECODE
    if NUMERIC_SEQUENCE_PATTERN.fullmatch(value) or FULL_NUMBER_PATTERN.fullmatch(value):
        return TextDisposition.NUMBER
    if SPEAKER_PATTERN.fullmatch(value):
        return TextDisposition.SPEAKER_LABEL
    if value in EXPLICIT_PROPER_TOKENS or STRONG_PRODUCT_PATTERN.fullmatch(value):
        return TextDisposition.PROPER_TOKEN
    return TextDisposition.TRANSLATE


def _has_only_valid_atomic_wrapping(
    text: str,
    occurrence: ProtectedOccurrence,
) -> bool:
    prefix = text[: occurrence.start]
    suffix = text[occurrence.end :]
    allowed = (
        ATOMIC_PUNCTUATION
        | frozenset(" \t\r\n")
        | frozenset(ATOMIC_BRACKET_PAIRS)
        | frozenset(ATOMIC_BRACKET_PAIRS.values())
        | frozenset(ATOMIC_QUOTE_PAIRS)
        | frozenset(ATOMIC_QUOTE_PAIRS.values())
    )
    if any(character not in allowed for character in prefix + suffix):
        return False
    opening_to_closing = ATOMIC_BRACKET_PAIRS | ATOMIC_QUOTE_PAIRS
    closing_characters = frozenset(opening_to_closing.values())
    ignored = ATOMIC_PUNCTUATION | frozenset(" \t\r\n")
    stack: list[str] = []
    for character in prefix:
        if character in ignored:
            continue
        if character not in opening_to_closing:
            return False
        stack.append(opening_to_closing[character])
    for character in suffix:
        if character in ignored:
            continue
        if character not in closing_characters or not stack or stack.pop() != character:
            return False
    return not stack


def classify_text(text: str) -> TextDisposition:
    stripped = text.strip()
    direct = _classify_token_value(stripped)
    if direct is not TextDisposition.TRANSLATE:
        return direct
    occurrences = protected_occurrences(stripped)
    if len(occurrences) != 1:
        return TextDisposition.TRANSLATE
    occurrence = occurrences[0]
    disposition = _classify_token_value(occurrence.value)
    if disposition is TextDisposition.TRANSLATE:
        return disposition
    if _has_only_valid_atomic_wrapping(stripped, occurrence):
        return disposition
    return TextDisposition.TRANSLATE


def _url_occurrences(text: str) -> list[ProtectedOccurrence]:
    occurrences: list[ProtectedOccurrence] = []
    for match in URL_START_PATTERN.finditer(text):
        end = match.end()
        while (
            end < len(text)
            and text[end] not in URL_STOP_CHARACTERS
            and text[end] not in URL_INLINE_BOUNDARY_CHARACTERS
        ):
            end += 1
        candidate = text[match.start() : end]
        while candidate:
            if candidate[-1] in URL_TRAILING_PUNCTUATION:
                candidate = candidate[:-1]
                end -= 1
                continue
            if candidate[-1] == ")" and candidate.count(")") > candidate.count("("):
                candidate = candidate[:-1]
                end -= 1
                continue
            break
        if candidate:
            occurrences.append(ProtectedOccurrence(match.start(), end, candidate))
    return occurrences


def protected_occurrences(text: str) -> list[ProtectedOccurrence]:
    candidates: list[tuple[int, int, int, str]] = [
        (occurrence.start, occurrence.end, 0, occurrence.value)
        for occurrence in _url_occurrences(text)
    ]
    for priority, pattern in enumerate(
        (
            NUMERIC_SEQUENCE_PATTERN,
            TIMECODE_PATTERN,
            SPEAKER_PATTERN,
            RESERVED_PREFIX_PATTERN,
            EXPLICIT_PROPER_PATTERN,
            STRONG_PRODUCT_PATTERN,
            NUMBER_PATTERN,
        ),
        start=1,
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


def _random_nonce() -> str:
    return secrets.token_hex(8).upper()


def choose_placeholder_nonce(
    texts: dict[int, str],
    nonce_factory: NonceFactory = _random_nonce,
) -> str:
    for _attempt in range(100):
        nonce = nonce_factory().strip().upper()
        if not NONCE_PATTERN.fullmatch(nonce):
            raise ValueError("placeholder nonce must contain 4-64 ASCII letters or digits")
        namespace = f"LVT_{nonce}_TOKEN_"
        if all(namespace not in text for text in texts.values()):
            return nonce
    raise ValueError("unable to generate a collision-free placeholder nonce")


def protect_texts(
    texts: dict[int, str],
    *,
    nonce_factory: NonceFactory = _random_nonce,
) -> tuple[dict[int, str], dict[int, list[PlaceholderToken]]]:
    nonce = choose_placeholder_nonce(texts, nonce_factory)
    next_token_id = 1
    protected_texts: dict[int, str] = {}
    manifests: dict[int, list[PlaceholderToken]] = {}
    for segment_id, text in texts.items():
        cursor = 0
        parts: list[str] = []
        tokens: list[PlaceholderToken] = []
        for occurrence in protected_occurrences(text):
            placeholder = f"LVT_{nonce}_TOKEN_{next_token_id:04d}"
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
    actual_matches = list(RESERVED_PREFIX_PATTERN.finditer(text))
    actual = [match.group(0) for match in actual_matches]
    if actual != expected:
        raise ValueError(f"placeholder sequence mismatch: expected={expected}, actual={actual}")
    namespace = ""
    if tokens:
        namespace = tokens[0].placeholder.rsplit("_", 1)[0] + "_"
    for match in actual_matches:
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if (before and re.match(r"[A-Za-z0-9_]", before)) or (
            after and re.match(r"[A-Za-z0-9_]", after)
        ):
            raise ValueError(f"placeholder boundary changed: {match.group(0)}")
    replacements = {token.placeholder: token.original for token in tokens}
    restored = RESERVED_PREFIX_PATTERN.sub(
        lambda match: replacements[match.group(0)],
        text,
    )
    if namespace and namespace in restored:
        raise ValueError("internal placeholder remained after restoration")
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
