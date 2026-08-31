from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "packaging" / "tools"
sys.path.insert(0, str(TOOLS))

import install  # noqa: E402
import provision  # noqa: E402
import runtime_layout as runtime_layout_module  # noqa: E402
import verify_install  # noqa: E402
from runtime_layout import (  # noqa: E402
    UnsupportedRuntimePlatformError,
    path_is_link_like,
    runtime_layout,
)


def _pe_x64(label: bytes) -> bytes:
    header = bytearray(256)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x80)
    header[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", header, 0x84, 0x8664)
    header[0x86:] = label
    return bytes(header)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_runtime_layout_preserves_macos_contract() -> None:
    layout = runtime_layout("darwin")

    assert layout.target == "macos-arm64"
    assert layout.architecture == "arm64"
    assert layout.dependency_manifest == "packaging/dependencies.json"
    assert layout.uv_executable == "uv"
    assert layout.python_executable == "bin/python3"
    assert layout.venv_python == ".venv/bin/python"
    assert layout.ffmpeg_executables == {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
    }
    assert layout.ollama_executables == {
        "ollama": "ollama",
        "llama-server": "llama-server",
        "llama-quantize": "llama-quantize",
    }
    assert layout.executable_format == "macho-arm64"


def test_runtime_layout_defines_windows_x64_contract() -> None:
    layout = runtime_layout("win32")

    assert layout.target == "windows-x64"
    assert layout.architecture == "x86_64"
    assert layout.dependency_manifest == "packaging/dependencies.windows-x64.json"
    assert layout.uv_executable == "uv.exe"
    assert layout.python_executable == "python.exe"
    assert layout.venv_python == ".venv/Scripts/python.exe"
    assert layout.ffmpeg_executables == {
        "ffmpeg": "ffmpeg.exe",
        "ffprobe": "ffprobe.exe",
    }
    assert layout.ollama_executables == {
        "ollama": "ollama.exe",
        "llama-server": "llama-server.exe",
        "llama-quantize": "llama-quantize.exe",
    }
    assert layout.executable_format == "pe-x64"
    assert layout.model_artifact_ids == (
        "asr-faster-whisper-small-config",
        "asr-faster-whisper-small-model",
        "asr-faster-whisper-small-tokenizer",
        "asr-faster-whisper-small-vocabulary",
        "diarization-segmentation",
        "diarization-embedding",
        "hy-mt2",
    )
    assert layout.required_packages == ("faster_whisper", "ctranslate2", "sherpa_onnx")


@pytest.mark.parametrize("system", ["linux", "cygwin", "msys", ""])
def test_runtime_layout_rejects_unsupported_platforms(system: str) -> None:
    with pytest.raises(UnsupportedRuntimePlatformError):
        runtime_layout(system)


@pytest.mark.parametrize(
    ("system", "expected_target"),
    [("darwin", "macos-arm64"), ("win32", "windows-x64")],
)
def test_install_loads_only_the_selected_platform_manifest(
    system: str,
    expected_target: str,
) -> None:
    payload = install._load_dependencies(ROOT, system=system)

    assert payload["target"] == expected_target


def test_install_rejects_windows_manifest_with_cross_architecture_artifact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    manifest = source / "packaging/dependencies.windows-x64.json"
    manifest.parent.mkdir(parents=True)
    payload = json.loads(
        (ROOT / "packaging/dependencies.windows-x64.json").read_text(encoding="utf-8")
    )
    payload["artifacts"][0]["architecture"] = "arm64"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(install.InstallError, match="invalid"):
        install._load_dependencies(source, system="win32")


def test_windows_installer_accepts_canonical_manifest_inside_platform_zip(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    manifest = source / "packaging/dependencies.json"
    manifest.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/dependencies.windows-x64.json", manifest)

    payload = install._load_dependencies(source, system="win32")

    assert payload["target"] == "windows-x64"


def test_windows_release_core_canonicalizes_selected_manifest(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"

    install._copy_release_core(ROOT, candidate, system="win32")

    canonical = json.loads((candidate / "packaging/dependencies.json").read_text(encoding="utf-8"))
    assert canonical["target"] == "windows-x64"
    assert not (candidate / "packaging/dependencies.windows-x64.json").exists()
    assert (candidate / "packaging/tools/runtime_layout.py").is_file()
    assert (
        candidate / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km"
    ).read_bytes() == (ROOT / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km").read_bytes()


def test_provision_rejects_manifest_for_another_platform(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest = release / "packaging/dependencies.json"
    manifest.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/dependencies.json", manifest)

    with pytest.raises(provision.ProvisionError, match="trust policy"):
        provision._load_dependencies(release, system="win32")


def test_verify_uses_windows_python_and_ffmpeg_names() -> None:
    layout = runtime_layout("win32")

    assert verify_install._release_python_relative(layout) == ".venv/Scripts/python.exe"
    assert verify_install._ffmpeg_executable_names(layout) == {
        "ffmpeg": "ffmpeg.exe",
        "ffprobe": "ffprobe.exe",
    }


def test_windows_installs_injected_uv_and_python_to_native_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = runtime_layout("win32")
    source_root = tmp_path / "source"
    tools_root = tmp_path / "tools"
    fixture_root = tmp_path / "fixtures"
    source_root.mkdir()
    tools_root.mkdir()
    fixture_root.mkdir()
    uv_source = fixture_root / "uv.exe"
    python_source = fixture_root / "python.exe"
    uv_source.write_bytes(b"uv")
    python_source.write_bytes(_pe_x64(b"python"))
    monkeypatch.setenv("LVT_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("LVT_TEST_UV_SOURCE", str(uv_source))
    monkeypatch.setenv("LVT_TEST_PYTHON_SOURCE", str(python_source))

    assert install._install_uv(source_root, tools_root, {}, layout=layout)
    assert install._install_python(source_root, tools_root, {}, layout=layout)

    assert (tools_root / "uv.exe").read_bytes() == b"uv"
    assert (tools_root / "python/python.exe").read_bytes() == _pe_x64(b"python")
    assert not (tools_root / "uv").exists()
    assert not (tools_root / "python/bin/python3").exists()


@pytest.mark.parametrize(
    ("machine", "expected"),
    [(0x8664, True), (0x014C, False), (0xAA64, False)],
)
def test_pe_x64_validation_is_machine_specific(
    tmp_path: Path,
    machine: int,
    expected: bool,
) -> None:
    binary = bytearray(_pe_x64(b"binary"))
    struct.pack_into("<H", binary, 0x84, machine)
    path = tmp_path / "tool.exe"
    path.write_bytes(binary)

    assert provision._is_x64_pe(path) is expected


def test_windows_ffmpeg_archive_publishes_exe_names_with_logical_digest_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = runtime_layout("win32")
    ffmpeg = _pe_x64(b"ffmpeg")
    ffprobe = _pe_x64(b"ffprobe")
    archive = tmp_path / "ffmpeg.zip"
    archive.write_bytes(
        _zip_bytes(
            {
                "win32/ffmpeg.exe": ffmpeg,
                "win32/ffprobe.exe": ffprobe,
            }
        )
    )
    source_root = tmp_path / "source"
    data_root = tmp_path / "data"
    source_root.mkdir()
    (data_root / "app").mkdir(parents=True)
    (data_root / "models/quarantine").mkdir(parents=True)
    artifact = {
        "id": "ffmpeg",
        "version": "fixture",
        "expected_files": ["win32/ffmpeg.exe", "win32/ffprobe.exe"],
    }
    monkeypatch.setattr(provision, "_ensure_archive", lambda *_args: archive)

    directory, digests = provision._install_archive_tool(
        source_root,
        data_root,
        artifact,
        tool_name="ffmpeg",
        layout=layout,
    )

    assert (directory / "ffmpeg.exe").read_bytes() == ffmpeg
    assert (directory / "ffprobe.exe").read_bytes() == ffprobe
    assert digests == {
        "ffmpeg": hashlib.sha256(ffmpeg).hexdigest(),
        "ffprobe": hashlib.sha256(ffprobe).hexdigest(),
    }


def test_windows_ollama_archive_preserves_required_runtime_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = runtime_layout("win32")
    files = {
        "ollama.exe": _pe_x64(b"ollama"),
        "lib/ollama/llama-server.exe": _pe_x64(b"server"),
        "lib/ollama/llama-quantize.exe": _pe_x64(b"quantize"),
        "lib/ollama/ggml.dll": _pe_x64(b"ggml"),
        "lib/ollama/ggml-cpu-x64.dll": _pe_x64(b"cpu"),
    }
    archive = tmp_path / "ollama.zip"
    archive.write_bytes(_zip_bytes(files))
    source_root = tmp_path / "source"
    data_root = tmp_path / "data"
    source_root.mkdir()
    (data_root / "app").mkdir(parents=True)
    (data_root / "models/quarantine").mkdir(parents=True)
    artifact = {
        "id": "ollama",
        "version": "fixture",
        "expected_files": [
            "ollama.exe",
            "lib/ollama/llama-server.exe",
            "lib/ollama/llama-quantize.exe",
        ],
        "executable": "ollama.exe",
    }
    monkeypatch.setattr(provision, "_ensure_archive", lambda *_args: archive)

    directory, digests = provision._install_archive_tool(
        source_root,
        data_root,
        artifact,
        tool_name="ollama",
        layout=layout,
    )

    assert directory == data_root / "app/tools/ollama/fixture"
    for relative, expected in files.items():
        assert (directory / relative).read_bytes() == expected
    assert digests == {
        "ollama": hashlib.sha256(files["ollama.exe"]).hexdigest(),
        "llama-server": hashlib.sha256(files["lib/ollama/llama-server.exe"]).hexdigest(),
        "llama-quantize": hashlib.sha256(files["lib/ollama/llama-quantize.exe"]).hexdigest(),
    }


def test_windows_staging_core_uses_native_tool_and_venv_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    shutil.copytree(ROOT / "packaging/tests/fixtures/fake-release", source)
    shutil.copy2(
        ROOT / "packaging/dependencies.windows-x64.json",
        source / "packaging/dependencies.windows-x64.json",
    )
    modelfile = source / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km"
    modelfile.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km", modelfile)
    for name in (*install.PACKAGING_TOOLS, *install.WINDOWS_PACKAGING_TOOLS):
        destination = source / "packaging/tools" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "packaging/tools" / name, destination)
    shutil.copytree(ROOT / "scripts", source / "scripts", dirs_exist_ok=True)
    extension = source / "extension/dist"
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        '{"manifest_version":3,"name":"fixture","version":"0.1.0"}\n',
        encoding="utf-8",
    )
    uv_source = source / "test-tools/uv.exe"
    uv_source.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os, shutil",
                "from pathlib import Path",
                "environment = Path(os.environ['UV_PROJECT_ENVIRONMENT'])",
                "target = environment / 'Scripts/python.exe'",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "shutil.copy2(os.environ['LVT_INSTALL_PYTHON'], target)",
                "target.chmod(0o755)",
                "(environment / 'pyvenv.cfg').write_text('fixture = true\\n')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    uv_source.chmod(0o755)
    python_source = source / "test-tools/python.exe"
    shutil.copy2(source / "test-tools/python/bin/python3", python_source)
    python_source.chmod(0o755)
    data_root = tmp_path / "data" / "LocalVideoTranscriber"
    monkeypatch.setenv("LVT_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("LVT_TEST_UV_SOURCE", str(uv_source))
    monkeypatch.setenv("LVT_TEST_PYTHON_SOURCE", str(python_source))
    monkeypatch.setenv("LVT_TEST_RUNTIME_PYTHON", sys.executable)

    release = install.install_staging_core(source, data_root, system="win32")

    assert (data_root / "app/tools/uv.exe").is_file()
    assert (data_root / "app/tools/python/python.exe").is_file()
    assert (release / ".venv/Scripts/python.exe").is_file()
    manifest = json.loads((release / "packaging/dependencies.json").read_text(encoding="utf-8"))
    assert manifest["target"] == "windows-x64"


def test_staging_validator_import_does_not_require_process_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = ROOT / "packaging/tools/verify_install.py"
    spec = importlib.util.spec_from_file_location("verify_install_without_process", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "process_state", None)
    monkeypatch.setitem(sys.modules, spec.name, module)

    spec.loader.exec_module(module)

    assert callable(module.validate_install)


def test_windows_token_metadata_does_not_require_posix_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = tmp_path / "config" / "api-token"
    token.parent.mkdir()
    token.write_bytes(b"a" * 64 + b"\n")
    monkeypatch.delattr(os, "getuid", raising=False)

    check = verify_install._validate_token_metadata(tmp_path, runtime_layout("win32"))

    assert check.status is verify_install.CheckStatus.OK


def test_lifecycle_lock_import_does_not_require_fcntl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = ROOT / "packaging/tools/lifecycle_lock.py"
    spec = importlib.util.spec_from_file_location("lifecycle_lock_without_fcntl", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: object = None,
        locals: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "fcntl":
            raise ModuleNotFoundError("fcntl is unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setitem(sys.modules, spec.name, module)

    spec.loader.exec_module(module)

    assert callable(module.LifecycleLock)


class _DownloadResponse(io.BytesIO):
    def __init__(self, content: bytes, final_url: str) -> None:
        super().__init__(content)
        self._final_url = final_url

    def __enter__(self) -> _DownloadResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def geturl(self) -> str:
        return self._final_url


def test_windows_tool_download_uses_python_and_verifies_size_and_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = runtime_layout("win32")
    content = b"pinned-windows-tool"
    artifact = {
        "url": "https://downloads.example.invalid/tool.zip",
        "version": "1.0",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Windows download invoked zsh"),
    )
    monkeypatch.setattr(
        install.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _DownloadResponse(
            content,
            "https://cdn.example.invalid/tool.zip",
        ),
    )
    tools_root = tmp_path / "tools"
    tools_root.mkdir()

    archive = install._download_archive(
        tmp_path,
        tools_root,
        artifact,
        "uv",
        layout=layout,
    )

    assert archive.read_bytes() == content
    assert not list(tools_root.rglob("*.partial.*"))


def test_windows_tool_download_rejects_https_to_http_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = runtime_layout("win32")
    content = b"pinned-windows-tool"
    artifact = {
        "url": "https://downloads.example.invalid/tool.zip",
        "version": "1.0",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    monkeypatch.setattr(
        install.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _DownloadResponse(
            content,
            "http://cdn.example.invalid/tool.zip",
        ),
    )
    tools_root = tmp_path / "tools"
    tools_root.mkdir()

    with pytest.raises(install.InstallError, match="download failed"):
        install._download_archive(
            tmp_path,
            tools_root,
            artifact,
            "uv",
            layout=layout,
        )

    assert not list(tools_root.rglob("*.partial.*"))


def test_windows_reparse_point_is_treated_as_link_like(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "junction"
    candidate.mkdir()
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(
        runtime_layout_module.os,
        "lstat",
        lambda _path: SimpleNamespace(st_file_attributes=0x400),
    )

    assert path_is_link_like(candidate)


def test_windows_dependency_download_uses_python_without_zsh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = runtime_layout("win32")
    content = b"pinned-windows-dependency"
    digest = hashlib.sha256(content).hexdigest()
    source_root = tmp_path / "source"
    controlled_root = tmp_path / "controlled"
    source_root.mkdir()
    controlled_root.mkdir()
    monkeypatch.setattr(
        provision.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Windows download invoked zsh"),
    )
    monkeypatch.setattr(
        provision.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _DownloadResponse(
            content,
            "https://cdn.example.invalid/dependency.bin",
        ),
    )

    downloaded = provision._download_verified(
        source_root,
        "https://downloads.example.invalid/dependency.bin",
        controlled_root,
        "downloads/dependency.bin",
        digest,
        len(content),
        "dependency",
        layout=layout,
    )

    assert downloaded.read_bytes() == content
    assert not list(controlled_root.rglob("*.partial.*"))


def test_windows_staging_launcher_is_pinned_and_non_elevating() -> None:
    command = (ROOT / "启动 Local Video Transcriber.cmd").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install.ps1").read_text(encoding="utf-8")
    dependencies = json.loads(
        (ROOT / "packaging/dependencies.windows-x64.json").read_text(encoding="utf-8")
    )
    python = next(item for item in dependencies["artifacts"] if item["id"] == "python")

    assert "powershell.exe" in command
    assert "-NoProfile" in command
    assert "-ExecutionPolicy Bypass" in command
    assert "scripts\\install.ps1" in command
    assert "$env:LOCALAPPDATA" in installer
    assert python["url"] in installer
    assert python["sha256"] in installer
    assert str(python["size"]) in installer
    assert "Get-FileHash" in installer
    assert "WindowsIdentity" in installer
    assert "SetAccessRuleProtection($true, $false)" in installer
    assert "S-1-5-18" in installer
    assert "Get-ChildItem -LiteralPath $LiteralPath -Force -Recurse" in installer
    assert "Everyone" not in installer
    assert "packaging/tools/install.py" in installer
    assert 'string]$Phase = "all"' in installer
    assert "windows_publish_install.py" in installer
    assert "provision.py" in installer
    assert "Compare-LvtVersion" in installer
    assert "WINDOWS_DOWNGRADE_REFUSED" in installer
    assert "--phase" in installer
    assert "staging-core" in installer
    assert "Invoke-Expression" not in installer
    assert "-Verb RunAs" not in installer
    assert "SetExecutionPolicy" not in installer


def test_windows_lifecycle_scripts_require_activated_release_and_no_raw_pid_stop() -> None:
    common = (ROOT / "scripts/lib/WindowsCommon.psm1").read_text(encoding="utf-8")
    start = (ROOT / "scripts/start.ps1").read_text(encoding="utf-8")
    stop = (ROOT / "scripts/stop.ps1").read_text(encoding="utf-8")
    doctor = (ROOT / "scripts/doctor.ps1").read_text(encoding="utf-8")
    combined = "\n".join((common, start, stop, doctor))

    assert "core.activated" in common
    assert "Assert-LvtPrivateAcl" in common
    assert "windows_lifecycle.py" in common
    assert "Invoke-LvtLifecycle" in start
    assert "Invoke-LvtLifecycle" in stop
    assert "verify_install.py" in doctor
    assert "--target" in doctor
    assert "windows-x64" in doctor
    for forbidden in ("taskkill", "Stop-Process", "Invoke-Expression"):
        assert forbidden not in combined


def test_windows_current_release_resolves_only_durably_activated_state(
    tmp_path: Path,
) -> None:
    layout = runtime_layout("win32")
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.1"
    release.mkdir(parents=True)
    (release / "VERSION").write_text("0.1.1\n", encoding="utf-8")
    state = data_root / "runtime/install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "core": {
                    "release": "app/releases/0.1.1",
                    "verified": True,
                    "activated": True,
                    "version": "0.1.1",
                },
            }
        ),
        encoding="utf-8",
    )

    resolved, checks = verify_install._resolve_current_release(
        data_root,
        release,
        layout,
    )

    assert resolved == release
    assert all(check.status is verify_install.CheckStatus.OK for check in checks)


def test_windows_current_release_rejects_staging_only_state(tmp_path: Path) -> None:
    layout = runtime_layout("win32")
    data_root = tmp_path / "LocalVideoTranscriber"
    release = data_root / "app/releases/0.1.1"
    release.mkdir(parents=True)
    (release / "VERSION").write_text("0.1.1\n", encoding="utf-8")
    state = data_root / "runtime/install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "core": {
                    "release": "app/releases/0.1.1",
                    "verified": True,
                    "version": "0.1.1",
                },
            }
        ),
        encoding="utf-8",
    )

    resolved, checks = verify_install._resolve_current_release(
        data_root,
        release,
        layout,
    )

    assert resolved is None
    assert checks[0].code == "CURRENT_RELEASE_MISSING"
