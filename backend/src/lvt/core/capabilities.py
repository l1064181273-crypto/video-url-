from __future__ import annotations

import copy
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

MAX_OLLAMA_RESPONSE_BYTES = 1_000_000


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class CapabilityProbeResult:
    status: CapabilityStatus
    version: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class OllamaProbeResult:
    capability: CapabilityProbeResult
    models: frozenset[str] = frozenset()


CapabilityProbe = Callable[[float], CapabilityProbeResult]
OllamaProbe = Callable[[float], OllamaProbeResult]


@dataclass(frozen=True)
class CapabilityProbes:
    ffmpeg: CapabilityProbe
    ollama: OllamaProbe
    asr_package: CapabilityProbe
    asr_model: CapabilityProbe
    diarization: CapabilityProbe


class CapabilitiesSource(Protocol):
    def get_capabilities(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocalCapabilitiesConfig:
    asr_model: str
    segmentation_model: Path
    embedding_model: Path
    ollama_url: str
    primary_translation_model: str
    fallback_translation_model: str
    model_cache_root: Path
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    strict_ffmpeg: bool = False


JsonRequester = Callable[[str, float], Mapping[str, Any]]
PackageVersion = Callable[[str], str]
ExecutableLookup = Callable[[str], str | None]
CommandRunner = Callable[[list[str], float], tuple[int, str]]


class LocalCapabilityProbes:
    def __init__(
        self,
        config: LocalCapabilitiesConfig,
        *,
        package_version: PackageVersion = metadata.version,
        which: ExecutableLookup = shutil.which,
        run_command: CommandRunner | None = None,
        request_json: JsonRequester | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._package_version = package_version
        self._which = which
        self._run_command = run_command or _run_command
        self._request_json = request_json or _request_local_json
        self._monotonic = monotonic

    def as_probes(self) -> CapabilityProbes:
        return CapabilityProbes(
            ffmpeg=self.ffmpeg,
            ollama=self.ollama,
            asr_package=self.asr_package,
            asr_model=self.asr_model,
            diarization=self.diarization,
        )

    def ffmpeg(self, timeout: float) -> CapabilityProbeResult:
        try:
            if self.config.strict_ffmpeg:
                ffmpeg_path = self.config.ffmpeg_path
                ffprobe_path = self.config.ffprobe_path
                if (
                    ffmpeg_path is None
                    or ffprobe_path is None
                    or ffmpeg_path.is_symlink()
                    or ffprobe_path.is_symlink()
                    or not ffmpeg_path.is_file()
                    or not ffprobe_path.is_file()
                ):
                    return CapabilityProbeResult(CapabilityStatus.MISSING)
                ffmpeg_command = str(ffmpeg_path)
            else:
                discovered_ffmpeg = self._which("ffmpeg")
                discovered_ffprobe = self._which("ffprobe")
                if discovered_ffmpeg is None or discovered_ffprobe is None:
                    return CapabilityProbeResult(CapabilityStatus.MISSING)
                ffmpeg_command = discovered_ffmpeg
            return_code, output = self._run_command([ffmpeg_command, "-version"], timeout)
            if return_code != 0:
                return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
            version = _extract_ffmpeg_version(output)
            return CapabilityProbeResult(CapabilityStatus.AVAILABLE, version=version)
        except Exception:
            return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)

    def ollama(self, timeout: float) -> OllamaProbeResult:
        started = self._monotonic()
        try:
            base_url = _validate_ollama_url(self.config.ollama_url)
            version_response = self._request_json(
                f"{base_url}/api/version",
                _remaining_timeout(started, timeout, self._monotonic),
            )
            tags_response = self._request_json(
                f"{base_url}/api/tags",
                _remaining_timeout(started, timeout, self._monotonic),
            )
            models = _ollama_model_names(tags_response)
            version_value = version_response.get("version")
            version = version_value if isinstance(version_value, str) else None
            return OllamaProbeResult(
                capability=CapabilityProbeResult(
                    CapabilityStatus.AVAILABLE,
                    version=version,
                ),
                models=frozenset(models),
            )
        except Exception:
            return OllamaProbeResult(capability=CapabilityProbeResult(CapabilityStatus.UNAVAILABLE))

    def asr_package(self, _timeout: float) -> CapabilityProbeResult:
        try:
            version = self._package_version("mlx-whisper")
        except metadata.PackageNotFoundError:
            return CapabilityProbeResult(CapabilityStatus.MISSING)
        except Exception:
            return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
        return CapabilityProbeResult(CapabilityStatus.AVAILABLE, version=version)

    def asr_model(self, _timeout: float) -> CapabilityProbeResult:
        try:
            model_root = _hugging_face_model_root(
                self.config.model_cache_root,
                self.config.asr_model,
            )
            available = _mlx_whisper_snapshot_available(model_root / "snapshots")
        except Exception:
            return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
        return CapabilityProbeResult(
            CapabilityStatus.AVAILABLE if available else CapabilityStatus.MISSING,
            model=self.config.asr_model,
        )

    def diarization(self, _timeout: float) -> CapabilityProbeResult:
        try:
            version = self._package_version("sherpa-onnx")
        except metadata.PackageNotFoundError:
            return CapabilityProbeResult(CapabilityStatus.MISSING)
        except Exception:
            return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
        try:
            models_available = (
                self.config.segmentation_model.is_file() and self.config.embedding_model.is_file()
            )
        except OSError:
            return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
        if not models_available:
            return CapabilityProbeResult(CapabilityStatus.MISSING)
        return CapabilityProbeResult(CapabilityStatus.AVAILABLE, version=version)


class CapabilitiesProvider:
    def __init__(
        self,
        *,
        probes: CapabilityProbes,
        asr_model: str,
        primary_translation_model: str,
        fallback_translation_model: str,
        ttl_seconds: float = 5,
        probe_timeout: float = 1,
        overall_timeout: float = 2,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds != 5:
            raise ValueError("capabilities TTL must be 5 seconds")
        if not 0 < probe_timeout <= 1:
            raise ValueError("capability probe timeout must be at most 1 second")
        if not 0 < overall_timeout <= 2:
            raise ValueError("capability overall timeout must be at most 2 seconds")
        self._probes = probes
        self._asr_model = asr_model
        self._primary_translation_model = primary_translation_model
        self._fallback_translation_model = fallback_translation_model
        self._ttl_seconds = ttl_seconds
        self._probe_timeout = probe_timeout
        self._overall_timeout = overall_timeout
        self._monotonic = monotonic
        self._timeout_monotonic = timeout_monotonic
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._condition = threading.Condition()
        self._snapshot: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._refreshing = False
        self._active_probes_lock = threading.Lock()
        self._active_probes: set[str] = set()

    def get_capabilities(self) -> dict[str, Any]:
        with self._condition:
            now = self._monotonic()
            if self._snapshot is not None and now < self._expires_at:
                return copy.deepcopy(self._snapshot)
            if self._refreshing:
                while self._refreshing:
                    self._condition.wait()
                if self._snapshot is not None:
                    return copy.deepcopy(self._snapshot)
            self._refreshing = True

        try:
            snapshot = self._refresh()
        except Exception:
            snapshot = self._failed_snapshot()

        with self._condition:
            self._snapshot = snapshot
            self._expires_at = self._monotonic() + self._ttl_seconds
            self._refreshing = False
            self._condition.notify_all()
            return copy.deepcopy(snapshot)

    def _refresh(self) -> dict[str, Any]:
        probe_results = self._run_probes()
        checked_at = _checked_at(self._utcnow)
        ffmpeg = _coerce_probe_result(probe_results["ffmpeg"])
        ollama = _coerce_ollama_result(probe_results["ollama"])
        asr_package = _coerce_probe_result(probe_results["asr_package"])
        asr_model = _coerce_probe_result(probe_results["asr_model"])
        diarization = _coerce_probe_result(probe_results["diarization"])

        if asr_package.status is not CapabilityStatus.AVAILABLE:
            asr_model = CapabilityProbeResult(
                CapabilityStatus.UNCHECKED,
                model=self._asr_model,
            )
        if ollama.capability.status is CapabilityStatus.AVAILABLE:
            primary = CapabilityProbeResult(
                (
                    CapabilityStatus.AVAILABLE
                    if self._primary_translation_model in ollama.models
                    else CapabilityStatus.MISSING
                ),
                model=self._primary_translation_model,
            )
            fallback = CapabilityProbeResult(
                (
                    CapabilityStatus.AVAILABLE
                    if self._fallback_translation_model in ollama.models
                    else CapabilityStatus.MISSING
                ),
                model=self._fallback_translation_model,
            )
        else:
            primary = CapabilityProbeResult(
                CapabilityStatus.UNCHECKED,
                model=self._primary_translation_model,
            )
            fallback = CapabilityProbeResult(
                CapabilityStatus.UNCHECKED,
                model=self._fallback_translation_model,
            )

        return self._snapshot_from_components(
            checked_at,
            ffmpeg=ffmpeg,
            ollama=ollama.capability,
            asr_package=asr_package,
            asr_model=asr_model,
            diarization=diarization,
            translation_primary=primary,
            translation_fallback=fallback,
        )

    def _run_probes(self) -> dict[str, object]:
        calls: dict[str, Callable[[float], object]] = {
            "ffmpeg": self._probes.ffmpeg,
            "ollama": self._probes.ollama,
            "asr_package": self._probes.asr_package,
            "asr_model": self._probes.asr_model,
            "diarization": self._probes.diarization,
        }
        results: dict[str, object] = {}
        results_condition = threading.Condition()
        started = self._timeout_monotonic()

        def run(name: str, probe: Callable[[float], object]) -> None:
            try:
                try:
                    result = probe(self._probe_timeout)
                except Exception:
                    result = _unavailable_for(name)
                with results_condition:
                    results[name] = result
                    results_condition.notify_all()
            finally:
                with self._active_probes_lock:
                    self._active_probes.discard(name)

        for name, probe in calls.items():
            with self._active_probes_lock:
                if name in self._active_probes:
                    with results_condition:
                        results[name] = _unavailable_for(name)
                    continue
                self._active_probes.add(name)
            thread = threading.Thread(
                target=run,
                args=(name, probe),
                name=f"lvt-capability-{name}",
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError:
                with self._active_probes_lock:
                    self._active_probes.discard(name)
                with results_condition:
                    results[name] = _unavailable_for(name)

        deadline = started + min(self._probe_timeout, self._overall_timeout)
        with results_condition:
            while len(results) < len(calls):
                remaining = deadline - self._timeout_monotonic()
                if remaining <= 0:
                    break
                results_condition.wait(remaining)
            for name in calls:
                results.setdefault(name, _unavailable_for(name))
            return dict(results)

    def _failed_snapshot(self) -> dict[str, Any]:
        checked_at = _checked_at(self._utcnow)
        unavailable = CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
        unchecked_primary = CapabilityProbeResult(
            CapabilityStatus.UNCHECKED,
            model=self._primary_translation_model,
        )
        unchecked_fallback = CapabilityProbeResult(
            CapabilityStatus.UNCHECKED,
            model=self._fallback_translation_model,
        )
        return self._snapshot_from_components(
            checked_at,
            ffmpeg=unavailable,
            ollama=unavailable,
            asr_package=unavailable,
            asr_model=unavailable,
            diarization=unavailable,
            translation_primary=unchecked_primary,
            translation_fallback=unchecked_fallback,
        )

    def _snapshot_from_components(
        self,
        checked_at: str,
        **components: CapabilityProbeResult,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "checked_at": checked_at,
            "ttl_seconds": _public_duration(self._ttl_seconds),
        }
        configured_models = {
            "asr_model": self._asr_model,
            "translation_primary": self._primary_translation_model,
            "translation_fallback": self._fallback_translation_model,
        }
        for name, component in components.items():
            snapshot[name] = _component_payload(
                component,
                checked_at,
                model=configured_models.get(name),
            )
        return snapshot


def default_model_cache_root() -> Path:
    explicit_cache = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit_cache:
        return Path(explicit_cache).expanduser()
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _run_command(command: list[str], timeout: float) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout


def _request_local_json(url: str, timeout: float) -> Mapping[str, Any]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama probe URL must be local HTTP")
    connection = http.client.HTTPConnection(
        parsed.hostname,
        parsed.port or 80,
        timeout=timeout,
    )
    path = parsed.path or "/"
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Ollama probe failed")
        body = response.read(MAX_OLLAMA_RESPONSE_BYTES + 1)
        if len(body) > MAX_OLLAMA_RESPONSE_BYTES:
            raise RuntimeError("Ollama probe response exceeded limit")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError("Ollama probe response must be an object")
        return payload
    finally:
        connection.close()


def _validate_ollama_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Ollama URL must be a local HTTP origin")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{parsed.port or 80}"


def _remaining_timeout(
    started: float,
    timeout: float,
    monotonic: Callable[[], float],
) -> float:
    remaining = timeout - (monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("capability probe timed out")
    return remaining


def _ollama_model_names(payload: Mapping[str, Any]) -> set[str]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise TypeError("Ollama tags response is invalid")
    models: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise TypeError("Ollama model entry is invalid")
        name = raw_model.get("name", raw_model.get("model"))
        if not isinstance(name, str):
            raise TypeError("Ollama model name is invalid")
        models.add(name)
    return models


def _hugging_face_model_root(cache_root: Path, model: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", model):
        raise ValueError("ASR model identifier is invalid")
    owner, name = model.split("/", 1)
    return cache_root / f"models--{owner}--{name}"


def _mlx_whisper_snapshot_available(snapshots: Path) -> bool:
    if not snapshots.is_dir():
        return False
    return any(
        snapshot.is_dir()
        and (snapshot / "config.json").is_file()
        and (snapshot / "weights.npz").is_file()
        for snapshot in snapshots.iterdir()
    )


def _extract_ffmpeg_version(output: str) -> str | None:
    first_line = output.splitlines()[0] if output else ""
    match = re.match(r"ffmpeg version ([A-Za-z0-9._+-]+)", first_line)
    return match.group(1) if match else None


def _coerce_probe_result(value: object) -> CapabilityProbeResult:
    if isinstance(value, CapabilityProbeResult) and isinstance(value.status, CapabilityStatus):
        return value
    return CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)


def _coerce_ollama_result(value: object) -> OllamaProbeResult:
    if (
        isinstance(value, OllamaProbeResult)
        and isinstance(value.capability, CapabilityProbeResult)
        and isinstance(value.capability.status, CapabilityStatus)
        and isinstance(value.models, frozenset)
        and all(isinstance(model, str) for model in value.models)
    ):
        return value
    return OllamaProbeResult(CapabilityProbeResult(CapabilityStatus.UNAVAILABLE))


def _unavailable_for(name: str) -> object:
    unavailable = CapabilityProbeResult(CapabilityStatus.UNAVAILABLE)
    return OllamaProbeResult(unavailable) if name == "ollama" else unavailable


def _checked_at(utcnow: Callable[[], datetime]) -> str:
    try:
        value = utcnow()
        if value.tzinfo is None:
            raise ValueError("capability clock must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    except Exception:
        return datetime.now(UTC).isoformat()


def _component_payload(
    component: CapabilityProbeResult,
    checked_at: str,
    *,
    model: str | None,
) -> dict[str, str]:
    payload = {"status": component.status.value, "checked_at": checked_at}
    if model is not None:
        payload["model"] = model
    return payload


def _public_duration(value: float) -> int | float:
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric
