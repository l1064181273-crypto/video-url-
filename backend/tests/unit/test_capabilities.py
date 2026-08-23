from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest

from lvt.core.capabilities import (
    CapabilitiesProvider,
    CapabilityProbeResult,
    CapabilityProbes,
    CapabilityStatus,
    LocalCapabilitiesConfig,
    LocalCapabilityProbes,
    OllamaProbeResult,
)

PRIMARY_MODEL = "primary:model"
FALLBACK_MODEL = "fallback:model"
ASR_MODEL = "organization/asr-model"


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.base = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.base + timedelta(seconds=self.seconds)

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


def _result(
    status: CapabilityStatus = CapabilityStatus.AVAILABLE,
    *,
    version: str | None = None,
    model: str | None = None,
) -> CapabilityProbeResult:
    return CapabilityProbeResult(status=status, version=version, model=model)


def _ollama(
    status: CapabilityStatus = CapabilityStatus.AVAILABLE,
    *,
    models: frozenset[str] = frozenset({PRIMARY_MODEL, FALLBACK_MODEL}),
) -> OllamaProbeResult:
    return OllamaProbeResult(
        capability=_result(status, version="0.32.15"),
        models=models if status is CapabilityStatus.AVAILABLE else frozenset(),
    )


def _probes(
    *,
    ffmpeg: Callable[[float], CapabilityProbeResult] | None = None,
    ollama: Callable[[float], OllamaProbeResult] | None = None,
    asr_package: Callable[[float], CapabilityProbeResult] | None = None,
    asr_model: Callable[[float], CapabilityProbeResult] | None = None,
    diarization: Callable[[float], CapabilityProbeResult] | None = None,
) -> CapabilityProbes:
    return CapabilityProbes(
        ffmpeg=ffmpeg or (lambda _timeout: _result(version="7.0")),
        ollama=ollama or (lambda _timeout: _ollama()),
        asr_package=asr_package or (lambda _timeout: _result(version="0.4.3")),
        asr_model=asr_model or (lambda _timeout: _result(model=ASR_MODEL)),
        diarization=diarization or (lambda _timeout: _result(version="1.13.6")),
    )


def _provider(
    probes: CapabilityProbes | None = None,
    *,
    clock: FakeClock | None = None,
    ttl_seconds: float = 5,
    probe_timeout: float = 1,
    overall_timeout: float = 2,
) -> CapabilitiesProvider:
    monotonic = clock.monotonic if clock is not None else time.monotonic
    utcnow = clock.now if clock is not None else lambda: datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    return CapabilitiesProvider(
        probes=probes or _probes(),
        asr_model=ASR_MODEL,
        primary_translation_model=PRIMARY_MODEL,
        fallback_translation_model=FALLBACK_MODEL,
        ttl_seconds=ttl_seconds,
        probe_timeout=probe_timeout,
        overall_timeout=overall_timeout,
        monotonic=monotonic,
        utcnow=utcnow,
    )


def test_capability_status_values_are_closed() -> None:
    assert {status.value for status in CapabilityStatus} == {
        "available",
        "missing",
        "unavailable",
        "unchecked",
    }


def test_default_ttl_timeout_and_checked_at_contract() -> None:
    seen_timeouts: list[float] = []

    def record(result: Any) -> Callable[[float], Any]:
        def probe(timeout: float) -> Any:
            seen_timeouts.append(timeout)
            return result

        return probe

    snapshot = _provider(
        _probes(
            ffmpeg=record(_result()),
            ollama=record(_ollama()),
            asr_package=record(_result()),
            asr_model=record(_result()),
            diarization=record(_result()),
        )
    ).get_capabilities()

    assert snapshot["ttl_seconds"] == 5
    assert seen_timeouts == [1, 1, 1, 1, 1]
    for name in (
        "ffmpeg",
        "ollama",
        "asr_package",
        "asr_model",
        "diarization",
        "translation_primary",
        "translation_fallback",
    ):
        assert snapshot[name]["checked_at"] == snapshot["checked_at"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ttl_seconds": 4.999}, "TTL must be 5 seconds"),
        ({"probe_timeout": 1.001}, "probe timeout must be at most 1 second"),
        ({"overall_timeout": 2.001}, "overall timeout must be at most 2 seconds"),
    ],
)
def test_provider_rejects_relaxed_ttl_or_timeout_contract(
    overrides: dict[str, float],
    message: str,
) -> None:
    arguments: dict[str, Any] = {
        "probes": _probes(),
        "asr_model": ASR_MODEL,
        "primary_translation_model": PRIMARY_MODEL,
        "fallback_translation_model": FALLBACK_MODEL,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        CapabilitiesProvider(**arguments)


def test_ollama_down_then_up_refreshes_only_after_ttl() -> None:
    clock = FakeClock()
    current = {"value": _ollama(CapabilityStatus.UNAVAILABLE)}
    calls = 0

    def probe(_timeout: float) -> OllamaProbeResult:
        nonlocal calls
        calls += 1
        return current["value"]

    provider = _provider(_probes(ollama=probe), clock=clock)
    down = provider.get_capabilities()
    assert down["ollama"]["status"] == "unavailable"
    assert down["translation_primary"]["status"] == "unchecked"
    assert down["translation_fallback"]["status"] == "unchecked"
    assert calls == 1

    current["value"] = _ollama()
    assert provider.get_capabilities() == down
    assert calls == 1

    clock.advance(5)
    up = provider.get_capabilities()
    assert up["ollama"]["status"] == "available"
    assert up["translation_primary"]["status"] == "available"
    assert up["translation_fallback"]["status"] == "available"
    assert up["checked_at"] != down["checked_at"]
    assert calls == 2


@pytest.mark.parametrize(
    ("models", "primary_status", "fallback_status"),
    [
        (frozenset(), "missing", "missing"),
        (frozenset({PRIMARY_MODEL}), "available", "missing"),
        (frozenset({FALLBACK_MODEL}), "missing", "available"),
        (frozenset({PRIMARY_MODEL, FALLBACK_MODEL}), "available", "available"),
    ],
)
def test_translation_models_are_reported_independently(
    models: frozenset[str],
    primary_status: str,
    fallback_status: str,
) -> None:
    snapshot = _provider(_probes(ollama=lambda _timeout: _ollama(models=models))).get_capabilities()

    assert snapshot["translation_primary"]["status"] == primary_status
    assert snapshot["translation_fallback"]["status"] == fallback_status


@pytest.mark.parametrize(
    ("package_status", "model_status", "expected_model_status"),
    [
        (CapabilityStatus.MISSING, CapabilityStatus.MISSING, "unchecked"),
        (CapabilityStatus.AVAILABLE, CapabilityStatus.MISSING, "missing"),
        (CapabilityStatus.AVAILABLE, CapabilityStatus.AVAILABLE, "available"),
    ],
)
def test_asr_package_and_model_are_reported_separately(
    package_status: CapabilityStatus,
    model_status: CapabilityStatus,
    expected_model_status: str,
) -> None:
    snapshot = _provider(
        _probes(
            asr_package=lambda _timeout: _result(package_status),
            asr_model=lambda _timeout: _result(model_status, model=ASR_MODEL),
        )
    ).get_capabilities()

    assert snapshot["asr_package"]["status"] == package_status.value
    assert snapshot["asr_model"]["status"] == expected_model_status


def test_local_asr_probe_distinguishes_package_and_model(
    tmp_path: Path,
) -> None:
    config = LocalCapabilitiesConfig(
        asr_model=ASR_MODEL,
        segmentation_model=tmp_path / "segmentation.onnx",
        embedding_model=tmp_path / "embedding.onnx",
        ollama_url="http://127.0.0.1:11434",
        primary_translation_model=PRIMARY_MODEL,
        fallback_translation_model=FALLBACK_MODEL,
        model_cache_root=tmp_path / "cache",
    )
    package_missing = LocalCapabilityProbes(
        config,
        package_version=lambda _name: (_ for _ in ()).throw(PackageNotFoundError()),
    )
    assert package_missing.asr_package(1).status is CapabilityStatus.MISSING

    package_available = LocalCapabilityProbes(
        config,
        package_version=lambda _name: "0.4.3",
    )
    assert package_available.asr_model(1).status is CapabilityStatus.MISSING

    snapshot = config.model_cache_root / "models--organization--asr-model" / "snapshots" / "one"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "weights.npz").touch()
    assert package_available.asr_model(1).status is CapabilityStatus.AVAILABLE


def test_local_ollama_probe_distinguishes_down_and_up(tmp_path: Path) -> None:
    online = False

    def request(url: str, _timeout: float) -> dict[str, Any]:
        if not online:
            raise ConnectionRefusedError("controlled down")
        if url.endswith("/api/version"):
            return {"version": "0.32.15"}
        return {"models": [{"name": PRIMARY_MODEL}, {"name": FALLBACK_MODEL}]}

    config = LocalCapabilitiesConfig(
        asr_model=ASR_MODEL,
        segmentation_model=tmp_path / "segmentation.onnx",
        embedding_model=tmp_path / "embedding.onnx",
        ollama_url="http://127.0.0.1:11434",
        primary_translation_model=PRIMARY_MODEL,
        fallback_translation_model=FALLBACK_MODEL,
        model_cache_root=tmp_path / "cache",
    )
    probes = LocalCapabilityProbes(config, request_json=request)

    assert probes.ollama(1).capability.status is CapabilityStatus.UNAVAILABLE

    online = True
    available = probes.ollama(1)
    assert available.capability.status is CapabilityStatus.AVAILABLE
    assert available.capability.version == "0.32.15"
    assert available.models == frozenset({PRIMARY_MODEL, FALLBACK_MODEL})


