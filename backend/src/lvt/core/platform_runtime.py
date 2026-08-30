from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path, PureWindowsPath


class UnsupportedPlatformError(RuntimeError):
    pass


class PlatformConfigurationError(ValueError):
    pass


class RuntimePlatform(StrEnum):
    MACOS = "macos"
    WINDOWS = "windows"


def default_asr_model(platform: RuntimePlatform | None = None) -> str:
    selected = resolve_runtime_platform() if platform is None else platform
    if selected is RuntimePlatform.MACOS:
        return "mlx-community/whisper-small-mlx"
    return "Systran/faster-whisper-small"


def resolve_runtime_platform(system: str | None = None) -> RuntimePlatform:
    observed = sys.platform if system is None else system
    if observed == "darwin":
        return RuntimePlatform.MACOS
    if observed == "win32":
        return RuntimePlatform.WINDOWS
    raise UnsupportedPlatformError(f"unsupported operating system: {observed}")


def executable_name(name: str, platform: RuntimePlatform | None = None) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise PlatformConfigurationError("executable name must be one path component")
    selected = resolve_runtime_platform() if platform is None else platform
    if selected is RuntimePlatform.WINDOWS and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def default_data_root(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    runtime = resolve_runtime_platform(system)
    if runtime is RuntimePlatform.MACOS:
        home_root = Path.home() if home is None else home
        if not home_root.is_absolute():
            raise PlatformConfigurationError("macOS home directory must be absolute")
        return home_root / "Library" / "Application Support" / "LocalVideoTranscriber"

    values = os.environ if environment is None else environment
    local_app_data = values.get("LOCALAPPDATA")
    if not local_app_data or "\x00" in local_app_data:
        raise PlatformConfigurationError("Windows LOCALAPPDATA is unavailable")
    windows_root = PureWindowsPath(local_app_data)
    if not windows_root.is_absolute() or windows_root.anchor.startswith("\\\\"):
        raise PlatformConfigurationError("Windows LOCALAPPDATA must be a local absolute path")
    return Path(str(windows_root / "LocalVideoTranscriber"))
