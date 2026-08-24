#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


def collect_versions(root: Path) -> dict[str, str]:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    manifest = json.loads((root / "extension/public/manifest.json").read_text(encoding="utf-8"))
    app_source = (root / "backend/src/lvt/api/app.py").read_text(encoding="utf-8")
    fastapi = re.search(r'FastAPI\([^)]*\bversion="([^"]+)"', app_source)
    health = re.search(r'\{"status": "healthy", "version": "([^"]+)"\}', app_source)
    if fastapi is None or health is None:
        raise ValueError("backend app version declarations are missing")
    return {
        "VERSION": version,
        "backend_metadata": str(pyproject["project"]["version"]),
        "backend_fastapi": fastapi.group(1),
        "backend_health": health.group(1),
        "extension_manifest": str(manifest["version"]),
    }


def check_versions(root: Path) -> None:
    versions = collect_versions(root)
    expected = versions["VERSION"]
    if expected != "0.1.0":
        raise ValueError(f"VERSION must be 0.1.0, got {expected!r}")
    mismatches = {name: value for name, value in versions.items() if value != expected}
    if mismatches:
        raise ValueError(f"version mismatch: {mismatches}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        check_versions(args.root.resolve())
    except (KeyError, OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"version check failed: {exc}", file=sys.stderr)
        return 1
    print("version check passed: 0.1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
