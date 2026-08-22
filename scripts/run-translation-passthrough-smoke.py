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
        16: "https://example.com.",
        17: "(https://example.com)",
        18: "NASA.",
        19: "2026.",
        20: "Speaker 1:",
        21: "2026-2027",
        22: "计划为2026-2027年",
        23: "Visit https://example.com.",
        24: "NASA launched in 2026.",
        25: "Visit https://example.com,then continue",
        26: "Visit https://example.com。Continue",
        27: "访问https://example.com。继续",
        28: "https://example.com。继续",
        29: "Release on 2026-08-22",
        30: "Call 010-1234-5678 today",
        31: "计划为2026-08-22发布",
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
    filtering = FilteringTranslationEngine(observed)
    batch_ids = (range(1, 16), range(16, 32))
    batch_results = [
        filtering.translate(
            {segment_id: source[segment_id] for segment_id in ids},
            "en",
        )
        for ids in batch_ids
    ]
    translated = {
        segment_id: batch_result.texts[segment_id]
        for batch_result in batch_results
        for segment_id in batch_result.texts
    }

    sent_ids = [segment_id for model_batch in observed.calls for segment_id in model_batch]
    expected_sent_ids = [
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        14,
        15,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
    ]
    assert sent_ids == expected_sent_ids
    assert list(translated) == list(source)
    assert len(translated) == len(source)
    for segment_id in (1, 2, 3, 4, 5, 13, 16, 17, 18, 19, 20, 21):
        assert translated[segment_id] == source[segment_id]
    protected_ids = (7, 8, 10, 11, 12, 14, 15, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31)
    for segment_id in protected_ids:
        assert protected_tokens(translated[segment_id]) == protected_tokens(source[segment_id])
    assert translated[7].count("NASA") == 2
    assert translated[8].count("https://example.com") == 2
    assert "12026" not in translated[7]
    assert "2027" not in translated[11] + translated[12]
    assert "2026-2027" in translated[22]
    assert "2026-08-22" in translated[29] + translated[31]
    assert "010-1234-5678" in translated[30]
    assert "LVT_TOKEN_0001" in translated[14]
    assert "https://example.com/LVT_TOKEN_0002" in translated[15]

    protected_source: dict[int, str] = {}
    manifests_by_id = {}
    model_batches = []
    for batch_index, sent_source in enumerate(observed.calls, start=1):
        batch_protected, batch_manifests = protect_texts(
            sent_source,
            nonce_factory=nonce_factory,
        )
        protected_source.update(batch_protected)
        manifests_by_id.update(batch_manifests)
        model_batches.append(
            {
                "batch_index": batch_index,
                "sent_ids": list(sent_source),
                "protected_source": batch_protected,
                "placeholder_manifest": {
                    str(segment_id): [token.placeholder for token in tokens]
                    for segment_id, tokens in batch_manifests.items()
                },
                "engine_version": batch_results[batch_index - 1].engine_version,
                "warnings": batch_results[batch_index - 1].warnings,
            }
        )
    for segment_id, trailing_body in {
        25: ",then continue",
        26: "。Continue",
        27: "。继续",
        28: "。继续",
    }.items():
        assert "https://example.com" not in protected_source[segment_id]
        assert trailing_body in protected_source[segment_id]

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
        "model_batches": model_batches,
        "sent_ids": sent_ids,
        "passthrough_ids": [segment_id for segment_id in source if segment_id not in sent_ids],
        "atomic_punctuation_passthrough_ids": [16, 17, 18, 19, 20],
        "numeric_range_passthrough_ids": [21],
        "mixed_text_sent_ids": [22, 23, 24],
        "unspaced_url_body_sent_ids": [25, 26, 27, 28],
        "multi_part_numeric_sent_ids": [29, 30, 31],
        "protected_source_sent_to_model": protected_source,
        "placeholder_sequences": {
            str(segment_id): [token.placeholder for token in tokens]
            for segment_id, tokens in manifests_by_id.items()
        },
        "source": source,
        "translated": translated,
        "protected_tokens": {
            str(segment_id): {
                "source": protected_tokens(source[segment_id]),
                "translated": protected_tokens(translated[segment_id]),
            }
            for segment_id in source
        },
        "engine_versions": [result.engine_version for result in batch_results],
        "warnings": [warning for result in batch_results for warning in result.warnings],
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