def test_local_diarization_probe_requires_package_and_every_model_file(
    tmp_path: Path,
) -> None:
    segmentation = tmp_path / "segmentation.onnx"
    embedding = tmp_path / "embedding.onnx"
    config = LocalCapabilitiesConfig(
        asr_model=ASR_MODEL,
        segmentation_model=segmentation,
        embedding_model=embedding,
        ollama_url="http://127.0.0.1:11434",
        primary_translation_model=PRIMARY_MODEL,
        fallback_translation_model=FALLBACK_MODEL,
        model_cache_root=tmp_path / "cache",
    )
    probes = LocalCapabilityProbes(config, package_version=lambda _name: "1.13.6")

    embedding.touch()
    assert probes.diarization(1).status is CapabilityStatus.MISSING

    embedding.unlink()
    segmentation.touch()
    assert probes.diarization(1).status is CapabilityStatus.MISSING

    embedding.touch()
    capability = probes.diarization(1)
    assert capability.status is CapabilityStatus.AVAILABLE
    assert capability.version == "1.13.6"


def test_ttl_cache_does_not_repeat_probes() -> None:
    clock = FakeClock()
    calls = {name: 0 for name in ("ffmpeg", "ollama", "asr_package", "asr_model", "diarization")}

    def counted(name: str, value: Any) -> Callable[[float], Any]:
        def probe(_timeout: float) -> Any:
            calls[name] += 1
            return value

        return probe

    provider = _provider(
        _probes(
            ffmpeg=counted("ffmpeg", _result()),
            ollama=counted("ollama", _ollama()),
            asr_package=counted("asr_package", _result()),
            asr_model=counted("asr_model", _result()),
            diarization=counted("diarization", _result()),
        ),
        clock=clock,
    )

    first = provider.get_capabilities()
    assert provider.get_capabilities() == first
    clock.advance(4.999)
    assert provider.get_capabilities() == first
    assert set(calls.values()) == {1}


def test_ttl_expiry_concurrent_callers_share_one_refresh() -> None:
    clock = FakeClock()
    refresh_entered = threading.Event()
    refresh_release = threading.Event()
    calls = {name: 0 for name in ("ffmpeg", "ollama", "asr_package", "asr_model", "diarization")}
    calls_lock = threading.Lock()

    def counted(name: str, value: Any) -> Callable[[float], Any]:
        def probe(_timeout: float) -> Any:
            with calls_lock:
                calls[name] += 1
                current_call = calls[name]
            if name == "ffmpeg" and current_call == 2:
                refresh_entered.set()
                assert refresh_release.wait(timeout=2)
            return value

        return probe

    provider = _provider(
        _probes(
            ffmpeg=counted("ffmpeg", _result(version="7.0")),
            ollama=counted("ollama", _ollama()),
            asr_package=counted("asr_package", _result()),
            asr_model=counted("asr_model", _result()),
            diarization=counted("diarization", _result()),
        ),
        clock=clock,
    )
    provider.get_capabilities()
    clock.advance(5)
    caller_barrier = threading.Barrier(11)
    snapshots: list[dict[str, Any]] = []

    def read() -> None:
        caller_barrier.wait()
        snapshots.append(provider.get_capabilities())

    readers = [threading.Thread(target=read) for _ in range(10)]
    for reader in readers:
        reader.start()
    caller_barrier.wait()
    assert refresh_entered.wait(timeout=2)
    refresh_release.set()
    for reader in readers:
        reader.join(timeout=2)
        assert not reader.is_alive()

    assert set(calls.values()) == {2}
    assert len(snapshots) == 10
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_single_probe_timeout_does_not_block_other_results() -> None:
    release = threading.Event()

    def blocked(_timeout: float) -> CapabilityProbeResult:
        release.wait(timeout=2)
        return _result()

    provider = _provider(
        _probes(ffmpeg=blocked),
        probe_timeout=0.05,
        overall_timeout=0.2,
    )
    started = time.monotonic()
    snapshot = provider.get_capabilities()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.2
    assert snapshot["ffmpeg"]["status"] == "unavailable"
    assert snapshot["ollama"]["status"] == "available"
    assert snapshot["asr_package"]["status"] == "available"
    assert snapshot["diarization"]["status"] == "available"


