from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest

from lvt.core import config
from lvt.core.config import Settings
from lvt.core.platform_runtime import (
    PlatformConfigurationError,
    RuntimePlatform,
    UnsupportedPlatformError,
    default_data_root,
    executable_name,
    resolve_runtime_platform,
)


@pytest.mark.parametrize(
    ("system", "expected"),
    [("darwin", RuntimePlatform.MACOS), ("win32", RuntimePlatform.WINDOWS)],
)
def test_resolve_runtime_platform_accepts_supported_systems(
    system: str, expected: RuntimePlatform
) -> None:
    assert resolve_runtime_platform(system) is expected


def test_resolve_runtime_platform_rejects_unsupported_system() -> None:
    with pytest.raises(UnsupportedPlatformError, match="unsupported operating system"):
        resolve_runtime_platform("linux")


def test_default_data_root_preserves_macos_location(tmp_path: Path) -> None:
    home = tmp_path / "用户 Home"

    assert default_data_root(system="darwin", environment={}, home=home) == (
        home / "Library" / "Application Support" / "LocalVideoTranscriber"
    )


def test_default_data_root_uses_windows_local_app_data() -> None:
    root = default_data_root(
        system="win32",
        environment={"LOCALAPPDATA": r"C:\Users\Example User\AppData\Local"},
    )
    assert PureWindowsPath(os.fspath(root)) == PureWindowsPath(
        r"C:\Users\Example User\AppData\Local\LocalVideoTranscriber"
    )


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (RuntimePlatform.MACOS, "ffmpeg"),
        (RuntimePlatform.WINDOWS, "ffmpeg.exe"),
    ],
)
def test_executable_name_uses_platform_suffix(platform: RuntimePlatform, expected: str) -> None:
    assert executable_name("ffmpeg", platform) == expected


@pytest.mark.parametrize("name", ["", ".", "..", "../ffmpeg", r"bin\ffmpeg"])
def test_executable_name_rejects_path_components(name: str) -> None:
    with pytest.raises(PlatformConfigurationError, match="executable name"):
        executable_name(name, RuntimePlatform.WINDOWS)


@pytest.mark.parametrize(
    "local_app_data",
    [None, "", "relative\\AppData\\Local", r"\\server\share\AppData\Local"],
)
def test_default_data_root_rejects_unsafe_windows_local_app_data(
    local_app_data: str | None,
) -> None:
    environment = {} if local_app_data is None else {"LOCALAPPDATA": local_app_data}

    with pytest.raises(PlatformConfigurationError, match="LOCALAPPDATA"):
        default_data_root(system="win32", environment=environment)


def test_explicit_data_root_does_not_probe_platform_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LVT_DATA_ROOT", str(tmp_path))

    def unexpected_default() -> Path:
        raise AssertionError("platform default must not be evaluated")

    monkeypatch.setattr(config, "default_data_root", unexpected_default)

    assert Settings.from_env().data_root == tmp_path
