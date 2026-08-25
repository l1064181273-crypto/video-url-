from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROVISION = ROOT / "packaging/tools/provision.py"
INSTALL = ROOT / "packaging/tools/install.py"
VERIFY = ROOT / "packaging/tools/verify_install.py"
LOCK = ROOT / "packaging/tools/lifecycle_lock.py"
DOCTOR = ROOT / "packaging/tools/doctor.py"
RECONCILE = ROOT / "packaging/tools/reconcile_processes.py"
SUPERVISOR = ROOT / "packaging/tools/tool_supervisor.py"
INSTALL_COMMAND = ROOT / "scripts/install.command"
COMMON = ROOT / "scripts/lib/common.zsh"
DOWNLOAD = ROOT / "scripts/lib/download.zsh"
PROCESS = ROOT / "scripts/lib/process.zsh"
MODELFILE = ROOT / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km"
FIXTURE_ROOT = ROOT / "packaging/tests/fixtures"
DOWNLOAD_FIXTURE_ROOT = FIXTURE_ROOT / "download-server"
FAKE_RELEASE_ROOT = FIXTURE_ROOT / "fake-release"

server_spec = importlib.util.spec_from_file_location(
    "lvt_download_fixture",
    DOWNLOAD_FIXTURE_ROOT / "server.py",
)
assert server_spec is not None and server_spec.loader is not None
server_module = importlib.util.module_from_spec(server_spec)
sys.modules[server_spec.name] = server_module
server_spec.loader.exec_module(server_module)
DownloadFixture = server_module.DownloadFixture


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _macho_arm64(label: bytes) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + label


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100755 << 16
            archive.writestr(info, content)
    return stream.getvalue()


def _segmentation_archive(
    model: bytes,
    *,
    unsafe: str | None = None,
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:bz2") as archive:
        model_info = tarfile.TarInfo("sherpa-onnx-pyannote-segmentation-3-0/model.onnx")
        model_info.size = len(model)
        archive.addfile(model_info, io.BytesIO(model))
        if unsafe == "traversal":
            bad = tarfile.TarInfo("../../escape")
            bad.size = 1
            archive.addfile(bad, io.BytesIO(b"x"))
        elif unsafe == "symlink":
            bad = tarfile.TarInfo("unsafe-link")
            bad.type = tarfile.SYMTYPE
            bad.linkname = "/tmp/escape"
            archive.addfile(bad)
    return stream.getvalue()


def _artifact(
    identifier: str,
    content: bytes,
    expected_file: str,
    *,
    media_type: str = "application/octet-stream",
    version: str = "fixture-1",
    expected_file_size: int | None = None,
    expected_file_sha256: str | None = None,
) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "id": identifier,
        "kind": "model",
        "version": version,
        "architecture": "arm64",
        "url": f"https://fixtures.invalid/files/{identifier}",
        "sha256": _sha256(content),
        "size": len(content),
        "media_type": media_type,
        "license": "MIT",
        "license_url": "https://fixtures.invalid/licenses/fixed",
        "expected_files": [expected_file],
    }
    if expected_file_size is not None:
        artifact["expected_file_size"] = expected_file_size
    if expected_file_sha256 is not None:
        artifact["expected_file_sha256"] = expected_file_sha256
    return artifact


def _fixture_contract(
    *,
    segmentation_model: bytes = b"segmentation-model-v1",
    unsafe_archive: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    ffmpeg_archive = _zip_bytes(
        {
            "darwin_arm64/ffmpeg": _macho_arm64(b"ffmpeg"),
            "darwin_arm64/ffprobe": _macho_arm64(b"ffprobe"),
        }
    )
    ollama_archive = _zip_bytes(
        {
            "Ollama.app/Contents/MacOS/Ollama": _macho_arm64(b"app"),
            "Ollama.app/Contents/Resources/ollama": _macho_arm64(b"cli"),
        }
    )
    segmentation = _segmentation_archive(
        segmentation_model,
        unsafe=unsafe_archive,
    )
    config = b'{"model_type":"whisper"}\n'
    weights = b"mlx-weights"
    embedding = b"embedding-onnx"
    hy_model = b"hy-mt2-gguf"
    fixture_dependencies = json.loads(
        (FAKE_RELEASE_ROOT / "packaging/dependencies.json").read_text(encoding="utf-8")
    )
    artifacts = [
        *fixture_dependencies["artifacts"],
        {
            **_artifact(
                "ffmpeg",
                ffmpeg_archive,
                "darwin_arm64/ffmpeg",
                media_type="application/zip",
                version="8.0",
            ),
            "expected_files": ["darwin_arm64/ffmpeg", "darwin_arm64/ffprobe"],
            "kind": "tool",
        },
        {
            **_artifact(
                "ollama",
                ollama_archive,
                "Ollama.app/Contents/Resources/ollama",
                media_type="application/zip",
                version="0.32.15",
            ),
            "expected_files": [
                "Ollama.app/Contents/MacOS/Ollama",
                "Ollama.app/Contents/Resources/ollama",
            ],
            "kind": "tool",
            "executable": "Ollama.app/Contents/Resources/ollama",
        },
        _artifact(
            "asr-whisper-small-mlx-config",
            config,
            "models/asr/whisper-small-mlx/config.json",
        ),
        _artifact(
            "asr-whisper-small-mlx-weights",
            weights,
            "models/asr/whisper-small-mlx/weights.npz",
        ),
        _artifact(
            "diarization-segmentation",
            segmentation,
            "models/diarization/segmentation/model.onnx",
            media_type="application/x-bzip2",
            expected_file_size=len(segmentation_model),
            expected_file_sha256=_sha256(segmentation_model),
        ),
        _artifact(
            "diarization-embedding",
            embedding,
            "models/diarization/embedding/nemo_en_titanet_small.onnx",
            media_type="application/onnx",
        ),
        _artifact(
            "hy-mt2",
            hy_model,
            f"models/ollama/blobs/sha256-{_sha256(hy_model)}",
        ),
    ]
    blob_contents = [
        b'{"model_format":"gguf"}',
        b"qwen-model",
        b"qwen-system",
        b"qwen-template",
        b"qwen-license",
    ]
    media_types = [
        "application/vnd.docker.container.image.v1+json",
        "application/vnd.ollama.image.model",
        "application/vnd.ollama.image.system",
        "application/vnd.ollama.image.template",
        "application/vnd.ollama.image.license",
    ]
    blobs = [
        {
            "digest": f"sha256:{_sha256(content)}",
            "media_type": media_type,
            "size": len(content),
        }
        for content, media_type in zip(blob_contents, media_types, strict=True)
    ]
    qwen_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": blobs[0]["media_type"],
            "digest": blobs[0]["digest"],
            "size": blobs[0]["size"],
        },
        "layers": [
            {
                "mediaType": blob["media_type"],
                "digest": blob["digest"],
                "size": blob["size"],
            }
            for blob in blobs[1:]
        ],
    }
    qwen_bytes = json.dumps(
        qwen_manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    qwen = {
        "id": "qwen2.5-1.5b",
        "kind": "model",
        "version": "1.5b",
        "architecture": "arm64",
        "manifest_url": "https://fixtures.invalid/v2/library/qwen2.5/manifests/1.5b",
        "manifest_sha256": _sha256(qwen_bytes),
        "manifest_size": len(qwen_bytes),
        "manifest_media_type": qwen_manifest["mediaType"],
        "blobs": blobs,
        "license": "Apache-2.0",
        "license_url": "https://fixtures.invalid/licenses/qwen",
        "expected_files": ["models/ollama/manifests/registry.ollama.ai/library/qwen2.5/1.5b"],
    }
    dependencies = {
        "schema_version": 1,
        "target": "macos-arm64",
        "trust_policy": {
            "allowed_schemes": ["https"],
            "allowed_architectures": ["arm64"],
            "allow_floating_tags": False,
            "allow_runtime_digest_rewrite": False,
        },
        "artifacts": artifacts,
        "ollama_models": [qwen],
    }
    files = {
        "files/ffmpeg": ffmpeg_archive,
        "files/ollama": ollama_archive,
        "files/asr-whisper-small-mlx-config": config,
        "files/asr-whisper-small-mlx-weights": weights,
        "files/diarization-segmentation": segmentation,
        "files/diarization-embedding": embedding,
        "files/hy-mt2": hy_model,
        "v2/library/qwen2.5/manifests/1.5b": qwen_bytes,
    }
    for blob, content in zip(blobs, blob_contents, strict=True):
        files[f"v2/library/qwen2.5/blobs/{blob['digest']}"] = content
    return dependencies, files


def _copy(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if executable:
        destination.chmod(0o755)


def _candidate_tree(
    tmp_path: Path,
    dependencies: dict[str, Any],
) -> tuple[Path, Path]:
    data_root = tmp_path / "测试 数据" / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.0"
    (release / "backend/src/lvt").mkdir(parents=True)
    (release / "backend/pyproject.toml").write_text(
        '[project]\nname="local-video-transcriber"\nversion="0.1.0"\n',
        encoding="utf-8",
    )
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {sys.executable!r} "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    doctor = release / "scripts/doctor.command"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    doctor.chmod(0o755)
    _copy(VERIFY, release / "packaging/tools/verify_install.py", executable=True)
    _copy(PROVISION, release / "packaging/tools/provision.py", executable=True)
    _copy(LOCK, release / "packaging/tools/lifecycle_lock.py", executable=True)
    _copy(COMMON, release / "scripts/lib/common.zsh", executable=True)
    _copy(PROCESS, release / "scripts/lib/process.zsh", executable=True)
    _copy(MODELFILE, release / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km")
    manifest = release / "packaging/dependencies.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(dependencies), encoding="utf-8")
    state = data_root / "runtime/install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "core": {
                    "release": "app/releases/0.1.0",
                    "verified": True,
                    "version": "0.1.0",
                },
            }
        ),
        encoding="utf-8",
    )
    for name in ("models", "config", "db", "work", "exports", "logs"):
        (data_root / name).mkdir(parents=True, exist_ok=True)
    return data_root, release


