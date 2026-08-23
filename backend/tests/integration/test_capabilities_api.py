from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lvt.api.app import create_app
from lvt.core.capabilities import (
    CapabilitiesProvider,
    CapabilityProbeResult,
    CapabilityProbes,
    CapabilityStatus,
    OllamaProbeResult,
)

ASR_MODEL = "organization/asr-model"
PRIMARY_MODEL = "primary:model"
FALLBACK_MODEL = "fallback:model"


class RecordingProvider:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    def get_capabilities(self) -> dict[str, Any]:
        self.calls += 1
        return self.response


class FailingProvider:
    def get_capabilities(self) -> dict[str, Any]:
        raise RuntimeError(
            "token=secret-value path=/Users/private/cache command=ollama pull secret-model"
        )


def test_http_response_never_reflects_alphanumeric_api_token_metadata(
    tmp_path: Path,
) -> None:
    api_token = "LVTSecretToken123"
    malicious = CapabilityProbeResult(
        CapabilityStatus.AVAILABLE,
        version=api_token,
        model=api_token,
    )
    provider = CapabilitiesProvider(
        probes=CapabilityProbes(
            ffmpeg=lambda _timeout: malicious,
            ollama=lambda _timeout: OllamaProbeResult(
                capability=malicious,
                models=frozenset({PRIMARY_MODEL, FALLBACK_MODEL}),
            ),
            asr_package=lambda _timeout: malicious,
            asr_model=lambda _timeout: malicious,
            diarization=lambda _timeout: malicious,
        ),
        asr_model=ASR_MODEL,
        primary_translation_model=PRIMARY_MODEL,
        fallback_translation_model=FALLBACK_MODEL,
    )
    app = create_app(
        db_path=tmp_path / "lvt.sqlite3",
        api_token=api_token,
        capabilities_provider=provider,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={"X-LVT-Token": api_token},
        )

    body = response.json()
    serialized = json.dumps(body, ensure_ascii=False)
    assert response.status_code == 200
    assert api_token not in serialized
    assert all("version" not in body[name] for name in _component_names())
    assert body["asr_model"]["model"] == ASR_MODEL
    assert body["translation_primary"]["model"] == PRIMARY_MODEL
    assert body["translation_fallback"]["model"] == FALLBACK_MODEL


def test_capabilities_requires_valid_token_before_calling_provider(tmp_path: Path) -> None:
    expected = {
        "checked_at": "2026-08-23T10:00:00+00:00",
        "ttl_seconds": 5,
        "ffmpeg": {
            "status": "available",
            "checked_at": "2026-08-23T10:00:00+00:00",
        },
    }
    provider = RecordingProvider(expected)
    app = create_app(
        db_path=tmp_path / "lvt.sqlite3",
        api_token="test-token",
        capabilities_provider=provider,
    )

    with TestClient(app) as client:
        missing = client.get("/api/v1/capabilities")
        wrong = client.get(
            "/api/v1/capabilities",
            headers={"X-LVT-Token": "wrong-token"},
        )
        valid = client.get(
            "/api/v1/capabilities",
            headers={"X-LVT-Token": "test-token"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert provider.calls == 1
    assert valid.status_code == 200
    assert valid.json() == expected


def test_capabilities_provider_failure_is_stable_and_sanitized(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "lvt.sqlite3",
        api_token="test-token",
        capabilities_provider=FailingProvider(),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={"X-LVT-Token": "test-token"},
        )

    serialized = json.dumps(response.json(), ensure_ascii=False)
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error_code": "CAPABILITIES_UNAVAILABLE",
            "message": "本地能力探测暂时不可用",
        }
    }
    assert "secret-value" not in serialized
    assert "/Users/private" not in serialized
    assert "ollama pull" not in serialized


def test_capabilities_without_provider_returns_stable_503(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "lvt.sqlite3", api_token="test-token")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/capabilities",
            headers={"X-LVT-Token": "test-token"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error_code": "CAPABILITIES_UNAVAILABLE",
            "message": "本地能力探测暂时不可用",
        }
    }


def test_production_main_injects_dynamic_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LVT_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("LVT_TOKEN", "test-token")
    sys.modules.pop("lvt.main", None)

    try:
        main_module = importlib.import_module("lvt.main")
        assert isinstance(main_module.app.state.capabilities_provider, CapabilitiesProvider)
        assert main_module.app.state.capabilities_provider is main_module.capabilities_provider
    finally:
        sys.modules.pop("lvt.main", None)


def _component_names() -> tuple[str, ...]:
    return (
        "ffmpeg",
        "ollama",
        "asr_package",
        "asr_model",
        "diarization",
        "translation_primary",
        "translation_fallback",
    )
