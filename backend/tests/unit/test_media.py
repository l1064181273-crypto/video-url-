from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from lvt.core.errors import LVTError
from lvt.core.platform_runtime import RuntimePlatform
from lvt.engines import media


def _write_executable(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return hashlib.sha256(content).hexdigest()


def _installed_ffmpeg(tmp_path: Path) -> tuple[Path, Path, Path]:
    app_root = tmp_path / "app"
    ffmpeg_dir = app_root / "tools" / "ffmpeg" / "8.0" / "bin"
    ffmpeg_sha256 = _write_executable(ffmpeg_dir / "ffmpeg", b"verified ffmpeg")
    ffprobe_sha256 = _write_executable(ffmpeg_dir / "ffprobe", b"verified ffprobe")
    install_state = tmp_path / "runtime" / "install-state.json"
    install_state.parent.mkdir(parents=True)
    install_state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ffmpeg": {
                    "version": "8.0",
                    "directory": "tools/ffmpeg/8.0/bin",
                    "sha256": {
                        "ffmpeg": ffmpeg_sha256,
                        "ffprobe": ffprobe_sha256,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return app_root, ffmpeg_dir, install_state


def _discover_installed(app_root: Path, ffmpeg_dir: Path, install_state: Path) -> tuple[Path, Path]:
    return media.discover_ffmpeg_binaries(
        installed_mode=True,
        ffmpeg_dir=ffmpeg_dir,
        app_root=app_root,
        install_state=install_state,
        runtime_platform=RuntimePlatform.MACOS,
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="asserts POSIX executable mode bits and binary names",
)
def test_installed_ffmpeg_accepts_app_owned_digest_matched_binaries(tmp_path: Path) -> None:
    app_root, ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)

    assert _discover_installed(app_root, ffmpeg_dir, install_state) == (
        ffmpeg_dir / "ffmpeg",
        ffmpeg_dir / "ffprobe",
    )


def test_installed_ffmpeg_uses_windows_executable_names(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    ffmpeg_dir = app_root / "tools" / "ffmpeg" / "8.0" / "bin"
    ffmpeg_sha256 = _write_executable(ffmpeg_dir / "ffmpeg.exe", b"verified ffmpeg")
    ffprobe_sha256 = _write_executable(ffmpeg_dir / "ffprobe.exe", b"verified ffprobe")
    install_state = tmp_path / "runtime" / "install-state.json"
    install_state.parent.mkdir(parents=True)
    install_state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ffmpeg": {
                    "version": "8.0",
                    "directory": "tools/ffmpeg/8.0/bin",
                    "sha256": {
                        "ffmpeg": ffmpeg_sha256,
                        "ffprobe": ffprobe_sha256,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert media.discover_ffmpeg_binaries(
        installed_mode=True,
        ffmpeg_dir=ffmpeg_dir,
        app_root=app_root,
        install_state=install_state,
        runtime_platform=RuntimePlatform.WINDOWS,
    ) == (ffmpeg_dir / "ffmpeg.exe", ffmpeg_dir / "ffprobe.exe")


@pytest.mark.parametrize("filename", ["ffmpeg", "ffprobe"])
def test_installed_ffmpeg_rejects_missing_binary(tmp_path: Path, filename: str) -> None:
    app_root, ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)
    (ffmpeg_dir / filename).unlink()

    with pytest.raises(LVTError, match="FFMPEG_NOT_FOUND"):
        _discover_installed(app_root, ffmpeg_dir, install_state)


def test_installed_ffmpeg_rejects_digest_mismatch(tmp_path: Path) -> None:
    app_root, ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)
    (ffmpeg_dir / "ffmpeg").write_bytes(b"tampered")

    with pytest.raises(LVTError, match="FFMPEG_NOT_FOUND"):
        _discover_installed(app_root, ffmpeg_dir, install_state)


def test_installed_ffmpeg_rejects_symlink(tmp_path: Path) -> None:
    app_root, ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)
    external = tmp_path / "external-ffmpeg"
    _write_executable(external, b"verified ffmpeg")
    (ffmpeg_dir / "ffmpeg").unlink()
    (ffmpeg_dir / "ffmpeg").symlink_to(external)

    with pytest.raises(LVTError, match="FFMPEG_NOT_FOUND"):
        _discover_installed(app_root, ffmpeg_dir, install_state)


def test_installed_ffmpeg_rejects_directory_outside_app_root(tmp_path: Path) -> None:
    app_root, _ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)
    outside = tmp_path / "outside"
    _write_executable(outside / "ffmpeg", b"verified ffmpeg")
    _write_executable(outside / "ffprobe", b"verified ffprobe")

    with pytest.raises(LVTError, match="FFMPEG_NOT_FOUND"):
        _discover_installed(app_root, outside, install_state)


def test_installed_ffmpeg_rejects_install_state_outside_data_root(tmp_path: Path) -> None:
    app_root, ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)
    external_state = tmp_path / "external" / "install-state.json"
    external_state.parent.mkdir()
    external_state.write_bytes(install_state.read_bytes())

    with pytest.raises(LVTError, match="FFMPEG_NOT_FOUND"):
        _discover_installed(app_root, ffmpeg_dir, external_state)


def test_installed_ffmpeg_never_uses_path_or_static_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app_root, ffmpeg_dir, install_state = _installed_ffmpeg(tmp_path)
    (ffmpeg_dir / "ffmpeg").unlink()
    calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> str:
        calls.append("fallback")
        raise AssertionError("installed mode attempted a fallback")

    monkeypatch.setattr(media.shutil, "which", forbidden)
    monkeypatch.setattr("static_ffmpeg.add_paths", forbidden)

    with pytest.raises(LVTError, match="FFMPEG_NOT_FOUND"):
        _discover_installed(app_root, ffmpeg_dir, install_state)
    assert calls == []


def test_development_ffmpeg_discovery_keeps_existing_path_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = {
        "ffmpeg": "/development/bin/ffmpeg",
        "ffprobe": "/development/bin/ffprobe",
    }
    monkeypatch.setattr(media.shutil, "which", paths.get)

    assert media.discover_ffmpeg_binaries() == (
        Path(paths["ffmpeg"]),
        Path(paths["ffprobe"]),
    )