def _environment(
    tmp_path: Path,
    fixture: Any,
    *,
    behavior: dict[str, str] | None = None,
    create_fail: bool = False,
) -> dict[str, str]:
    (tmp_path / ".lvt-provision-test-root").write_text(
        "lvt-provision-test-root-v1\n",
        encoding="utf-8",
    )
    fixture_root = tmp_path / "provision-fixtures"
    fake_ollama = fixture_root / "fake-ollama"
    _copy(DOWNLOAD_FIXTURE_ROOT / "fake_ollama.py", fake_ollama, executable=True)
    download_library = fixture_root / "download.zsh"
    _copy(DOWNLOAD_FIXTURE_ROOT / "download.zsh", download_library)
    origin_marker = fixture_root / "download-origin.txt"
    origin_marker.write_text(f"{fixture.origin}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LVT_TEST_ROOT": str(tmp_path),
            "LVT_TEST_DOWNLOAD_ORIGIN": fixture.origin,
            "LVT_TEST_DOWNLOAD_ORIGIN_MARKER": str(origin_marker),
            "LVT_TEST_DOWNLOAD_LIBRARY": str(download_library),
            "LVT_TEST_OLLAMA_EXECUTABLE": str(fake_ollama),
            "LVT_TEST_OLLAMA_STATE": str(tmp_path / "ollama-models.txt"),
            "LVT_TEST_DOWNLOAD_BEHAVIOR": json.dumps(behavior or {}),
        }
    )
    if create_fail:
        environment["LVT_TEST_OLLAMA_CREATE_FAIL"] = "1"
    return environment


