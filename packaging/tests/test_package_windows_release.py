from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_TOOL = ROOT / "packaging/tools/package_windows_release.py"


def _package(output: Path) -> Path:
    completed = subprocess.run(
        [sys.executable, str(PACKAGE_TOOL), "--output-dir", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return output / "LocalVideoTranscriber-0.1.1-windows-x64.zip"


def test_windows_zip_is_reproducible_and_contains_only_windows_runtime(
    tmp_path: Path,
) -> None:
    first = _package(tmp_path / "first")
    second = _package(tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        root = "LocalVideoTranscriber-0.1.1"
        names = archive.namelist()
        required = {
            f"{root}/启动 Local Video Transcriber.cmd",
            f"{root}/新手使用说明.txt",
            f"{root}/scripts/install.ps1",
            f"{root}/scripts/start.ps1",
            f"{root}/scripts/stop.ps1",
            f"{root}/scripts/doctor.ps1",
            f"{root}/scripts/lib/WindowsCommon.psm1",
            f"{root}/packaging/dependencies.json",
            f"{root}/packaging/tools/windows_publish_install.py",
            f"{root}/packaging/tools/windows_lifecycle.py",
            f"{root}/packaging/tools/windows_tool_supervisor.py",
            f"{root}/extension/manifest.json",
        }
        assert required <= set(names)
        assert f"{root}/启动 Local Video Transcriber.command" not in names
        assert not any(name.endswith(".zsh") or name.endswith(".command") for name in names)
        dependencies = json.loads(
            archive.read(f"{root}/packaging/dependencies.json").decode("utf-8")
        )
        assert dependencies["target"] == "windows-x64"
        for relative in (
            "启动 Local Video Transcriber.cmd",
            "scripts/install.ps1",
            "scripts/start.ps1",
            "scripts/stop.ps1",
            "scripts/doctor.ps1",
            "scripts/lib/WindowsCommon.psm1",
        ):
            content = archive.read(f"{root}/{relative}")
            assert b"\r\n" in content
            assert b"\n" not in content.replace(b"\r\n", b"")
        guide = archive.read(f"{root}/新手使用说明.txt").decode("utf-8")
        assert "chrome://extensions" in guide
        assert "启动 Local Video Transcriber.cmd" in guide
        assert "%LOCALAPPDATA%" in guide
        lowered = "\n".join(names).lower()
        for forbidden in (
            ".git/",
            ".venv/",
            "node_modules/",
            "api-token",
            "test-results/",
            "playwright-report/",
            ".sqlite",
            ".log",
            ".gguf",
            ".onnx",
        ):
            assert forbidden not in lowered

    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    checksum = first.with_suffix(first.suffix + ".sha256")
    assert checksum.read_text(encoding="ascii") == f"{digest}  {first.name}\n"


def test_windows_native_acceptance_workflow_is_manual_and_archives_evidence() -> None:
    workflow = (ROOT / ".github/workflows/windows-validation.yml").read_text(encoding="utf-8")
    acceptance = (ROOT / "scripts/windows-acceptance.ps1").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "runs-on: windows-2022" in workflow
    assert "full_model_install" in workflow
    assert "./scripts/windows-acceptance.ps1" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "ruff-backend" in acceptance
    assert "mypy-backend" in acceptance
    assert "native-staging-core" in acceptance
    assert "native-dependencies" in acceptance
    assert "native-asr-cpu" in acceptance
    assert "windows-asr-smoke.py" in acceptance
    assert "native-publish" in acceptance
    assert "native-runtime-doctor" in acceptance
    assert "chrome-e2e" in acceptance
    assert "Get-NetTCPConnection" in acceptance
    assert "Get-CimInstance Win32_Process" in acceptance
    assert "taskkill" not in acceptance
    assert "Stop-Process" not in acceptance
