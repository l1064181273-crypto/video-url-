from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / "packaging" / "tools" / "doctor.py"
VERIFY_INSTALL = ROOT / "packaging" / "tools" / "verify_install.py"
SCHEMA = ROOT / "packaging" / "schemas" / "doctor-v1.schema.json"
GIB = 1024**3


def _write_executable(path: Path, content: str = "#!/bin/zsh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _base_environment(test_root: Path, fake_bin: Path) -> dict[str, str]:
    home = test_root / "临时 HOME"
    home.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": str(fake_bin),
            "LVT_TEST_ROOT": str(test_root),
            "LVT_TEST_PLATFORM": "macos",
            "LVT_TEST_MACOS_VERSION": "13.0",
            "LVT_TEST_ARCH": "arm64",
            "LVT_TEST_ROSETTA": "0",
            "LVT_TEST_MEMORY_BYTES": str(16 * GIB),
            "LVT_TEST_DISK_BYTES": str(12 * GIB),
            "LVT_TEST_OLLAMA_PORT": "owned",
            "LVT_TEST_BACKEND_HEALTH": "healthy",
        }
    )
    return environment


def _fake_path(root: Path, *, missing: set[str] | None = None) -> Path:
    missing = missing or set()
    fake_bin = root / "fake bin"
    fake_bin.mkdir(parents=True)
    for command in ("python3", "ffmpeg", "ffprobe", "ollama"):
        if command not in missing:
            _write_executable(fake_bin / command)
    return fake_bin


