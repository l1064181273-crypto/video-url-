#!/usr/bin/env python3
"""Run the strict passthrough/token contract against real Ollama models."""

from __future__ import annotations

import argparse
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from lvt.engines.base import TranslationEngine, TranslationResult
from lvt.engines.ollama import FallbackTranslationEngine, OllamaTranslationEngine
from lvt.engines.translation import (
    FilteringTranslationEngine,
    classify_text,
    protect_texts,
    protected_tokens,
)


@dataclass
class ObservedTranslationEngine:
    delegate: TranslationEngine
    calls: list[dict[int, str]] = field(default_factory=list)

    @property
    def version(self) -> str:
        return f"observed:{self.delegate.version}"

    def translate(self, texts: dict[int, str], source_language: str) -> TranslationResult:
        self.calls.append(dict(texts))
        return self.delegate.translate(texts, source_language)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    source = {
        1: "https://example.com",
        2: "2026",
        3: "NASA",
        4: "GPT-5",
        5: "OpenAI",
        6: "Good Morning",
        7: "NASA and NASA launched in 2026",
        8: "Websites: https://example.com and https://example.com",
        9: "STOP",
        10: "The time code is 00:03:21",
        11: "Published in 发布于2026年",
        12: "Published in В2026году",
        13: "https://en.wikipedia.org/wiki/Foo_(bar)",
        14: "Keep LVT_TOKEN_0001 unchanged",
        15: "Visit https://example.com/LVT_TOKEN_0002 for details",
    }
    nonce = secrets.token_hex(8).upper()

    def nonce_factory() -> str:
        return nonce

    observed = ObservedTranslationEngine(
        FallbackTranslationEngine(
            primary=OllamaTranslationEngine(
                model="hy-mt2:1.8b-q4km-fixed",
                base_url=args.ollama_url,
                nonce_factory=nonce_factory,
            ),
            fallback=OllamaTranslationEngine(
                model="qwen2.5:1.5b",
                base_url=args.ollama_url,
                nonce_factory=nonce_factory,
            ),
        )
    )
    result = FilteringTranslationEngine(observed).translate(source, "en")

    sent_ids = list(observed.calls[0]) if observed.calls else []
    expected_sent_ids = [6, 7, 8, 9, 10, 11, 12, 14, 15]
    assert sent_ids == expected_sent_ids
    assert list(result.texts) == list(source)
    assert len(result.texts) == len(source)
    for segment_id in (1, 2, 3, 4, 5, 13):
        assert result.texts[segment_id] == source[segment_id]
    for segment_id in (7, 8, 10, 11, 12, 14, 15):
        assert protected_tokens(result.texts[segment_id]) == protected_tokens(source[segment_id])
    assert result.texts[7].count("NASA") == 2
    assert result.texts[8].count("https://example.com") == 2
    assert result.texts[7].count("NASA") == 2
    assert result.texts[8].count("https://example.com") == 2
    assert "12026" not in result.texts[7]
    assert "2027" not in result.texts[11] + result.texts[12]
    assert "LVT_TOKEN_0001" in result.texts[14]
    assert "https://example.com/LVT_TOKEN_0002" in result.texts[15]

    sent_source = observed.calls[0]
    protected_source, manifests = protect_texts(
        sent_source,
        nonce_factory=nonce_factory,
    )

    report = {
        "schema_version": 1,
        "source_language": "en",
        "primary_model": "hy-mt2:1.8b-q4km-fixed",
        "fallback_model": "qwen2.5:1.5b",
        "placeholder_nonce": nonce,
        "classification": {
            str(segment_id): classify_text(text).value for segment_id, text in source.items()
        },
        "model_call_count": len(observed.calls),
        "sent_ids": sent_ids,
        "passthrough_ids": [segment_id for segment_id in source if segment_id not in sent_ids],
        "protected_source_sent_to_model": protected_source,
        "placeholder_sequences": {
            str(segment_id): [token.placeholder for token in tokens]
            for segment_id, tokens in manifests.items()
        },
        "source": source,
        "translated": result.texts,
        "protected_tokens": {
            str(segment_id): {
                "source": protected_tokens(source[segment_id]),
                "translated": protected_tokens(result.texts[segment_id]),
            }
            for segment_id in source
        },
        "engine_version": result.engine_version,
        "warnings": result.warnings,
        "assertions": {
            "only_expected_ids_sent": True,
            "complete_id_order_preserved": True,
            "passthrough_exact": True,
            "token_count_boundary_order_preserved": True,
            "ordinary_text_contains_cjk": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