def _run_provision(
    data_root: Path,
    release: Path,
    environment: dict[str, str],
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(release / "packaging/tools/provision.py"),
            "--phase",
            "dependencies",
            "--data-root",
            str(data_root),
            "--release-root",
            str(release),
            *extra,
        ],
        cwd="/",
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_dependencies_verify(
    data_root: Path,
    release: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--json",
            "--phase",
            "dependencies",
            "--data-root",
            str(data_root),
            "--release-root",
            str(release),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_port(port: int, *, in_use: bool) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            active = probe.connect_ex(("127.0.0.1", port)) == 0
        if active is in_use:
            return
        time.sleep(0.02)
    raise AssertionError(f"port {port} did not become {'used' if in_use else 'free'}")


@contextmanager
def _occupied_port(port: int) -> Iterator[None]:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
    thread = None
    try:
        yield
    finally:
        server.close()
        if thread is not None:
            thread.join(timeout=1)


def _run_fixture_download(
    fixture: Any,
    tmp_path: Path,
    behavior: str,
    content: bytes,
    *,
    expected_sha256: str | None = None,
) -> subprocess.CompletedProcess[str]:
    root = tmp_path / f"download-{behavior}"
    root.mkdir(exist_ok=True)
    return subprocess.run(
        [
            "/bin/zsh",
            "-c",
            'source "$1"; lvt_download_verified "$2" "$3" "$4" "$5" "$6"',
            "fixture-download",
            str(DOWNLOAD_FIXTURE_ROOT / "download.zsh"),
            f"{fixture.origin}/{behavior}/payload",
            str(root),
            "result.bin",
            expected_sha256 or _sha256(content),
            str(len(content)),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_download_fixture_normal_and_resumes_partial(
    tmp_path: Path,
) -> None:
    content = b"0123456789" * 64
    with DownloadFixture({"payload": content}) as fixture:
        normal = _run_fixture_download(fixture, tmp_path, "normal", content)
        first_resume = _run_fixture_download(fixture, tmp_path, "resume", content)
        second_resume = _run_fixture_download(fixture, tmp_path, "resume", content)

    assert normal.returncode == 0
    assert first_resume.returncode != 0
    assert second_resume.returncode == 0
    assert any(header is not None for path, header in fixture.ranges if "/resume/" in path)


@pytest.mark.parametrize(
    ("behavior", "digest"),
    [
        ("truncate", None),
        ("corrupt", "correct"),
        ("redirect-http", None),
    ],
)
def test_download_fixture_rejects_untrusted_or_invalid_responses(
    tmp_path: Path,
    behavior: str,
    digest: str | None,
) -> None:
    content = b"verified fixture payload"
    expected = _sha256(content) if digest == "correct" else None
    with DownloadFixture({"payload": content}) as fixture:
        completed = _run_fixture_download(
            fixture,
            tmp_path,
            behavior,
            content,
            expected_sha256=expected,
        )

    assert completed.returncode != 0


def test_provision_builds_healthy_dependencies_candidate(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "INSTALL_DEPENDENCIES_READY" in completed.stdout
    verified = _run_dependencies_verify(data_root, release)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["status"] == "healthy"
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert set(state) == {"schema_version", "core", "ffmpeg", "ollama_models"}
    assert not (data_root / "app/current").exists()
    assert not (data_root / "extension/manifest.json").exists()


def test_dependencies_verify_rejects_same_size_qwen_blob_corruption(
    tmp_path: Path,
) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    digest = dependencies["ollama_models"][0]["blobs"][0]["digest"][7:]
    blob = data_root / "models/ollama/blobs" / f"sha256-{digest}"
    content = bytearray(blob.read_bytes())
    content[0] ^= 0x01
    blob.write_bytes(content)

    verified = _run_dependencies_verify(data_root, release)
    report = json.loads(verified.stdout)
    qwen_check = next(check for check in report["checks"] if check["id"] == "qwen_blob_0")
    assert verified.returncode != 0
    assert qwen_check["code"] == "MODEL_DIGEST_INVALID"
    assert "摘要" in qwen_check["message"]


@pytest.mark.parametrize("unsafe", ["traversal", "symlink"])
def test_segmentation_archive_rejects_unsafe_members(
    tmp_path: Path,
    unsafe: str,
) -> None:
    dependencies, files = _fixture_contract(unsafe_archive=unsafe)
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 2
    assert not (data_root / "models/diarization/segmentation/model.onnx").exists()
    assert any((data_root / "models/quarantine").iterdir())


@pytest.mark.parametrize("drift", ["size", "digest"])
def test_segmentation_extracted_contract_is_enforced(
    tmp_path: Path,
    drift: str,
) -> None:
    expected = b"segmentation-model-v1"
    actual = b"x" * 6_958_444 if drift == "size" else b"X" + expected[1:]
    dependencies, files = _fixture_contract(segmentation_model=actual)
    segmentation = next(
        item for item in dependencies["artifacts"] if item["id"] == "diarization-segmentation"
    )
    segmentation["expected_file_size"] = len(expected)
    segmentation["expected_file_sha256"] = _sha256(expected)
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 2
    assert not (data_root / "models/diarization/segmentation/model.onnx").exists()
    assert any((data_root / "models/quarantine").iterdir())


def test_idempotent_cache_and_single_file_repair(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        environment = _environment(tmp_path, fixture)
        first = _run_provision(data_root, release, environment)
        first_counts = fixture.counts.copy()
        next((data_root / "app/downloads").glob("ffmpeg-*")).unlink()
        second = _run_provision(data_root, release, environment)
        second_counts = fixture.counts.copy()
        embedding = data_root / "models/diarization/embedding/nemo_en_titanet_small.onnx"
        embedding.write_bytes(b"broken")
        repaired = _run_provision(data_root, release, environment)

    assert first.returncode == second.returncode == repaired.returncode == 0
    assert second_counts == first_counts
    changed = {
        path for path, count in fixture.counts.items() if count != second_counts.get(path, 0)
    }
    assert changed == {"/normal/files/diarization-embedding"}


@pytest.mark.parametrize(
    "behavior",
    [
        pytest.param({"qwen-manifest": "corrupt"}, id="mutable-tag-new-manifest"),
        pytest.param({"qwen-blob-0": "corrupt"}, id="blob-checksum"),
        pytest.param({"qwen-blob-2": "truncate"}, id="blob-size"),
    ],
)
def test_qwen_integrity_failures_are_quarantined(
    tmp_path: Path,
    behavior: dict[str, str],
) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture, behavior=behavior),
        )

    assert completed.returncode == 2
    qwen_manifest = data_root / "models/ollama/manifests/registry.ollama.ai/library/qwen2.5/1.5b"
    assert not qwen_manifest.exists()
    assert any((data_root / "models/quarantine").iterdir())
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert set(state) == {"schema_version", "core"}
    assert all(
        path.is_relative_to(data_root / "models/quarantine")
        for path in data_root.rglob("*.partial*")
    )


def test_qwen_incorrect_pinned_manifest_digest_is_quarantined(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    dependencies["ollama_models"][0]["manifest_sha256"] = "0" * 64
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 2
    assert any((data_root / "models/quarantine").iterdir())


def test_missing_qwen_blob_is_quarantined(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    qwen = dependencies["ollama_models"][0]
    missing = qwen["blobs"][4]["digest"]
    files.pop(f"v2/library/qwen2.5/blobs/{missing}")
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 2
    assert any((data_root / "models/quarantine").iterdir())


def test_occupied_11435_fails_and_11434_is_untouched(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with _occupied_port(11434), _occupied_port(11435), DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 2
    assert "11434" not in completed.stdout + completed.stderr


def test_user_ollama_on_11434_does_not_affect_provision(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with _occupied_port(11434), DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_external_ollama_compatible_daemon_on_11435_fails_untouched(
    tmp_path: Path,
) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        environment = _environment(tmp_path, fixture)
        daemon_environment = {
            "HOME": environment.get("HOME", "/"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "OLLAMA_HOST": "127.0.0.1:11435",
            "OLLAMA_MODELS": str(data_root / "models/ollama"),
            "LVT_TEST_ROOT": environment["LVT_TEST_ROOT"],
            "LVT_TEST_OLLAMA_STATE": environment["LVT_TEST_OLLAMA_STATE"],
        }
        state_path = Path(environment["LVT_TEST_OLLAMA_STATE"])
        state_path.write_text("external-sentinel\n", encoding="utf-8")
        daemon = subprocess.Popen(
            [environment["LVT_TEST_OLLAMA_EXECUTABLE"], "serve"],
            env=daemon_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_port(11435, in_use=True)
            completed = _run_provision(data_root, release, environment)
            assert daemon.poll() is None
            assert state_path.read_text(encoding="utf-8") == "external-sentinel\n"
        finally:
            daemon.terminate()
            daemon.wait(timeout=5)

    assert completed.returncode == 2
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert set(state) == {"schema_version", "core"}


def test_missing_asr_runtime_package_fails_closed(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        environment = _environment(tmp_path, fixture)
        environment["LVT_TEST_MISSING_PACKAGE"] = "mlx_whisper"
        completed = _run_provision(data_root, release, environment)

    assert completed.returncode == 2
    assert "INSTALL_DEPENDENCIES_READY" not in completed.stdout


def test_primary_model_create_failure_is_not_reported_ready(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture, create_fail=True),
        )

    assert completed.returncode == 2
    assert "INSTALL_DEPENDENCIES_READY" not in completed.stdout


def test_skip_models_is_explicitly_incomplete(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
            "--skip-models",
        )

    assert completed.returncode == 1
    assert "INSTALL_DEPENDENCIES_INCOMPLETE" in completed.stdout
    assert "INSTALL_DEPENDENCIES_READY" not in completed.stdout


def test_logs_argv_and_audit_do_not_expose_secrets(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    secret = "LVT_TEST_SECRET_" + "x" * 48
    with DownloadFixture(files) as fixture:
        environment = _environment(tmp_path, fixture)
        audit = tmp_path / "ollama-audit.json"
        environment.update(
            {
                "LVT_TOKEN": secret,
                "OPENAI_API_KEY": "openai-" + secret,
                "LVT_TEST_PROTECTED_URL": f"https://example.invalid/file?token={secret}",
                "LVT_TEST_OLLAMA_AUDIT": str(audit),
            }
        )
        completed = _run_provision(data_root, release, environment)

    transcript = completed.stdout + completed.stderr + audit.read_text(encoding="utf-8")
    child_environments = list(data_root.rglob("download-child-environment.txt"))
    assert completed.returncode == 0
    assert child_environments
    for child_environment in child_environments:
        assert child_environment.read_text(encoding="utf-8") == (
            "LVT_TOKEN=<unset>\nOPENAI_API_KEY=<unset>\n"
        )
    assert secret not in transcript
    assert "?token=" not in transcript


def test_unrelated_test_root_rejects_all_injections(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    unrelated = tmp_path / "unrelated-test-root"
    unrelated.mkdir()
    (unrelated / ".lvt-provision-test-root").write_text(
        "lvt-provision-test-root-v1\n",
        encoding="utf-8",
    )
    with DownloadFixture(files) as fixture:
        environment = _environment(tmp_path, fixture)
        environment["LVT_TEST_ROOT"] = str(unrelated)
        completed = _run_provision(data_root, release, environment)

    assert completed.returncode == 2
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert set(state) == {"schema_version", "core"}
    assert not list(data_root.rglob("*.partial*"))
    assert not list(data_root.rglob(".verified"))


@pytest.mark.parametrize(
    "setting",
    [
        "LVT_TEST_DOWNLOAD_LIBRARY",
        "LVT_TEST_DOWNLOAD_ORIGIN_MARKER",
        "LVT_TEST_OLLAMA_EXECUTABLE",
        "LVT_TEST_OLLAMA_STATE",
        "LVT_TEST_OLLAMA_AUDIT",
    ],
)
def test_path_injection_outside_test_root_is_rejected(
    tmp_path: Path,
    setting: str,
) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    outside_root = tmp_path.with_name(f"{tmp_path.name}-outside")
    outside_root.mkdir()
    with DownloadFixture(files) as fixture:
        environment = _environment(tmp_path, fixture)
        paths = {
            "LVT_TEST_DOWNLOAD_LIBRARY": outside_root / "download.zsh",
            "LVT_TEST_DOWNLOAD_ORIGIN_MARKER": outside_root / "download-origin.txt",
            "LVT_TEST_OLLAMA_EXECUTABLE": outside_root / "fake-ollama",
            "LVT_TEST_OLLAMA_STATE": outside_root / "ollama-models.txt",
            "LVT_TEST_OLLAMA_AUDIT": outside_root / "ollama-audit.json",
        }
        _copy(DOWNLOAD_FIXTURE_ROOT / "download.zsh", paths["LVT_TEST_DOWNLOAD_LIBRARY"])
        _copy(
            DOWNLOAD_FIXTURE_ROOT / "fake_ollama.py",
            paths["LVT_TEST_OLLAMA_EXECUTABLE"],
            executable=True,
        )
        paths["LVT_TEST_DOWNLOAD_ORIGIN_MARKER"].write_text(
            f"{fixture.origin}\n",
            encoding="utf-8",
        )
        environment[setting] = str(paths[setting])
        try:
            completed = _run_provision(data_root, release, environment)
        finally:
            shutil.rmtree(outside_root)

    assert completed.returncode == 2
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert set(state) == {"schema_version", "core"}
    assert not list(data_root.rglob("*.partial*"))


def test_dependency_url_query_is_rejected_without_disclosure(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    secret = "protected-download-secret"
    artifact = next(
        item for item in dependencies["artifacts"] if item["id"] == "asr-whisper-small-mlx-config"
    )
    artifact["url"] += f"?token={secret}"
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )

    assert completed.returncode == 2
    assert secret not in completed.stdout + completed.stderr
    assert fixture.counts["/normal/files/asr-whisper-small-mlx-config"] == 0


def test_app_owned_ffmpeg_is_arm64_executable_and_runtime_never_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies, files = _fixture_contract()
    data_root, release = _candidate_tree(tmp_path, dependencies)
    with DownloadFixture(files) as fixture:
        completed = _run_provision(
            data_root,
            release,
            _environment(tmp_path, fixture),
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    ffmpeg_dir = data_root / "app" / state["ffmpeg"]["directory"]
    for name in ("ffmpeg", "ffprobe"):
        path = ffmpeg_dir / name
        assert stat.S_ISREG(path.lstat().st_mode)
        assert path.stat().st_mode & 0o111
        assert path.read_bytes()[4:8] == struct.pack("<I", 0x0100000C)
        assert _sha256(path.read_bytes()) == state["ffmpeg"]["sha256"][name]

    from lvt.engines import media

    monkeypatch.setitem(
        sys.modules,
        "static_ffmpeg",
        type(
            "Sentinel",
            (),
            {"add_paths": lambda: pytest.fail("runtime attempted static-ffmpeg download")},
        )(),
    )
    resolved = media.discover_ffmpeg_binaries(
        installed_mode=True,
        ffmpeg_dir=ffmpeg_dir,
        app_root=data_root / "app",
        install_state=data_root / "runtime/install-state.json",
    )
    assert resolved == (ffmpeg_dir / "ffmpeg", ffmpeg_dir / "ffprobe")


def _build_install_source(
    tmp_path: Path,
    dependencies: dict[str, Any],
) -> Path:
    release = tmp_path / "Release 源 naïve"
    shutil.copytree(FAKE_RELEASE_ROOT, release)
    (release / "packaging/dependencies.json").write_text(
        json.dumps(dependencies),
        encoding="utf-8",
    )
    extension = release / "extension/dist/manifest.json"
    extension.parent.mkdir(parents=True, exist_ok=True)
    extension.write_text(
        '{"manifest_version":3,"name":"fixture","version":"0.1.0"}\n',
        encoding="utf-8",
    )
    _copy(INSTALL_COMMAND, release / "scripts/install.command", executable=True)
    _copy(COMMON, release / "scripts/lib/common.zsh", executable=True)
    _copy(DOWNLOAD, release / "scripts/lib/download.zsh", executable=True)
    _copy(PROCESS, release / "scripts/lib/process.zsh", executable=True)
    _copy(INSTALL, release / "packaging/tools/install.py", executable=True)
    _copy(PROVISION, release / "packaging/tools/provision.py", executable=True)
    _copy(VERIFY, release / "packaging/tools/verify_install.py", executable=True)
    _copy(LOCK, release / "packaging/tools/lifecycle_lock.py", executable=True)
    _copy(DOCTOR, release / "packaging/tools/doctor.py", executable=True)
    _copy(RECONCILE, release / "packaging/tools/reconcile_processes.py", executable=True)
    _copy(SUPERVISOR, release / "packaging/tools/tool_supervisor.py", executable=True)
    _copy(MODELFILE, release / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km")
    (release / "scripts/doctor.command").chmod(0o755)
    (release / "test-tools/uv").chmod(0o755)
    (release / "test-tools/python/bin/python3").chmod(0o755)
    return release


def test_default_install_runs_staging_then_dependencies_without_publish(
    tmp_path: Path,
) -> None:
    dependencies, files = _fixture_contract()
    test_root = tmp_path / "安装 根"
    test_root.mkdir()
    release = _build_install_source(test_root, dependencies)
    home = tmp_path / "empty-home"
    home.mkdir()
    with DownloadFixture(files) as fixture:
        environment = _environment(test_root, fixture)
        environment.update(
            {
                "HOME": str(home),
                "LVT_TEST_ROOT": str(test_root),
                "LVT_PYTHON": sys.executable,
                "LVT_TEST_UV_SOURCE": str(release / "test-tools/uv"),
                "LVT_TEST_PYTHON_SOURCE": str(release / "test-tools/python/bin/python3"),
                "LVT_TEST_RUNTIME_PYTHON": sys.executable,
            }
        )
        completed = subprocess.run(
            [str(release / "scripts/install.command")],
            cwd="/",
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    data_root = test_root / "LocalVideoTranscriber"
    candidate = data_root / "app/releases/0.1.0"
    verified = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--json",
            "--phase",
            "dependencies",
            "--data-root",
            str(data_root),
            "--release-root",
            str(candidate),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["status"] == "healthy"
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert {"core", "ffmpeg", "ollama_models"} <= set(state)
    assert not (data_root / "app/current").exists()
    assert not (data_root / "extension/manifest.json").exists()


def test_default_install_skip_models_reports_incomplete(tmp_path: Path) -> None:
    dependencies, files = _fixture_contract()
    test_root = tmp_path / "skip 根"
    test_root.mkdir()
    release = _build_install_source(test_root, dependencies)
    home = tmp_path / "empty-home"
    home.mkdir()
    with DownloadFixture(files) as fixture:
        environment = _environment(test_root, fixture)
        environment.update(
            {
                "HOME": str(home),
                "LVT_TEST_ROOT": str(test_root),
                "LVT_PYTHON": sys.executable,
                "LVT_TEST_UV_SOURCE": str(release / "test-tools/uv"),
                "LVT_TEST_PYTHON_SOURCE": str(release / "test-tools/python/bin/python3"),
                "LVT_TEST_RUNTIME_PYTHON": sys.executable,
            }
        )
        completed = subprocess.run(
            [str(release / "scripts/install.command"), "--skip-models"],
            cwd="/",
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert completed.returncode == 1
    assert "INSTALL_DEPENDENCIES_INCOMPLETE" in completed.stdout
    assert "INSTALL_DEPENDENCIES_READY" not in completed.stdout
    data_root = test_root / "LocalVideoTranscriber"
    state = json.loads((data_root / "runtime/install-state.json").read_text())
    assert {"schema_version", "core", "ffmpeg"} <= set(state)
    assert "ollama_models" not in state
    assert not (data_root / "app/current").exists()
    assert not (data_root / "extension/manifest.json").exists()