def _release_tree(root: Path) -> Path:
    release = root / "发布 candidate"
    (release / "backend" / "src" / "lvt").mkdir(parents=True)
    (release / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "local-video-transcriber"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    _write_executable(release / ".venv" / "bin" / "python")
    _write_executable(release / "scripts" / "doctor.command")
    dependencies = release / "packaging" / "dependencies.json"
    dependencies.parent.mkdir(parents=True)
    dependencies.write_bytes((ROOT / "packaging" / "dependencies.json").read_bytes())
    modelfile = release / "packaging" / "ollama" / "Modelfile.hy-mt2-1.8b-q4km"
    modelfile.parent.mkdir()
    modelfile.write_bytes(
        (ROOT / "packaging" / "ollama" / "Modelfile.hy-mt2-1.8b-q4km").read_bytes()
    )
    return release


def _dependency_tree(test_root: Path, release: Path) -> Path:
    data_root = test_root / "用户 数据" / "LocalVideoTranscriber"
    dependencies_path = release / "packaging" / "dependencies.json"
    dependencies = json.loads(dependencies_path.read_text())
    for item in [*dependencies["artifacts"], *dependencies["ollama_models"]]:
        for relative in item["expected_files"]:
            if relative.startswith("models/"):
                path = data_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if "expected_file_sha256" in item:
                    content = f"verified extracted fixture:{item['id']}".encode()
                    path.write_bytes(content)
                    item["expected_file_size"] = len(content)
                    item["expected_file_sha256"] = hashlib.sha256(content).hexdigest()
                else:
                    path.touch()
                    os.truncate(
                        path,
                        item.get(
                            "expected_file_size",
                            item.get("manifest_size", item.get("size", 1)),
                        ),
                    )
    qwen = next(item for item in dependencies["ollama_models"] if item["id"] == "qwen2.5-1.5b")
    for index, blob in enumerate(qwen["blobs"]):
        content = f"verified qwen fixture blob:{index}".encode()
        digest = hashlib.sha256(content).hexdigest()
        blob["digest"] = f"sha256:{digest}"
        blob["size"] = len(content)
        path = data_root / "models" / "ollama" / "blobs" / f"sha256-{digest}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    dependencies_path.write_text(json.dumps(dependencies), encoding="utf-8")

    ffmpeg_dir = data_root / "app" / "tools" / "ffmpeg" / "8.0" / "bin"
    ffmpeg_sha = _write_binary(ffmpeg_dir / "ffmpeg", b"verified ffmpeg")
    ffprobe_sha = _write_binary(ffmpeg_dir / "ffprobe", b"verified ffprobe")
    install_state = data_root / "runtime" / "install-state.json"
    install_state.parent.mkdir(parents=True, exist_ok=True)
    install_state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ffmpeg": {
                    "version": "8.0",
                    "directory": "tools/ffmpeg/8.0/bin",
                    "sha256": {"ffmpeg": ffmpeg_sha, "ffprobe": ffprobe_sha},
                },
                "ollama_models": {
                    "qwen2.5-1.5b": {
                        "verified": True,
                        "manifest_sha256": qwen["manifest_sha256"],
                        "manifest_size": qwen["manifest_size"],
                        "manifest_media_type": qwen["manifest_media_type"],
                        "blobs": qwen["blobs"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return data_root


def _write_binary(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return hashlib.sha256(content).hexdigest()


def _prefix(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(32)


def _installed_tree(test_root: Path) -> tuple[Path, Path]:
    candidate = _release_tree(test_root)
    data_root = _dependency_tree(test_root, candidate)
    installed = data_root / "app" / "releases" / "0.1.0"
    installed.parent.mkdir(parents=True)
    candidate.rename(installed)
    (data_root / "app" / "current").symlink_to(Path("releases") / "0.1.0")
    extension = data_root / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text('{"version":"0.1.0"}\n', encoding="utf-8")
    token = data_root / "config" / "api-token"
    token.parent.mkdir()
    token.write_text("LVT_TEST_SECRET_" + "x" * 48, encoding="utf-8")
    token.chmod(0o600)
    for name in ("exports", "logs", "work"):
        (data_root / name).mkdir()
    database = data_root / "db" / "lvt.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE healthcheck (value INTEGER)")
    return data_root, installed


def _run(
    tool: Path,
    *arguments: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tool), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _validate_schema(instance: Any, schema: dict[str, Any]) -> None:
    if "const" in schema:
        assert instance == schema["const"]
    if "enum" in schema:
        assert instance in schema["enum"]
    expected_type = schema.get("type")
    if expected_type == "object":
        assert isinstance(instance, dict)
        assert set(schema.get("required", ())) <= set(instance)
        if schema.get("additionalProperties") is False:
            assert set(instance) <= set(schema.get("properties", {}))
        for key, value in instance.items():
            _validate_schema(value, schema["properties"][key])
    elif expected_type == "array":
        assert isinstance(instance, list)
        for value in instance:
            _validate_schema(value, schema["items"])
    elif expected_type == "string":
        assert isinstance(instance, str)
    elif expected_type == "integer":
        assert type(instance) is int
    elif expected_type == "boolean":
        assert type(instance) is bool


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _walk_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _walk_strings(child)]
    return []


def test_healthy_doctor_json_matches_schema_and_is_redacted(tmp_path: Path) -> None:
    data_root, installed = _installed_tree(tmp_path)
    fake_bin = _fake_path(tmp_path)
    environment = _base_environment(tmp_path, fake_bin)
    environment["LVT_TEST_SECRET"] = "LVT_TEST_SECRET_" + "x" * 48
    environment["LVT_TEST_URL"] = "https://example.invalid/file?token=do-not-print"

    completed = _run(
        DOCTOR,
        "--json",
        "--data-root",
        str(data_root),
        "--release-root",
        str(installed),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    _validate_schema(payload, json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert payload["status"] == "healthy"
    assert payload["exit_code"] == 0
    rendered = "\n".join(_walk_strings(payload))
    assert environment["LVT_TEST_SECRET"] not in rendered
    assert "do-not-print" not in rendered
    assert str(tmp_path) not in rendered
    assert "traceback" not in rendered.lower()


def test_installed_doctor_uses_app_owned_python_and_ffmpeg(tmp_path: Path) -> None:
    data_root, installed = _installed_tree(tmp_path)
    fake_bin = _fake_path(tmp_path, missing={"python3", "ffmpeg", "ffprobe"})
    environment = _base_environment(tmp_path, fake_bin)

    completed = _run(
        DOCTOR,
        "--json",
        "--data-root",
        str(data_root),
        "--release-root",
        str(installed),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("missing", "expected_code"),
    [
        ({"python3"}, "PYTHON_MISSING"),
        ({"ffmpeg"}, "FFMPEG_MISSING"),
        ({"ollama"}, "OLLAMA_MISSING"),
    ],
)
def test_fake_path_missing_dependencies_return_warning(
    tmp_path: Path, missing: set[str], expected_code: str
) -> None:
    release = _release_tree(tmp_path)
    fake_bin = _fake_path(tmp_path, missing=missing)
    environment = _base_environment(tmp_path, fake_bin)

    completed = _run(
        DOCTOR,
        "--json",
        "--phase",
        "staging-core",
        "--release-root",
        str(release),
        environment=environment,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "warning"
    assert expected_code in {check["code"] for check in payload["checks"]}


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"LVT_TEST_ARCH": "x86_64"}, "ARCH_UNSUPPORTED"),
        ({"LVT_TEST_MACOS_VERSION": "12.6"}, "MACOS_UNSUPPORTED"),
        ({"LVT_TEST_ROSETTA": "1"}, "ROSETTA_UNSUPPORTED"),
    ],
)
def test_unsupported_platform_returns_failure(
    tmp_path: Path, overrides: dict[str, str], expected_code: str
) -> None:
    release = _release_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    environment.update(overrides)

    completed = _run(
        DOCTOR,
        "--json",
        "--phase",
        "staging-core",
        "--release-root",
        str(release),
        environment=environment,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "failed"
    assert expected_code in {check["code"] for check in payload["checks"]}


@pytest.mark.parametrize(
    ("free_bytes", "expected_exit"),
    [(8 * GIB, 0), (8 * GIB - 1, 1)],
)
def test_disk_boundary_is_deterministic(
    tmp_path: Path, free_bytes: int, expected_exit: int
) -> None:
    release = _release_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    environment["LVT_TEST_DISK_BYTES"] = str(free_bytes)

    completed = _run(
        DOCTOR,
        "--json",
        "--phase",
        "staging-core",
        "--release-root",
        str(release),
        environment=environment,
    )

    assert completed.returncode == expected_exit


def test_unowned_11435_port_is_unsafe_and_11434_is_never_probed(tmp_path: Path) -> None:
    data_root, installed = _installed_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    environment["LVT_TEST_OLLAMA_PORT"] = "occupied"
    environment["LVT_TEST_FORBID_PORT"] = "11434"

    completed = _run(
        DOCTOR,
        "--json",
        "--phase",
        "installed-prerequisites",
        "--data-root",
        str(data_root),
        "--release-root",
        str(installed),
        environment=environment,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert "OLLAMA_PORT_UNOWNED" in {check["code"] for check in payload["checks"]}


def test_validator_phase_boundaries_do_not_form_current_backend_cycle(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    data_root = _dependency_tree(tmp_path, release)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    (data_root / "app").mkdir(exist_ok=True)
    (data_root / "app" / "current").symlink_to("current")

    staging = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "staging-core",
        "--release-root",
        str(release),
        "--data-root",
        str(data_root),
        environment=environment,
    )
    dependencies = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "dependencies",
        "--release-root",
        str(release),
        "--data-root",
        str(data_root),
        environment=environment,
    )

    assert staging.returncode == 0, staging.stderr
    assert dependencies.returncode == 0, dependencies.stderr

    missing_current = tmp_path / "missing install"
    runtime = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "runtime-full",
        "--data-root",
        str(missing_current),
        environment=environment,
    )
    assert runtime.returncode != 0
    assert json.loads(runtime.stdout)["status"] != "healthy"


def test_installed_prerequisites_does_not_require_backend_health(tmp_path: Path) -> None:
    data_root, installed = _installed_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    environment["LVT_TEST_BACKEND_HEALTH"] = "forbidden"
    environment["LVT_TEST_OLLAMA_PORT"] = "free"

    completed = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "installed-prerequisites",
        "--data-root",
        str(data_root),
        "--release-root",
        str(installed),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr


def test_dependencies_reject_missing_qwen_blob_and_drifted_state(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    data_root = _dependency_tree(tmp_path, release)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    blob = next((data_root / "models" / "ollama" / "blobs").iterdir())
    blob.unlink()

    missing_blob = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "dependencies",
        "--data-root",
        str(data_root),
        "--release-root",
        str(release),
        environment=environment,
    )
    assert missing_blob.returncode == 1

    blob.touch()
    dependencies = json.loads((release / "packaging" / "dependencies.json").read_text())
    qwen = next(item for item in dependencies["ollama_models"] if item["id"] == "qwen2.5-1.5b")
    expected_blob = next(
        item for item in qwen["blobs"] if blob.name == f"sha256-{item['digest'][7:]}"
    )
    os.truncate(blob, expected_blob["size"])
    state_path = data_root / "runtime" / "install-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["ollama_models"]["qwen2.5-1.5b"]["manifest_size"] = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")

    drifted_state = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "dependencies",
        "--data-root",
        str(data_root),
        "--release-root",
        str(release),
        environment=environment,
    )
    assert drifted_state.returncode == 2
    assert "QWEN_INTEGRITY_FAILED" in {
        check["code"] for check in json.loads(drifted_state.stdout)["checks"]
    }


