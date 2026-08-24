#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement

SHA256 = re.compile(r"^[0-9a-f]{64}$")
FLOATING = re.compile(r"(?:^|[/@:])(latest|main|master|stable)(?:$|[/])", re.IGNORECASE)
KNOWN_LICENSES = {
    "Apache-2.0",
    "Apache-2.0 OR MIT",
    "GPL-3.0-or-later",
    "MIT",
    "PSF-2.0",
}
PROJECT_LICENSE = {
    "spdx": "MIT",
    "owner_approved": True,
    "confirmed_by": "Leoy",
    "confirmed_at": "2026-08-24",
    "copyright": "Copyright (c) 2026 Leoy",
}
QWEN_MANIFEST = {
    "manifest_sha256": "65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b",
    "manifest_size": 857,
    "manifest_media_type": "application/vnd.docker.distribution.manifest.v2+json",
    "blobs": [
        {
            "digest": "sha256:377ac4d7aeefd5b870c9fccff9a6d4df36901d99fe3277c2f755bc401601ba1c",
            "size": 487,
            "media_type": "application/vnd.docker.container.image.v1+json",
        },
        {
            "digest": "sha256:183715c435899236895da3869489cc30ac241476b4971a20285b1a462818a5b4",
            "size": 986048512,
            "media_type": "application/vnd.ollama.image.model",
        },
        {
            "digest": "sha256:66b9ea09bd5b7099cbb4fc820f31b575c0366fa439b08245566692c6784e281e",
            "size": 68,
            "media_type": "application/vnd.ollama.image.system",
        },
        {
            "digest": "sha256:eb4402837c7829a690fa845de4d7f3fd842c2adee476d5341da8a46ea9255175",
            "size": 1482,
            "media_type": "application/vnd.ollama.image.template",
        },
        {
            "digest": "sha256:832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e",
            "size": 11343,
            "media_type": "application/vnd.ollama.image.license",
        },
    ],
}
COMPONENT_EVIDENCE = {
    "ollama": {
        "path": "docs/LICENSES/Ollama-MIT.txt",
        "sha256": "5934ed2ce0d15154bcdb9c85203210abac0da4314af34081e36df4599f90b226",
        "required_notice": "Copyright (c) Ollama",
    },
    "asr-whisper-small-mlx-config": {
        "path": "docs/LICENSES/Whisper-MIT.txt",
        "sha256": "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d",
        "required_notice": "Copyright (c) 2022 OpenAI",
    },
    "asr-whisper-small-mlx-weights": {
        "path": "docs/LICENSES/Whisper-MIT.txt",
        "sha256": "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d",
        "required_notice": "Copyright (c) 2022 OpenAI",
    },
    "diarization-segmentation": {
        "path": "docs/LICENSES/Pyannote-Segmentation-MIT.txt",
        "sha256": "63a777ad4b3c7aed4b260b084d8fb49ec781c46c70c6b599ca5d2402ef7ebe50",
        "required_notice": "Copyright (c) 2023 CNRS",
    },
    "hy-mt2": {
        "path": "docs/LICENSES/Hy-MT2-Apache-2.0.txt",
        "sha256": "1af3c6dc0c697277cbb6b68720787c1caa43a79c5626bf9f19cd8c00de9c8cd4",
        "required_notice": "Copyright (C) 2026 Tencent. All rights reserved.",
    },
    "qwen2.5-1.5b": {
        "path": "docs/LICENSES/Qwen2.5-Apache-2.0.txt",
        "sha256": "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e",
        "size": 11343,
        "required_notice": "Copyright 2024 Alibaba Cloud",
    },
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_expected_files(value: Any, identifier: str) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"dependency expected files are invalid: {identifier}")
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"dependency expected file is invalid: {identifier}")
        if (
            "\\" in entry
            or PurePosixPath(entry).is_absolute()
            or PureWindowsPath(entry).is_absolute()
            or any(part in {"", ".", ".."} for part in entry.split("/"))
        ):
            raise ValueError(f"dependency expected file path is unsafe: {identifier}")


