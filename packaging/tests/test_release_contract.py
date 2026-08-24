from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_versions = _load_module("check_versions", "packaging/tools/check_versions.py")
license_inventory = _load_module("license_inventory", "packaging/tools/license_inventory.py")


def _dependencies() -> dict[str, Any]:
    return json.loads((ROOT / "packaging/dependencies.json").read_text(encoding="utf-8"))


def _uv() -> str:
    return os.environ.get("UV", "uv")


def _copy_license_contract(destination: Path) -> None:
    for relative in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "extension/package-lock.json",
        "packaging/dependencies.json",
        "packaging/release-manifest.json",
        "docs/LICENSES/python-runtime.json",
        "docs/LICENSES/npm-all.json",
        "docs/LICENSES/MIT.txt",
        "docs/LICENSES/Apache-2.0.txt",
        "docs/LICENSES/GPL-3.0-or-later.txt",
        "docs/LICENSES/PSF-2.0.txt",
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def validate_dependencies(payload: dict[str, Any]) -> None:
    license_inventory.validate_dependency_manifest(payload)


def test_versions_are_consistent() -> None:
    check_versions.check_versions(ROOT)


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        ("VERSION", "0.1.0", "0.1.1"),
        ("backend/pyproject.toml", 'version = "0.1.0"', 'version = "0.1.1"'),
        (
            "backend/src/lvt/api/app.py",
            '{"status": "healthy", "version": "0.1.0"}',
            '{"status": "healthy", "version": "0.1.1"}',
        ),
        ("extension/public/manifest.json", '"version": "0.1.0"', '"version": "0.1.1"'),
    ],
)
def test_each_version_source_mismatch_is_rejected(
    tmp_path: Path, relative: str, old: str, new: str
) -> None:
    for path in (
        "VERSION",
        "backend/pyproject.toml",
        "backend/src/lvt/api/app.py",
        "extension/public/manifest.json",
    ):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    target = tmp_path / relative
    target.write_text(target.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError):
        check_versions.check_versions(tmp_path)


def test_dependency_manifest_is_fully_pinned() -> None:
    validate_dependencies(_dependencies())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["artifacts"][0].update(url="http://example.invalid/uv.tar.gz"),
        lambda data: data["artifacts"][0].update(sha256=""),
        lambda data: data["artifacts"][0].update(architecture="x86_64"),
        lambda data: data["artifacts"][0].update(
            url="https://github.com/example/releases/latest/download/tool.zip"
        ),
        lambda data: data["artifacts"][0].update(license="UNKNOWN"),
        lambda data: data["artifacts"][2].update(
            url="https://raw.githubusercontent.com/zackees/ffmpeg_bins/"
            "df95abcb0ce6efff710dda5ef28a2f6f1dc21493/v8.0/darwin_arm64.zip"
        ),
        lambda data: data["artifacts"][4].update(
            license_url="https://github.com/openai/whisper/blob/main/LICENSE"
        ),
        lambda data: data["ollama_models"][0]["blobs"][0].update(digest=""),
        lambda data: data["trust_policy"].update(allow_runtime_digest_rewrite=True),
    ],
)
def test_dependency_manifest_rejects_unsafe_mutations(mutation: Any) -> None:
    payload = copy.deepcopy(_dependencies())
    mutation(payload)
    with pytest.raises(ValueError):
        validate_dependencies(payload)


def test_release_manifest_defines_arm64_allowlist_and_exclusions() -> None:
    payload = json.loads((ROOT / "packaging/release-manifest.json").read_text(encoding="utf-8"))
    assert payload["product"] == {"name": "Local Video Transcriber", "version": "0.1.0"}
    assert payload["project_license"] == license_inventory.PROJECT_LICENSE
    assert payload["platform"] == {
        "os": "macos",
        "minimum_version": "13.0",
        "architecture": "arm64",
    }
    assert payload["archive"]["filename"] == "LocalVideoTranscriber-0.1.0-macos-arm64.zip"
    assert payload["allowlist"]
    assert payload["file_modes"]["executables"]["*.command"] == "0755"
    forbidden = "\n".join(payload["forbidden"]).lower()
    for marker in ("model", "api-token", "sqlite", ".log", ".mp4", "cache", "test-results", ".map"):
        assert marker in forbidden


def test_uv_lock_is_complete_and_has_no_machine_paths() -> None:
    lock = (ROOT / "backend/uv.lock").read_text(encoding="utf-8")
    assert 'name = "local-video-transcriber"' in lock
    assert 'version = "0.1.0"' in lock
    assert "sdist = {" in lock
    assert "wheels = [" in lock
    assert "/" + "Users/" not in lock
    assert ".venv-smoke" not in lock
    assert re.findall(r'source = \{ editable = "([^"]+)" \}', lock) == ["."]


def test_license_inventory_matches_notices() -> None:
    assert license_inventory.check_inventory(ROOT, _uv()) == (77, 166)


