from __future__ import annotations

import copy
import importlib.util
import json
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
    license_inventory.check_inventory(ROOT)


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