def validate_dependency_manifest(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1 or payload.get("target") != "macos-arm64":
        raise ValueError("dependency manifest schema or target is invalid")
    if payload.get("trust_policy") != {
        "allow_runtime_digest_rewrite": False,
        "allow_floating_tags": False,
        "allowed_schemes": ["https"],
        "allowed_architectures": ["arm64"],
    }:
        raise ValueError("dependency trust policy is not strict")

    seen: set[str] = set()
    for item in payload["artifacts"]:
        identifier = item.get("id")
        if not isinstance(identifier, str) or identifier in seen:
            raise ValueError(f"invalid or duplicate dependency id: {identifier!r}")
        seen.add(identifier)
        url = item.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme != "https" or FLOATING.search(url):
            raise ValueError(f"dependency URL is not immutable HTTPS: {identifier}")
        if identifier == "ffmpeg":
            if "raw.githubusercontent.com/zackees/ffmpeg_bins/" in url:
                raise ValueError("FFmpeg URL resolves to a Git LFS pointer")
            if not url.startswith("https://media.githubusercontent.com/media/zackees/ffmpeg_bins/"):
                raise ValueError("FFmpeg must use the pinned GitHub media URL")
        if item.get("architecture") != "arm64":
            raise ValueError(f"dependency architecture is not arm64: {identifier}")
        if not isinstance(item.get("sha256"), str) or not SHA256.fullmatch(item["sha256"]):
            raise ValueError(f"dependency SHA-256 is invalid: {identifier}")
        if (
            not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] <= 0
        ):
            raise ValueError(f"dependency size is invalid: {identifier}")
        if not isinstance(item.get("media_type"), str) or not item["media_type"].strip():
            raise ValueError(f"dependency media type is invalid: {identifier}")
        _validate_expected_files(item.get("expected_files"), identifier)
        if item.get("license") not in KNOWN_LICENSES:
            raise ValueError(f"unknown license for {identifier}: {item.get('license')!r}")
        license_url = item.get("license_url")
        if (
            not isinstance(license_url, str)
            or urlsplit(license_url).scheme != "https"
            or FLOATING.search(license_url)
        ):
            raise ValueError(f"license source is not immutable HTTPS: {identifier}")
        if identifier == "ollama":
            if item.get("expected_files") != [
                "Ollama.app/Contents/MacOS/Ollama",
                "Ollama.app/Contents/Resources/ollama",
            ]:
                raise ValueError("Ollama expected files must preserve GUI and CLI paths")
            if item.get("executable") != "Ollama.app/Contents/Resources/ollama":
                raise ValueError("Ollama executable must be the CLI/daemon")
        if identifier == "hy-mt2":
            if item.get("license_url") != (
                "https://huggingface.co/tencent/Hy-MT2-1.8B/resolve/"
                "9a341cd1b679d3efd23b46e847b01745a71ed792/LICENSE.txt"
            ):
                raise ValueError("Hy-MT2 license URL must use the fixed base-model license")
            if item.get("license_basis") != {
                "base_model": "tencent/Hy-MT2-1.8B",
                "readme_url": (
                    "https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/resolve/"
                    "1cd5208700acedef4ef93019b6cfc148b8522d45/README.md"
                ),
                "readme_sha256": (
                    "4c37a2e6b69773b102027c71e1d5377946d697c927c2604f91af5cdd5624f91f"
                ),
                "evidence_path": "docs/LICENSES/Hy-MT2-GGUF-README.md",
                "evidence_sha256": (
                    "4319879604ecb49e6998d3e2623525600b346e75501ebd0c1ce25abec0c9f05f"
                ),
            }:
                raise ValueError("Hy-MT2 GGUF base-model evidence is invalid")

    for model in payload["ollama_models"]:
        identifier = model.get("id")
        if not isinstance(identifier, str) or identifier in seen:
            raise ValueError(f"invalid or duplicate dependency id: {identifier!r}")
        seen.add(identifier)
        url = model.get("manifest_url")
        if not isinstance(url, str) or urlsplit(url).scheme != "https" or FLOATING.search(url):
            raise ValueError(f"Ollama manifest URL is not pinned HTTPS: {identifier}")
        if model.get("architecture") != "arm64":
            raise ValueError(f"Ollama model architecture is not arm64: {identifier}")
        if not isinstance(model.get("manifest_sha256"), str) or not SHA256.fullmatch(
            model["manifest_sha256"]
        ):
            raise ValueError(f"Ollama manifest SHA-256 is invalid: {identifier}")
        if (
            not isinstance(model.get("manifest_size"), int)
            or isinstance(model.get("manifest_size"), bool)
            or model["manifest_size"] <= 0
        ):
            raise ValueError(f"Ollama manifest size is invalid: {identifier}")
        if (
            not isinstance(model.get("manifest_media_type"), str)
            or not model["manifest_media_type"]
        ):
            raise ValueError(f"Ollama manifest media type is invalid: {identifier}")
        if model.get("license") not in KNOWN_LICENSES:
            raise ValueError(f"unknown license for {identifier}: {model.get('license')!r}")
        license_url = model.get("license_url")
        if (
            not isinstance(license_url, str)
            or urlsplit(license_url).scheme != "https"
            or FLOATING.search(license_url)
        ):
            raise ValueError(f"license source is not immutable HTTPS: {identifier}")
        _validate_expected_files(model.get("expected_files"), identifier)
        if not model.get("blobs"):
            raise ValueError(f"Ollama model metadata is incomplete: {identifier}")
        for blob in model["blobs"]:
            if not isinstance(blob.get("digest"), str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", blob["digest"]
            ):
                raise ValueError(f"Ollama blob digest is invalid: {identifier}")
            if not isinstance(blob.get("size"), int) or blob["size"] <= 0:
                raise ValueError(f"Ollama blob size is invalid: {identifier}")
            if not blob.get("media_type"):
                raise ValueError(f"Ollama blob media type is missing: {identifier}")
        if identifier == "qwen2.5-1.5b":
            if any(
                model.get(key) != value for key, value in QWEN_MANIFEST.items() if key != "blobs"
            ):
                raise ValueError("qwen manifest metadata changed")
            if model.get("blobs") != QWEN_MANIFEST["blobs"]:
                raise ValueError("qwen blob metadata changed")
            if model.get("license_url") != (
                "https://registry.ollama.ai/v2/library/qwen2.5/blobs/"
                "sha256:832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
            ):
                raise ValueError("qwen license URL must use the content-addressed license blob")


