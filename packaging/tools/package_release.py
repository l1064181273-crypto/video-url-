#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXED_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
RUNTIME_TOOLS = (
    "doctor.py",
    "install.py",
    "lifecycle_lock.py",
    "process_state.py",
    "provision.py",
    "publish_install.py",
    "reconcile_processes.py",
    "runtime_layout.py",
    "tool_supervisor.py",
    "transaction_journal.py",
    "verify_install.py",
)
RUNTIME_SCRIPTS = (
    "doctor.command",
    "install.command",
    "start.command",
    "stop.command",
    "lib/common.zsh",
    "lib/download.zsh",
    "lib/process.zsh",
)
SKIP_NAMES = {".DS_Store", "__pycache__"}


class PackageError(RuntimeError):
    pass


def _manifest() -> dict[str, object]:
    path = ROOT / "packaging/release-manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError("release manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise PackageError("release manifest is invalid")
    return payload


def _source_mappings() -> tuple[tuple[Path, Path], ...]:
    mappings = [
        (ROOT / "VERSION", Path("VERSION")),
        (ROOT / "LICENSE", Path("LICENSE")),
        (ROOT / "THIRD_PARTY_NOTICES.md", Path("THIRD_PARTY_NOTICES.md")),
        (ROOT / "README.md", Path("README.md")),
        (ROOT / "新手使用说明.txt", Path("新手使用说明.txt")),
        (
            ROOT / "启动 Local Video Transcriber.command",
            Path("启动 Local Video Transcriber.command"),
        ),
        (ROOT / "backend/pyproject.toml", Path("backend/pyproject.toml")),
        (ROOT / "backend/uv.lock", Path("backend/uv.lock")),
        (ROOT / "backend/src", Path("backend/src")),
        (ROOT / "extension/dist", Path("extension")),
        (ROOT / "packaging/dependencies.json", Path("packaging/dependencies.json")),
        (
            ROOT / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km",
            Path("packaging/ollama/Modelfile.hy-mt2-1.8b-q4km"),
        ),
        (ROOT / "packaging/schemas", Path("packaging/schemas")),
        (ROOT / "docs/LICENSES", Path("docs/LICENSES")),
    ]
    for name in RUNTIME_SCRIPTS:
        mappings.append((ROOT / "scripts" / name, Path("scripts") / name))
    for name in (
        "INSTALLATION.md",
        "USER_GUIDE.md",
        "TROUBLESHOOTING.md",
        "KNOWN_LIMITATIONS.md",
    ):
        mappings.append((ROOT / "docs" / name, Path("docs") / name))
    for name in RUNTIME_TOOLS:
        mappings.append((ROOT / "packaging/tools" / name, Path("packaging/tools") / name))
    return tuple(mappings)


def _entries() -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    destinations: set[Path] = set()
    for source, destination in _source_mappings():
        if source.is_symlink() or not source.exists():
            raise PackageError(f"release source is missing or unsafe: {destination}")
        candidates = [source] if source.is_file() else sorted(source.rglob("*"))
        for candidate in candidates:
            if candidate.is_symlink():
                raise PackageError(f"release source contains a symlink: {destination}")
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
            if any(part in SKIP_NAMES for part in target.parts) or target in destinations:
                continue
            destinations.add(target)
            entries.append((candidate, target))
    return sorted(entries, key=lambda item: item[1].as_posix())


def package_release(output_dir: Path) -> Path:
    payload = _manifest()
    product = payload.get("product")
    archive_config = payload.get("archive")
    if not isinstance(product, dict) or not isinstance(archive_config, dict):
        raise PackageError("release manifest is invalid")
    version = product.get("version")
    filename = archive_config.get("filename")
    if not isinstance(version, str) or not isinstance(filename, str):
        raise PackageError("release manifest is invalid")
    if (ROOT / "VERSION").read_text(encoding="ascii").strip() != version:
        raise PackageError("release version is inconsistent")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / filename
    if archive_path.is_symlink():
        raise PackageError("archive destination is unsafe")
    package_root = Path(f"LocalVideoTranscriber-{version}")
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for source, destination in _entries():
            archive_name = (package_root / destination).as_posix()
            mode = 0o755 if source.suffix == ".command" else 0o644
            info = zipfile.ZipInfo(archive_name, FIXED_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Local Video Transcriber 分发 ZIP")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args(argv)
    try:
        archive = package_release(arguments.output_dir)
    except Exception:
        print("[ERROR] PACKAGE_FAILED：分发包构建失败", file=sys.stderr)
        return 2
    print(f"[INFO] PACKAGE_READY：{archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