def test_segmentation_manifest_separates_archive_and_extracted_file_contract() -> None:
    dependencies = json.loads(
        (ROOT / "packaging" / "dependencies.json").read_text(encoding="utf-8")
    )
    segmentation = next(
        item for item in dependencies["artifacts"] if item["id"] == "diarization-segmentation"
    )

    assert segmentation["size"] == 6_958_444
    assert (
        segmentation["sha256"] == "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"
    )
    assert segmentation["expected_file_size"] == 5_992_913
    assert (
        segmentation["expected_file_sha256"]
        == "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"
    )
    assert segmentation["expected_files"] == ["models/diarization/segmentation/model.onnx"]


def test_dependencies_reject_segmentation_archive_size_as_extracted_size(
    tmp_path: Path,
) -> None:
    release = _release_tree(tmp_path)
    data_root = _dependency_tree(tmp_path, release)
    segmentation = data_root / "models/diarization/segmentation/model.onnx"
    os.truncate(segmentation, 6_958_444)

    completed = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "dependencies",
        "--data-root",
        str(data_root),
        "--release-root",
        str(release),
        environment=_base_environment(tmp_path, _fake_path(tmp_path)),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert any(
        check["id"] == "model_diarization-segmentation" and check["code"] == "MODEL_SIZE_INVALID"
        for check in payload["checks"]
    )