def validate_component_license_evidence(root: Path, payload: dict[str, Any]) -> None:
    components = {item["id"]: item for item in [*payload["artifacts"], *payload["ollama_models"]]}
    for identifier, expected in COMPONENT_EVIDENCE.items():
        evidence = components[identifier].get("license_evidence")
        if evidence != expected:
            raise ValueError(f"component license evidence metadata mismatch: {identifier}")
        path = root / expected["path"]
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected["sha256"]:
            raise ValueError(f"component license evidence digest mismatch: {identifier}")
        if "size" in expected and len(content) != expected["size"]:
            raise ValueError(f"component license evidence size mismatch: {identifier}")
        if expected["required_notice"] not in content.decode("utf-8"):
            raise ValueError(f"component license notice missing: {identifier}")

    hy_basis = components["hy-mt2"]["license_basis"]
    readme = root / hy_basis["evidence_path"]
    if _sha256(readme) != hy_basis["evidence_sha256"]:
        raise ValueError("Hy-MT2 GGUF README digest mismatch")
    if "- tencent/Hy-MT2-1.8B" not in readme.read_text(encoding="utf-8"):
        raise ValueError("Hy-MT2 GGUF README does not prove the base-model relationship")


def _export_packages(root: Path, uv: str, *, dev: bool) -> dict[str, str]:
    command = [
        uv,
        "export",
        "--project",
        str(root / "backend"),
        "--frozen",
        "--no-hashes",
        "--no-emit-project",
        "--format",
        "requirements-txt",
    ]
    if dev:
        command.extend(["--extra", "dev"])
    else:
        command.append("--no-dev")
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    environment = default_environment()
    environment.update(
        {
            "python_version": "3.11",
            "python_full_version": "3.11.15",
            "sys_platform": "darwin",
            "platform_system": "Darwin",
            "platform_machine": "arm64",
            "implementation_name": "cpython",
        }
    )
    packages: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        if not requirement.specifier:
            raise ValueError(f"uv export returned an unpinned requirement: {line}")
        versions = [item.version for item in requirement.specifier if item.operator == "=="]
        if len(versions) != 1:
            raise ValueError(f"uv export returned a non-exact requirement: {line}")
        packages[_normalize(requirement.name)] = versions[0]
    return packages


def validate_python_inventory(root: Path, uv: str) -> int:
    inventory_path = root / "docs/LICENSES/python-runtime.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    lock_path = root / inventory["lock_file"]
    if inventory["lock_sha256"] != _sha256(lock_path):
        raise ValueError("Python inventory uv.lock fingerprint mismatch")

    runtime = _export_packages(root, uv, dev=False)
    development = _export_packages(root, uv, dev=True)
    expected = set(development)
    entries = inventory["packages"]
    by_name = {item["package"]: item for item in entries}
    if len(by_name) != len(entries) or set(by_name) != expected:
        raise ValueError("Python inventory package set does not match frozen uv export")
    if inventory["runtime_count"] != len(runtime):
        raise ValueError("Python runtime inventory count mismatch")
    if inventory["development_only_count"] != len(expected - set(runtime)):
        raise ValueError("Python development inventory count mismatch")

    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    lock_versions: dict[str, set[str]] = {}
    for item in lock["package"]:
        lock_versions.setdefault(_normalize(item["name"]), set()).add(item["version"])
    for name, item in by_name.items():
        version = development[name]
        expected_scope = "runtime" if name in runtime else "development-only"
        if item.get("version") != version or version not in lock_versions.get(name, set()):
            raise ValueError(f"Python inventory version mismatch: {name}")
        if item.get("scope") != expected_scope:
            raise ValueError(f"Python inventory scope mismatch: {name}")
        if not item.get("license") or str(item["license"]).upper() == "UNKNOWN":
            raise ValueError(f"Python inventory license is unknown: {name}")
        if item.get("source") != f"https://pypi.org/project/{name}/{version}/":
            raise ValueError(f"Python inventory source mismatch: {name}")
        if item.get("license_source") != f"https://pypi.org/pypi/{name}/{version}/json":
            raise ValueError(f"Python inventory license source mismatch: {name}")
        if item.get("lock_ref") != f"backend/uv.lock:{name}=={version}":
            raise ValueError(f"Python inventory lock trace mismatch: {name}")
    return len(entries)


