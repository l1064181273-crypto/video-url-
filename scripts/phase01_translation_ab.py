#!/usr/bin/env python3
"""Reproducible Phase 0.1 translation A/B benchmark for Ollama models."""

from __future__ import annotations

import argparse
import copy
import json
import re
import statistics
import time
import urllib.request
from typing import Any


def segment(
    segment_id: int,
    start_ms: int,
    end_ms: int,
    speaker: str,
    source_language: str,
    source_text: str,
) -> dict[str, Any]:
    return {
        "id": segment_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "speaker": speaker,
        "source_language": source_language,
        "source_text": source_text,
        "translated_text": "",
    }


TEST_CASES = {
    "en": [
        segment(
            1,
            0,
            3200,
            "Speaker 1",
            "en",
            "Local processing keeps your audio and transcript private.",
        ),
        segment(
            2,
            3200,
            6900,
            "Speaker 2",
            "en",
            "The application can recover an interrupted job from its last safe stage.",
        ),
    ],
    "ru": [
        segment(
            1,
            0,
            3900,
            "Speaker 1",
            "ru",
            "Система автоматически определяет язык и создаёт точные субтитры.",
        ),
        segment(
            2,
            3900,
            7600,
            "Speaker 2",
            "ru",
            "После перезапуска обработка продолжается с последнего безопасного этапа.",
        ),
    ],
    "sw": [
        segment(
            1,
            0,
            3900,
            "Speaker 1",
            "sw",
            "Mfumo huu unafanya kazi kwenye kompyuta yako bila kutuma data mtandaoni.",
        ),
        segment(
            2,
            3900,
            7800,
            "Speaker 2",
            "sw",
            "Baada ya hitilafu, kazi inaweza kuendelea kutoka hatua salama ya mwisho.",
        ),
    ],
}

LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "sw": "Swahili"}

USER_PROMPT = """### Task
Translate the user-facing {source_language} text values within the following
JSON object into Simplified Chinese.

### Strict Rules
1. Structure Preservation: return ONLY one valid JSON object with the exact same keys.
2. Selective Translation: translate ONLY the values into natural Simplified Chinese.
3. Strict Non-Translation: never alter, add, remove, merge, reorder, or renumber keys.
4. Values must contain translated text only: no timestamps, speaker labels, notes, or source text.

### Source Data
{source_data}"""


def request_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.load(response)


def invariant_snapshot(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "speaker": item["speaker"],
            "source_language": item["source_language"],
            "source_text": item["source_text"],
        }
        for item in segments
    ]


def parse_mapping(content: str, source_map: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    expected_ids = set(source_map)
    try:
        mapping = json.loads(content)
    except json.JSONDecodeError as exc:
        return {}, [f"invalid_json: {exc}"]
    if not isinstance(mapping, dict):
        return {}, ["response_not_object"]
    actual_ids = set(mapping)
    if actual_ids != expected_ids:
        errors.append(
            f"id_set_mismatch: expected={sorted(expected_ids)} actual={sorted(actual_ids)}"
        )
    for key, value in mapping.items():
        if not isinstance(value, str) or not value.strip():
            errors.append(f"invalid_translation_value: id={key}")
            continue
        if key in source_map and value.strip() == source_map[key].strip():
            errors.append(f"untranslated_source_text: id={key}")
        if not re.search(r"[\u3400-\u9fff]", value):
            errors.append(f"translation_has_no_cjk: id={key}")
    return mapping, errors


def benchmark_case(
    base_url: str,
    model: str,
    language: str,
    segments: list[dict[str, Any]],
    runs: int,
) -> dict[str, Any]:
    source_map = {str(item["id"]): item["source_text"] for item in segments}
    run_results: list[dict[str, Any]] = []

    for run_index in range(runs):
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": USER_PROMPT.format(
                        source_language=LANGUAGE_NAMES[language],
                        source_data=json.dumps(source_map, ensure_ascii=False),
                    ),
                },
            ],
            "stream": False,
            "format": "json",
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "top_p": 0.6,
                "top_k": 20,
                "repeat_penalty": 1.05,
                "seed": 42,
                "num_ctx": 4096,
            },
        }
        started = time.perf_counter()
        response = request_json(f"{base_url}/api/chat", payload)
        wall_seconds = time.perf_counter() - started
        content = response.get("message", {}).get("content", "")
        mapping, errors = parse_mapping(content, source_map)

        result_segments = copy.deepcopy(segments)
        before = invariant_snapshot(result_segments)
        for segment in result_segments:
            segment["translated_text"] = mapping.get(str(segment["id"]), "")
        if invariant_snapshot(result_segments) != before:
            errors.append("segment_metadata_or_source_text_changed")

        eval_count = int(response.get("eval_count", 0))
        eval_duration = int(response.get("eval_duration", 0))
        tokens_per_second = (
            eval_count / (eval_duration / 1_000_000_000) if eval_count and eval_duration else 0.0
        )
        run_results.append(
            {
                "run": run_index + 1,
                "wall_seconds": round(wall_seconds, 3),
                "total_seconds": round(response.get("total_duration", 0) / 1_000_000_000, 3),
                "load_seconds": round(response.get("load_duration", 0) / 1_000_000_000, 3),
                "eval_count": eval_count,
                "tokens_per_second": round(tokens_per_second, 2),
                "mapping": mapping,
                "errors": errors,
                "passed": not errors,
            }
        )

    warm_runs = run_results[1:] if len(run_results) > 1 else run_results
    return {
        "source_language": language,
        "all_runs_passed": all(item["passed"] for item in run_results),
        "median_warm_wall_seconds": round(
            statistics.median(item["wall_seconds"] for item in warm_runs), 3
        ),
        "median_warm_tokens_per_second": round(
            statistics.median(item["tokens_per_second"] for item in warm_runs), 2
        ),
        "runs": run_results,
    }


def benchmark_model(base_url: str, model: str, runs: int) -> dict[str, Any]:
    cases = [
        benchmark_case(base_url, model, language, segments, runs)
        for language, segments in TEST_CASES.items()
    ]
    process_data = request_json(f"{base_url}/api/ps")
    running_model = next(
        (item for item in process_data.get("models", []) if item.get("name") == model),
        process_data.get("models", [{}])[0] if process_data.get("models") else {},
    )
    all_warm_runs = [
        run
        for case in cases
        for run in (case["runs"][1:] if len(case["runs"]) > 1 else case["runs"])
    ]
    return {
        "model": model,
        "all_runs_passed": all(case["all_runs_passed"] for case in cases),
        "median_warm_wall_seconds": round(
            statistics.median(item["wall_seconds"] for item in all_warm_runs), 3
        ),
        "median_warm_tokens_per_second": round(
            statistics.median(item["tokens_per_second"] for item in all_warm_runs), 2
        ),
        "memory": {
            "size_bytes": running_model.get("size", 0),
            "size_vram_bytes": running_model.get("size_vram", 0),
            "context_length": running_model.get("context_length", 0),
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", required=True)
    parser.add_argument("models", nargs="+")
    args = parser.parse_args()

    report = {
        "schema_version": 1,
        "test_inputs": TEST_CASES,
        "models": [
            benchmark_model(args.base_url.rstrip("/"), model, args.runs) for model in args.models
        ],
    }
    with open(args.output, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