def test_timed_out_probe_is_not_duplicated_across_ttl_refreshes() -> None:
    clock = FakeClock()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def blocked(_timeout: float) -> CapabilityProbeResult:
        nonlocal calls
        calls += 1
        release.wait(timeout=2)
        finished.set()
        return _result()

    provider = _provider(
        _probes(ffmpeg=blocked),
        clock=clock,
        probe_timeout=0.05,
        overall_timeout=0.2,
    )
    assert provider.get_capabilities()["ffmpeg"]["status"] == "unavailable"

    clock.advance(5)
    assert provider.get_capabilities()["ffmpeg"]["status"] == "unavailable"
    assert calls == 1

    release.set()
    assert finished.wait(timeout=2)


def test_overall_timeout_bounds_all_blocked_probes() -> None:
    release = threading.Event()

    def blocked(_timeout: float) -> CapabilityProbeResult:
        release.wait(timeout=2)
        return _result()

    def blocked_ollama(_timeout: float) -> OllamaProbeResult:
        release.wait(timeout=2)
        return _ollama()

    provider = _provider(
        _probes(
            ffmpeg=blocked,
            ollama=blocked_ollama,
            asr_package=blocked,
            asr_model=blocked,
            diarization=blocked,
        ),
        probe_timeout=1,
        overall_timeout=0.05,
    )
    started = time.monotonic()
    snapshot = provider.get_capabilities()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.2
    assert snapshot["ffmpeg"]["status"] == "unavailable"
    assert snapshot["ollama"]["status"] == "unavailable"
    assert snapshot["translation_primary"]["status"] == "unchecked"
    assert snapshot["translation_fallback"]["status"] == "unchecked"


def test_probe_exceptions_are_recursively_sanitized(tmp_path: Path) -> None:
    secret = f"token=secret-value path={tmp_path} command=ollama pull model"

    def fail(_timeout: float) -> CapabilityProbeResult:
        raise RuntimeError(secret)

    snapshot = _provider(_probes(ffmpeg=fail)).get_capabilities()
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["ffmpeg"]["status"] == "unavailable"
    assert "secret-value" not in serialized
    assert str(tmp_path) not in serialized
    assert "ollama pull" not in serialized
    assert "traceback" not in serialized.lower()


def test_successful_probe_metadata_is_recursively_sanitized(tmp_path: Path) -> None:
    secret = f"/Users/private/{tmp_path.name}/token=secret-value"
    snapshot = _provider(
        _probes(
            ffmpeg=lambda _timeout: _result(version=secret),
            asr_model=lambda _timeout: _result(model=secret),
        )
    ).get_capabilities()
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["ffmpeg"] == {
        "status": "available",
        "checked_at": snapshot["checked_at"],
    }
    assert snapshot["asr_model"] == {
        "status": "available",
        "checked_at": snapshot["checked_at"],
    }
    assert "secret-value" not in serialized
    assert "/Users/private" not in serialized


def test_local_probes_never_call_download_or_engine_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("download, installation, or engine construction was attempted")

    monkeypatch.setattr("static_ffmpeg.add_paths", forbidden)
    monkeypatch.setattr("huggingface_hub.hf_hub_download", forbidden)
    monkeypatch.setattr("huggingface_hub.snapshot_download", forbidden)
    monkeypatch.setattr("mlx_whisper.transcribe", forbidden)
    monkeypatch.setattr("lvt.engines.mlx_whisper.MLXWhisperASREngine.__init__", forbidden)
    monkeypatch.setattr(
        "lvt.engines.sherpa_diarization.SherpaOnnxDiarizationEngine.__init__",
        forbidden,
    )
    monkeypatch.setattr("lvt.engines.ollama.OllamaTranslationEngine.__init__", forbidden)

    config = LocalCapabilitiesConfig(
        asr_model=ASR_MODEL,
        segmentation_model=tmp_path / "missing-segmentation.onnx",
        embedding_model=tmp_path / "missing-embedding.onnx",
        ollama_url="http://127.0.0.1:11434",
        primary_translation_model=PRIMARY_MODEL,
        fallback_translation_model=FALLBACK_MODEL,
        model_cache_root=tmp_path / "cache",
    )
    local = LocalCapabilityProbes(
        config,
        package_version=lambda _name: "1.0",
        which=lambda _name: None,
        request_json=lambda _url, _timeout: {"version": "0.1", "models": []},
    )
    provider = _provider(local.as_probes())

    snapshot = provider.get_capabilities()
    assert snapshot["ffmpeg"]["status"] == "missing"
    assert snapshot["asr_model"]["status"] == "missing"
    assert snapshot["diarization"]["status"] == "missing"
    assert not config.model_cache_root.exists()