def validate_npm_inventory(root: Path) -> int:
    lock_path = root / "extension/package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    inventory = json.loads((root / "docs/LICENSES/npm-all.json").read_text(encoding="utf-8"))
    if inventory["lock_sha256"] != _sha256(lock_path):
        raise ValueError("npm inventory package-lock fingerprint mismatch")

    expected = {path: item for path, item in lock["packages"].items() if path}
    entries = inventory["packages"]
    by_path = {item["path"]: item for item in entries}
    if len(by_path) != len(entries) or set(by_path) != set(expected):
        raise ValueError("npm inventory package paths do not match package-lock")
    if inventory["package_count"] != len(expected):
        raise ValueError("npm inventory count mismatch")

    for path, locked in expected.items():
        item = by_path[path]
        expected_scope = "development-build-only" if locked.get("dev") else "runtime"
        for field in ("version", "license", "source", "integrity"):
            locked_field = "resolved" if field == "source" else field
            if item.get(field) != locked.get(locked_field):
                raise ValueError(f"npm inventory {field} mismatch: {path}")
        if item.get("scope") != expected_scope:
            raise ValueError(f"npm inventory scope mismatch: {path}")
        if not item["license"] or str(item["license"]).upper() == "UNKNOWN":
            raise ValueError(f"npm inventory license is unknown: {path}")
        if item.get("lock_ref") != f"extension/package-lock.json#packages/{path}":
            raise ValueError(f"npm inventory lock trace mismatch: {path}")
    return len(entries)


def check_inventory(root: Path, uv: str) -> tuple[int, int]:
    release = json.loads((root / "packaging/release-manifest.json").read_text(encoding="utf-8"))
    if release.get("project_license") != PROJECT_LICENSE:
        raise ValueError("release contract does not contain the approved MIT owner record")
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    if "MIT License\n\nCopyright (c) 2026 Leoy\n" not in license_text:
        raise ValueError("project LICENSE does not match the owner-approved copyright")

    dependencies = json.loads((root / "packaging/dependencies.json").read_text(encoding="utf-8"))
    validate_dependency_manifest(dependencies)
    validate_component_license_evidence(root, dependencies)
    python_count = validate_python_inventory(root, uv)
    npm_count = validate_npm_inventory(root)

    notice = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for marker in (
        "Copyright (c) 2026 Leoy",
        "67 runtime",
        "10 development-only",
        "166 development/build-only",
        "docs/LICENSES/python-runtime.json",
        "docs/LICENSES/npm-all.json",
        "docs/LICENSES/Ollama-MIT.txt",
        "docs/LICENSES/Whisper-MIT.txt",
        "docs/LICENSES/Pyannote-Segmentation-MIT.txt",
        "docs/LICENSES/Hy-MT2-Apache-2.0.txt",
        "docs/LICENSES/Hy-MT2-GGUF-README.md",
        "docs/LICENSES/Qwen2.5-Apache-2.0.txt",
    ):
        if marker not in notice:
            raise ValueError(f"THIRD_PARTY_NOTICES missing coverage marker: {marker}")

    required_texts = {
        "MIT.txt": ("MIT License", 1000),
        "Apache-2.0.txt": ("Apache License", 10000),
        "GPL-3.0-or-later.txt": ("GNU GENERAL PUBLIC LICENSE", 30000),
        "PSF-2.0.txt": ("PYTHON SOFTWARE FOUNDATION LICENSE", 10000),
    }
    for filename, (marker, minimum_size) in required_texts.items():
        text = (root / "docs/LICENSES" / filename).read_text(encoding="utf-8")
        if marker not in text or len(text) < minimum_size:
            raise ValueError(f"license text is incomplete: docs/LICENSES/{filename}")
    return python_count, npm_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    try:
        python_count, npm_count = check_inventory(args.root.resolve(), args.uv)
    except (
        KeyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"license inventory check failed: {exc}", file=sys.stderr)
        return 1
    print(f"license inventory passed: python={python_count}, npm={npm_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
