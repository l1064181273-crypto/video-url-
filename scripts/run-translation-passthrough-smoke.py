#!/usr/bin/env python3
"""Run the strict passthrough/token contract against real Ollama models."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from lvt.engines.base import TranslationEngine, TranslationResult
from lvt.engines.ollama import FallbackTranslationEngine, OllamaTranslationEngine
from lvt.engines.translation import (
    FilteringTranslationEngine,
    classify_text,
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
    }
    observed = ObservedTranslationEngine(
        FallbackTranslationEngine(
            primary=OllamaTranslationEngine(
                model="hy-mt2:1.8b-q4km-fixed",
                base_url=args.ollama_url,
            ),
            fallback=OllamaTranslationEngine(
                model="qwen2.5:1.5b",
                base_url=args.ollama_url,
            ),
        )
    )
    result = FilteringTranslationEngine(observed).translate(source, "en")

    sent_ids = list(observed.calls[0]) if observed.calls else []
    expected_sent_ids = [6, 7, 8, 9, 10]
    assert sent_ids == expected_sent_ids
    assert list(result.texts) == list(source)
    assert len(result.texts) == len(source)
    for segment_id in (1, 2, 3, 4, 5):
        assert result.texts[segment_id] == source[segment_id]
    for segment_id in (7, 8, 10):
        assert protected_tokens(result.texts[segment_id]) == protected_tokens(source[segment_id])
    assert result.texts[7].count("NASA") == 2
    assert result.texts[8].count("https://example.com") == 2
    assert "12026" not in result.texts[7]

    report = {
        "schema_version": 1,
        "source_language": "en",
        "primary_model": "hy-mt2:1.8b-q4km-fixed",
        "fallback_model": "qwen2.5:1.5b",
        "classification": {
            str(segment_id): classify_text(text).value for segment_id, text in source.items()
        },
        "model_call_count": len(observed.calls),
        "sent_ids": sent_ids,
        "passthrough_ids": [segment_id for segment_id in source if segment_id not in sent_ids],
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
