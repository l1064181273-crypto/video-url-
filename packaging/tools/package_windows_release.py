#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from runtime_layout import path_is_link_like

ROOT = Path(__file__).resolve().parents[2]
FIXED_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
WINDOWS_TOOLS = (
    "install.py",
    "lifecycle_lock.py",
    "provision.py",
    "runtime_layout.py",
    "transaction_journal.py",
    "verify_install.py",
    "windows_job.py",
    "windows_lifecycle.py",
    "windows_process.py",
    "windows_publication.py",
    "windows_publish_install.py",
    "windows_service.py",
    "windows_supervisor.py",
)
WINDOWS_SCRIPTS = (
    "install.ps1",
    "start.ps1",
    "stop.ps1",
    "doctor.ps1",
    "lib/WindowsCommon.psm1",
)
CRLF_SUFFIXES = {".cmd", ".ps1", ".psm1"}
SKIP_NAMES = {".DS_Store", "__pycache__"}
SECRET_MARKERS = (
    b"CheckpointOneRuntimeToken",
    b"Recursive422SecretToken",
    b"ReplacementInputSecret",
    b"LVT_TEST_SECRET_",
    b"BEGIN PRIVATE KEY",
    b"BEGIN RSA PRIVATE KEY",
)


class WindowsPackageError(RuntimeError):
    pass


def _manifest() -> dict[str, object]:
    path = ROOT / "packaging/release-manifest.windows-x64.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsPackageError("Windows release manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise WindowsPackageError("Windows release manifest is invalid")
    return payload


def _source_mappings() -> list[tuple[Path, Path]]:
    mappings = [
        (ROOT / "VERSION", Path("VERSION")),
        (ROOT / "LICENSE", Path("LICENSE")),
        (ROOT / "THIRD_PARTY_NOTICES.md", Path("THIRD_PARTY_NOTICES.md")),
        (ROOT / "docs/WINDOWS_README.md", Path("README.md")),
        (ROOT / "docs/WINDOWS_QUICK_START.txt", Path("新手使用说明.txt")),
        (
            ROOT / "启动 Local Video Transcriber.cmd",
            Path("启动 Local Video Transcriber.cmd"),
        ),
        (ROOT / "backend/pyproject.toml", Path("backend/pyproject.toml")),
        (ROOT / "backend/uv.lock", Path("backend/uv.lock")),
        (ROOT / "backend/src", Path("backend/src")),
        (ROOT / "extension/dist", Path("extension")),
        (
            ROOT / "packaging/dependencies.windows-x64.json",
            Path("packaging/dependencies.json"),
        ),
        (
            ROOT / "packaging/release-manifest.windows-x64.json",
            Path("packaging/release-manifest.json"),
        ),
        (
            ROOT / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km",
            Path("packaging/ollama/Modelfile.hy-mt2-1.8b-q4km"),
        ),
        (ROOT / "packaging/schemas", Path("packaging/schemas")),
        (ROOT / "docs/LICENSES", Path("docs/LICENSES")),
    ]
    for name in WINDOWS_SCRIPTS:
        mappings.append((ROOT / "scripts" / name, Path("scripts") / name))
    for name in WINDOWS_TOOLS:
        mappings.append((ROOT / "packaging/tools" / name, Path("packaging/tools") / name))
    return mappings


def _entries() -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    destinations: set[Path] = set()
    for source, destination in _source_mappings():
        if path_is_link_like(source) or not source.exists():
            raise WindowsPackageError(f"Windows release source is unsafe: {destination}")
        candidates = [source] if source.is_file() else sorted(source.rglob("*"))
        for candidate in candidates:
            if path_is_link_like(candidate):
                raise WindowsPackageError("Windows release source contains a link")
            if candidate.is_dir():
                continue
            if (
                not candidate.is_file()
                or candidate.name in SKIP_NAMES
                or candidate.suffix == ".pyc"
            ):
                continue
            relative = Path() if source.is_file() else candidate.relative_to(source)
            target = destination / relative
            if target in destinations:
                raise WindowsPackageError(f"duplicate Windows release path: {target}")
            destinations.add(target)
            entries.append((candidate, target))
    return sorted(entries, key=lambda item: item[1].as_posix())


def _content(source: Path) -> bytes:
    content = source.read_bytes()
    if source.suffix.lower() in CRLF_SUFFIXES:
        content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if any(marker in content for marker in SECRET_MARKERS):
        raise WindowsPackageError("Windows release source contains a secret marker")
    return content


def package_windows_release(output_dir: Path) -> Path:
    manifest = _manifest()
    product = manifest.get("product")
    archive_config = manifest.get("archive")
    platform = manifest.get("platform")
    if (
        not isinstance(product, dict)
        or not isinstance(archive_config, dict)
        or not isinstance(platform, dict)
        or platform.get("os") != "windows"
        or platform.get("architecture") != "x86_64"
    ):
        raise WindowsPackageError("Windows release manifest is invalid")
    version = product.get("version")
    filename = archive_config.get("filename")
    root_name = archive_config.get("root")
    if (
        not isinstance(version, str)
        or not isinstance(filename, str)
        or not isinstance(root_name, str)
        or filename != f"LocalVideoTranscriber-{version}-windows-x64.zip"
        or root_name != f"LocalVideoTranscriber-{version}"
        or (ROOT / "VERSION").read_text(encoding="ascii").strip() != version
    ):
        raise WindowsPackageError("Windows release version is inconsistent")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / filename
    if path_is_link_like(archive_path):
        raise WindowsPackageError("Windows archive destination is unsafe")
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for source, destination in _entries():
            archive_name = (Path(root_name) / destination).as_posix()
            info = zipfile.ZipInfo(archive_name, FIXED_TIMESTAMP)
            info.create_system = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            info.flag_bits |= 0x800
            archive.writestr(info, _content(source), compress_type=zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Windows x64 release ZIP")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist-windows")
    arguments = parser.parse_args(argv)
    try:
        archive = package_windows_release(arguments.output_dir)
    except Exception:
        print("[ERROR] WINDOWS_PACKAGE_FAILED", file=sys.stderr)
        return 2
    print(f"[INFO] WINDOWS_PACKAGE_READY: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