def test_ffmpeg_uses_pinned_media_archive() -> None:
    ffmpeg = next(item for item in _dependencies()["artifacts"] if item["id"] == "ffmpeg")
    assert ffmpeg == {
        "id": "ffmpeg",
        "kind": "tool",
        "version": "8.0",
        "architecture": "arm64",
        "url": "https://media.githubusercontent.com/media/zackees/ffmpeg_bins/"
        "df95abcb0ce6efff710dda5ef28a2f6f1dc21493/v8.0/darwin_arm64.zip",
        "sha256": "b2da44a8169c4d09a97db996250690c3346f72e4795521d23d3dbb1e72421207",
        "size": 41925556,
        "media_type": "application/zip",
        "license": "GPL-3.0-or-later",
        "license_url": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n8.0/COPYING.GPLv3",
        "expected_files": ["darwin_arm64/ffmpeg", "darwin_arm64/ffprobe"],
    }


def test_qwen_manifest_and_blobs_remain_frozen() -> None:
    dependencies = _dependencies()
    qwen = dependencies["ollama_models"][0]
    assert qwen["manifest_sha256"] == (
        "65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b"
    )
    assert [blob["digest"] for blob in qwen["blobs"]] == [
        "sha256:377ac4d7aeefd5b870c9fccff9a6d4df36901d99fe3277c2f755bc401601ba1c",
        "sha256:183715c435899236895da3869489cc30ac241476b4971a20285b1a462818a5b4",
        "sha256:66b9ea09bd5b7099cbb4fc820f31b575c0366fa439b08245566692c6784e281e",
        "sha256:eb4402837c7829a690fa845de4d7f3fd842c2adee476d5341da8a46ea9255175",
        "sha256:832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e",
    ]
    assert dependencies["trust_policy"]["allow_runtime_digest_rewrite"] is False


def test_owner_approved_license_is_explicit() -> None:
    assert "Copyright (c) 2026 Leoy" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    release = json.loads((ROOT / "packaging/release-manifest.json").read_text(encoding="utf-8"))
    assert release["project_license"] == license_inventory.PROJECT_LICENSE


@pytest.mark.parametrize(
    ("inventory", "mutation"),
    [
        ("python-runtime.json", lambda data: data["packages"].pop()),
        ("python-runtime.json", lambda data: data["packages"][0].update(version="0.0.0")),
        ("python-runtime.json", lambda data: data["packages"][0].update(license="UNKNOWN")),
        (
            "python-runtime.json",
            lambda data: data["packages"][0].update(source="https://example.invalid"),
        ),
        ("npm-all.json", lambda data: data["packages"].pop()),
        ("npm-all.json", lambda data: data["packages"][0].update(version="0.0.0")),
        ("npm-all.json", lambda data: data["packages"][0].update(license="UNKNOWN")),
        ("npm-all.json", lambda data: data["packages"][0].update(source="https://example.invalid")),
    ],
)
def test_transitive_inventory_mutations_are_rejected(
    tmp_path: Path, inventory: str, mutation: Any
) -> None:
    _copy_license_contract(tmp_path)
    path = tmp_path / "docs/LICENSES" / inventory
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        license_inventory.check_inventory(tmp_path, _uv())


def test_normative_license_texts_are_complete() -> None:
    expected = {
        "MIT.txt": ("MIT License", 1000),
        "Apache-2.0.txt": ("Apache License", 10000),
        "GPL-3.0-or-later.txt": ("GNU GENERAL PUBLIC LICENSE", 30000),
        "PSF-2.0.txt": ("PYTHON SOFTWARE FOUNDATION LICENSE", 10000),
    }
    for filename, (marker, minimum) in expected.items():
        text = (ROOT / "docs/LICENSES" / filename).read_text(encoding="utf-8")
        assert marker in text
        assert len(text) >= minimum


@pytest.mark.parametrize("target", ["package", "verify-archive", "extracted-smoke", "verify"])
def test_future_make_targets_do_not_exist(target: str) -> None:
    completed = subprocess.run(
        ["make", "--no-print-directory", "-n", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "No rule to make target" in completed.stderr


def test_makefile_contains_only_checkpoint_one_targets_with_recipes() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = re.findall(r"^([a-z][a-z-]*):(?:\s.*)?$", makefile, re.MULTILINE)
    assert set(targets) == {
        "setup",
        "lint",
        "typecheck",
        "test",
        "test-integration",
        "build-extension",
        "smoke",
        "verify-source",
    }
    for target in targets:
        assert re.search(rf"^{re.escape(target)}:\n\t\S", makefile, re.MULTILINE)


def test_verify_source_runs_gates_serially() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = makefile.split("verify-source:\n", 1)[1]
    expected = [
        "$(VERIFY_SETUP)",
        "$(MAKE) --no-print-directory lint",
        "$(MAKE) --no-print-directory typecheck",
        "$(MAKE) --no-print-directory test",
        "$(MAKE) --no-print-directory test-integration",
        "$(MAKE) --no-print-directory build-extension",
        "$(MAKE) --no-print-directory smoke",
    ]
    positions = [recipe.index(command) for command in expected]
    assert positions == sorted(positions)


def test_verify_source_propagates_subcommand_failure() -> None:
    completed = subprocess.run(
        ["make", "--no-print-directory", "verify-source", "VERIFY_SETUP=false"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