def test_dependencies_reject_segmentation_digest_drift(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    data_root = _dependency_tree(tmp_path, release)
    segmentation = data_root / "models/diarization/segmentation/model.onnx"
    content = bytearray(segmentation.read_bytes())
    content[0] ^= 1
    segmentation.write_bytes(content)

    completed = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "dependencies",
        "--data-root",
        str(data_root),
        "--release-root",
        str(release),
        environment=_base_environment(tmp_path, _fake_path(tmp_path)),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert any(
        check["id"] == "model_diarization-segmentation" and check["code"] == "MODEL_DIGEST_INVALID"
        for check in payload["checks"]
    )


def test_dependencies_keep_direct_file_size_fallback(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    data_root = _dependency_tree(tmp_path, release)
    embedding = data_root / "models/diarization/embedding/nemo_en_titanet_small.onnx"
    os.truncate(embedding, embedding.stat().st_size - 1)

    completed = _run(
        VERIFY_INSTALL,
        "--json",
        "--phase",
        "dependencies",
        "--data-root",
        str(data_root),
        "--release-root",
        str(release),
        environment=_base_environment(tmp_path, _fake_path(tmp_path)),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert any(
        check["id"] == "model_diarization-embedding" and check["code"] == "MODEL_SIZE_INVALID"
        for check in payload["checks"]
    )


def test_doctor_is_read_only_for_token_database_and_models(tmp_path: Path) -> None:
    data_root, installed = _installed_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    protected = [
        data_root / "config" / "api-token",
        data_root / "db" / "lvt.sqlite3",
        *[path for path in (data_root / "models").rglob("*") if path.is_file()],
    ]
    before = {
        path: (
            path.stat().st_ino,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _prefix(path),
        )
        for path in protected
    }

    completed = _run(
        DOCTOR,
        "--json",
        "--data-root",
        str(data_root),
        "--release-root",
        str(installed),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr
    after = {
        path: (
            path.stat().st_ino,
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _prefix(path),
        )
        for path in protected
    }
    assert after == before


def test_symlink_or_unwritable_data_root_fails_closed(tmp_path: Path) -> None:
    real_root, installed = _installed_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    symlink_root = tmp_path / "linked-data"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    linked = _run(
        DOCTOR,
        "--json",
        "--data-root",
        str(symlink_root),
        "--release-root",
        str(installed),
        environment=environment,
    )
    assert linked.returncode == 2

    (real_root / "logs").chmod(0o500)
    unwritable = _run(
        DOCTOR,
        "--json",
        "--data-root",
        str(real_root),
        "--release-root",
        str(installed),
        environment=environment,
    )
    assert unwritable.returncode == 2
    assert "DIRECTORY_PERMISSION_UNSAFE" in {
        check["code"] for check in json.loads(unwritable.stdout)["checks"]
    }


def test_default_data_root_rejects_symlinked_home(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    data_root = real_home / "Library" / "Application Support" / "LocalVideoTranscriber"
    data_root.mkdir(parents=True)
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    environment = _base_environment(tmp_path, _fake_path(tmp_path))
    environment["HOME"] = str(linked_home)

    completed = _run(
        DOCTOR,
        "--json",
        environment=environment,
    )

    assert completed.returncode == 2
    assert "ROOT_PATH_UNSAFE" in {check["code"] for check in json.loads(completed.stdout)["checks"]}


def test_human_output_is_stable_chinese_without_absolute_paths(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    environment = _base_environment(tmp_path, _fake_path(tmp_path, missing={"ollama"}))

    completed = _run(
        DOCTOR,
        "--phase",
        "staging-core",
        "--release-root",
        str(release),
        environment=environment,
    )

    assert completed.returncode == 1
    assert "状态：需要处理" in completed.stdout
    assert "OLLAMA_MISSING" in completed.stdout
    assert str(tmp_path) not in completed.stdout + completed.stderr
    assert not re.search(r"Traceback|/[A-Za-z0-9_.-]+/", completed.stdout + completed.stderr)
