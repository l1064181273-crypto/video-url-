#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

KNOWN_LICENSES = {
    "0BSD",
    "Apache-2.0",
    "Apache-2.0 OR MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "GPL-3.0-or-later",
    "ISC",
    "MIT",
    "MPL-2.0",
    "PSF-2.0",
    "Python-2.0",
    "Unlicense",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FLOATING = re.compile(r"(?:^|[/@:])(latest|main|master|stable)(?:$|[/])", re.IGNORECASE)


def _requirement_name(value: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", value)
    if match is None:
        raise ValueError(f"invalid requirement: {value!r}")
    return match.group(0).lower().replace("_", "-")


def expected_inventory(root: Path) -> dict[str, set[str]]:
    pyproject = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    python = {_requirement_name(item) for item in pyproject["project"]["dependencies"]}
    package = json.loads((root / "extension/package.json").read_text(encoding="utf-8"))
    npm = set(package.get("dependencies", {})) | set(package.get("devDependencies", {}))
    dependencies = json.loads((root / "packaging/dependencies.json").read_text(encoding="utf-8"))
    external = {str(item["id"]) for item in dependencies["artifacts"]}
    external |= {str(item["id"]) for item in dependencies["ollama_models"]}
    return {"python": python, "npm": npm, "external": external}


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
        if item.get("architecture") != "arm64":
            raise ValueError(f"dependency architecture is not arm64: {identifier}")
        if not isinstance(item.get("sha256"), str) or not SHA256.fullmatch(item["sha256"]):
            raise ValueError(f"dependency SHA-256 is invalid: {identifier}")
        if not isinstance(item.get("size"), int) or item["size"] <= 0:
            raise ValueError(f"dependency size is invalid: {identifier}")
        if not item.get("media_type") or not item.get("expected_files"):
            raise ValueError(f"dependency metadata is incomplete: {identifier}")
        license_name = item.get("license")
        if license_name not in KNOWN_LICENSES:
            raise ValueError(f"unknown license for {identifier}: {license_name!r}")
        license_url = item.get("license_url")
        if not isinstance(license_url, str) or urlsplit(license_url).scheme != "https":
            raise ValueError(f"invalid license source for {identifier}")
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
        if not isinstance(model.get("manifest_size"), int) or model["manifest_size"] <= 0:
            raise ValueError(f"Ollama manifest size is invalid: {identifier}")
        if not model.get("manifest_media_type"):
            raise ValueError(f"Ollama manifest media type is missing: {identifier}")
        if model.get("license") not in KNOWN_LICENSES:
            raise ValueError(f"unknown license for {identifier}: {model.get('license')!r}")
        license_url = model.get("license_url")
        if not isinstance(license_url, str) or urlsplit(license_url).scheme != "https":
            raise ValueError(f"invalid license source for {identifier}")
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


def _validate_npm_licenses(root: Path) -> None:
    lock = json.loads((root / "extension/package-lock.json").read_text(encoding="utf-8"))
    for path, package in lock["packages"].items():
        if path == "":
            continue
        license_name = package.get("license")
        if not isinstance(license_name, str) or not license_name.strip():
            raise ValueError(f"npm package has no declared license: {path}")
        if "SEE LICENSE IN" in license_name.upper():
            raise ValueError(f"npm package has unresolved license: {path}")


def validate_installed_python_licenses() -> int:
    checked = 0
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "").lower().replace("_", "-")
        if not name or name == "local-video-transcriber":
            continue
        declared = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or next(
                (
                    value
                    for value in distribution.metadata.get_all("Classifier", [])
                    if value.startswith("License ::")
                ),
                "",
            )
        )
        if not declared.strip() or declared.strip().upper() == "UNKNOWN":
            raise ValueError(f"installed Python package has no declared license: {name}")
        checked += 1
    if checked == 0:
        raise ValueError("no installed Python dependency metadata found")
    return checked


def check_inventory(root: Path) -> None:
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    if not license_text.startswith("MIT License\n"):
        raise ValueError("project LICENSE is not the owner-approved MIT license")

    dependencies = json.loads((root / "packaging/dependencies.json").read_text(encoding="utf-8"))
    validate_dependency_manifest(dependencies)
    _validate_npm_licenses(root)
    validate_installed_python_licenses()

    notice = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for ecosystem, names in expected_inventory(root).items():
        missing = sorted(name for name in names if f"`{ecosystem}:{name}`" not in notice)
        if missing:
            raise ValueError(f"THIRD_PARTY_NOTICES missing {ecosystem}: {missing}")

    for filename in ("MIT.txt", "Apache-2.0.txt", "GPL-3.0-or-later.txt", "PSF-2.0.txt"):
        path = root / "docs/LICENSES" / filename
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing license reference: {path.relative_to(root)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        check_inventory(args.root.resolve())
    except (KeyError, OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"license inventory check failed: {exc}", file=sys.stderr)
        return 1
    counts = expected_inventory(args.root.resolve())
    installed_python = validate_installed_python_licenses()
    print(
        "license inventory passed: "
        + ", ".join(f"{name}={len(items)}" for name, items in sorted(counts.items()))
        + f", installed_python={installed_python}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
