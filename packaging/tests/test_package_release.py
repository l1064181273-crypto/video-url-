from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_TOOL = ROOT / "packaging/tools/package_release.py"


def _package(output: Path) -> Path:
    completed = subprocess.run(
        [sys.executable, str(PACKAGE_TOOL), "--output-dir", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest = json.loads((ROOT / "packaging/release-manifest.json").read_text(encoding="utf-8"))
    return output / manifest["archive"]["filename"]


def test_release_zip_is_reproducible_loadable_and_contains_one_click_launcher(
    tmp_path: Path,
) -> None:
    first = _package(tmp_path / "first")
    second = _package(tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    assert first.with_suffix(first.suffix + ".sha256").is_file()
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        root = "LocalVideoTranscriber-0.1.1"
        assert f"{root}/启动 Local Video Transcriber.command" in names
        assert f"{root}/extension/manifest.json" in names
        assert f"{root}/extension/dist/manifest.json" not in names
        assert f"{root}/README.md" in names
        beginner_guide = f"{root}/新手使用说明.txt"
        assert beginner_guide in names
        guide = archive.read(beginner_guide).decode("utf-8")
        assert "chrome://extensions" in guide
        assert "启动 Local Video Transcriber.command" in guide
        assert "~/Library/Application Support/LocalVideoTranscriber/extension" in guide
        assert "START_READY" in guide
        for developer_script in (
            "scripts/make-test-assets.sh",
            "scripts/run-phase2-acceptance.py",
            "scripts/run-public-smoke.py",
            "scripts/run-real-e2e.py",
            "scripts/verify-real-e2e.py",
        ):
            assert f"{root}/{developer_script}" not in names
        launcher = archive.getinfo(f"{root}/启动 Local Video Transcriber.command")
        assert stat.S_IMODE(launcher.external_attr >> 16) == 0o755
        lowered = "\n".join(names).lower()
        for forbidden in ("api-token", ".git/", "node_modules/", ".sqlite", ".log"):
            assert forbidden not in lowered

    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.with_suffix(first.suffix + ".sha256").read_text(encoding="ascii") == (
        f"{digest}  {first.name}\n"
    )
