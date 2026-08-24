#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
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


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        if not isinstance(item.get("size"), int) or item["size"] <= 0:
            raise ValueError(f"dependency size is invalid: {identifier}")
        if not item.get("media_type") or not item.get("expected_files"):
            raise ValueError(f"dependency metadata is incomplete: {identifier}")
        if item.get("license") not in KNOWN_LICENSES:
            raise ValueError(f"unknown license for {identifier}: {item.get('license')!r}")
        license_url = item.get("license_url")
        if (
            not isinstance(license_url, str)
            or urlsplit(license_url).scheme != "https"
            or FLOATING.search(license_url)
        ):
            raise ValueError(f"license source is not immutable HTTPS: {identifier}")

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
        if model.get("license") not in KNOWN_LICENSES:
            raise ValueError(f"unknown license for {identifier}: {model.get('license')!r}")
        license_url = model.get("license_url")
        if (
            not isinstance(license_url, str)
            or urlsplit(license_url).scheme != "https"
            or FLOATING.search(license_url)
        ):
            raise ValueError(f"license source is not immutable HTTPS: {identifier}")
        if not model.get("expected_files") or not model.get("blobs"):
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
