from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "fake-release"
INSTALL_COMMAND = REPOSITORY_ROOT / "scripts" / "install.command"
INSTALL_TOOL = REPOSITORY_ROOT / "packaging" / "tools" / "install.py"
VERIFY_TOOL = REPOSITORY_ROOT / "packaging" / "tools" / "verify_install.py"
LOCK_TOOL = REPOSITORY_ROOT / "packaging" / "tools" / "lifecycle_lock.py"
DOCTOR_TOOL = REPOSITORY_ROOT / "packaging" / "tools" / "doctor.py"
COMMON_LIBRARY = REPOSITORY_ROOT / "scripts" / "lib" / "common.zsh"
DATA_DIRECTORIES = ("config", "db", "runtime", "work", "exports", "logs", "models")
FAILURE_POINTS = (
    "before-uv",
    "before-python",
    "before-venv-sync",
    "before-token",
    "before-extension-candidate",
)


def _copy_file(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if executable:
        destination.chmod(0o755)


def _build_release(tmp_path: Path, name: str = "Release 源 naïve") -> Path:
    release = tmp_path / name
    shutil.copytree(FIXTURE_ROOT, release)
    extension_manifest = release / "extension/dist/manifest.json"
    extension_manifest.parent.mkdir(parents=True, exist_ok=True)
    extension_manifest.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Local Video Transcriber Fixture",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )
    _copy_file(INSTALL_COMMAND, release / "scripts/install.command", executable=True)
    _copy_file(COMMON_LIBRARY, release / "scripts/lib/common.zsh", executable=True)
    _copy_file(INSTALL_TOOL, release / "packaging/tools/install.py", executable=True)
    _copy_file(VERIFY_TOOL, release / "packaging/tools/verify_install.py", executable=True)
    _copy_file(LOCK_TOOL, release / "packaging/tools/lifecycle_lock.py", executable=True)
    _copy_file(DOCTOR_TOOL, release / "packaging/tools/doctor.py", executable=True)
    (release / "scripts/doctor.command").chmod(0o755)
    (release / "test-tools/uv").chmod(0o755)
    (release / "test-tools/python/bin/python3").chmod(0o755)
    return release


def _environment(
    release: Path,
    test_root: Path,
    *,
    home: Path,
    failure: str | None = None,
    audit_path: Path | None = None,
    environment_audit_path: Path | None = None,
    injected_python: bool = True,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LVT_TEST_ROOT": str(test_root),
            "LVT_TEST_UV_SOURCE": str(release / "test-tools/uv"),
            "LVT_TEST_PYTHON_SOURCE": str(release / "test-tools/python/bin/python3"),
            "LVT_TEST_RUNTIME_PYTHON": sys.executable,
        }
    )
    if injected_python:
        environment["LVT_PYTHON"] = sys.executable
    else:
        environment.pop("LVT_PYTHON", None)
    if failure is not None:
        environment["LVT_TEST_FAIL_AT"] = failure
    if audit_path is not None:
        environment["LVT_TEST_PROCESS_AUDIT"] = str(audit_path)
    if environment_audit_path is not None:
        environment["LVT_TEST_ENV_AUDIT"] = str(environment_audit_path)
    return environment


def _run_install(
    release: Path,
    test_root: Path,
    *,
    home: Path,
    data_root: Path | None = None,
    failure: str | None = None,
    audit_path: Path | None = None,
    environment_audit_path: Path | None = None,
    injected_python: bool = True,
) -> subprocess.CompletedProcess[str]:
    arguments = [str(release / "scripts/install.command"), "--phase", "staging-core"]
    if data_root is not None:
        arguments.extend(["--data-root", str(data_root)])
    return subprocess.run(
        arguments,
        cwd="/",
        env=_environment(
            release,
            test_root,
            home=home,
            failure=failure,
            audit_path=audit_path,
            environment_audit_path=environment_audit_path,
            injected_python=injected_python,
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _candidate(data_root: Path, version: str = "0.1.0") -> Path:
    return data_root / "app" / "releases" / version


def _assert_no_cp4_publish(data_root: Path) -> None:
    assert not (data_root / "app/current").exists()
    assert not (data_root / "app/current").is_symlink()
    assert not (data_root / "extension/manifest.json").exists()


def _assert_staging_valid(data_root: Path, candidate: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(VERIFY_TOOL),
            "--phase",
            "staging-core",
            "--data-root",
            str(data_root),
            "--release-root",
            str(candidate),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "healthy"
    assert report["exit_code"] == 0
    return report


def _snapshot(path: Path) -> tuple[int, bytes]:
    return path.stat().st_ino, path.read_bytes()


def test_empty_home_first_install_builds_valid_staging_candidate_without_secret_leak(
    tmp_path: Path,
) -> None:
    release = _build_release(tmp_path)
    test_root = tmp_path / "Test Root"
    home = tmp_path / "empty-home"
    home.mkdir()
    audit = tmp_path / "process-arguments.json"
    environment_audit = tmp_path / "process-environment.txt"

    completed = _run_install(
        release,
        test_root,
        home=home,
        audit_path=audit,
        environment_audit_path=environment_audit,
        injected_python=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    data_root = test_root / "LocalVideoTranscriber"
    candidate = _candidate(data_root)
    _assert_staging_valid(data_root, candidate)
    for relative in DATA_DIRECTORIES:
        directory = data_root / relative
        assert directory.is_dir()
        assert not directory.is_symlink()
    for relative in ("db", "exports", "logs", "work", "runtime"):
        assert os.access(data_root / relative, os.W_OK)
    assert (data_root / "app/tools/uv").is_file()
    assert (data_root / "app/tools/python/bin/python3").is_file()
    assert (candidate / ".venv/bin/python").is_file()
    assert (candidate / "extension/manifest.json").is_file()
    token = data_root / "config/api-token"
    token_metadata = token.lstat()
    assert stat.S_ISREG(token_metadata.st_mode)
    assert not token.is_symlink()
    assert token_metadata.st_uid == os.getuid()
    assert token_metadata.st_mode & 0o777 == 0o600
    assert 32 <= token_metadata.st_size <= 4096
    secret = token.read_text(encoding="ascii")
    transcript = (
        completed.stdout
        + completed.stderr
        + audit.read_text(encoding="utf-8")
        + environment_audit.read_text(encoding="utf-8")
    )
    assert secret not in transcript
    assert "api-token" not in audit.read_text(encoding="utf-8")
    state = json.loads((data_root / "runtime/install-state.json").read_text(encoding="utf-8"))
    assert state["core"] == {
        "release": "app/releases/0.1.0",
        "verified": True,
        "version": "0.1.0",
    }
    _assert_no_cp4_publish(data_root)


@pytest.mark.parametrize("directory_name", ["path with spaces", "中文目录", "naïve-路径"])
def test_install_supports_arbitrary_non_ascii_extraction_and_data_paths(
    tmp_path: Path,
    directory_name: str,
) -> None:
    release = _build_release(tmp_path, f"{directory_name} release")
    test_root = tmp_path / f"{directory_name} test"
    home = tmp_path / f"{directory_name} home"
    home.mkdir()
    data_root = test_root / f"{directory_name} data"

    completed = _run_install(
        release,
        test_root,
        home=home,
        data_root=data_root,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    _assert_staging_valid(data_root, _candidate(data_root))
    _assert_no_cp4_publish(data_root)


def test_same_version_second_install_preserves_data_and_verified_tools(
    tmp_path: Path,
) -> None:
    release = _build_release(tmp_path)
    test_root = tmp_path / "test-root"
    home = tmp_path / "home"
    home.mkdir()
    data_root = test_root / "LocalVideoTranscriber"

    first = _run_install(release, test_root, home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    database = data_root / "db/lvt.sqlite3"
    database.write_bytes(b"existing database bytes")
    tracked = {
        "token": data_root / "config/api-token",
        "database": database,
        "uv": data_root / "app/tools/uv",
        "python": data_root / "app/tools/python/bin/python3",
        "venv": _candidate(data_root) / ".venv/bin/python",
    }
    before = {name: _snapshot(path) for name, path in tracked.items()}

    second = _run_install(release, test_root, home=home, injected_python=False)

    assert second.returncode == 0, second.stdout + second.stderr
    assert {name: _snapshot(path) for name, path in tracked.items()} == before
    _assert_staging_valid(data_root, _candidate(data_root))
    _assert_no_cp4_publish(data_root)


@pytest.mark.parametrize("failure_point", FAILURE_POINTS)
def test_injected_failure_never_publishes_current_or_damages_existing_data(
    tmp_path: Path,
    failure_point: str,
) -> None:
    release = _build_release(tmp_path)
    test_root = tmp_path / "test-root"
    home = tmp_path / "home"
    home.mkdir()
    data_root = test_root / "LocalVideoTranscriber"
    (data_root / "app").mkdir(parents=True)
    (data_root / "config").mkdir()
    (data_root / "db").mkdir()
    token = data_root / "config/api-token"
    token.write_bytes(b"T" * 48)
    token.chmod(0o600)
    database = data_root / "db/lvt.sqlite3"
    database.write_bytes(b"database-before-failure")
    before = {"token": _snapshot(token), "database": _snapshot(database)}

    completed = _run_install(
        release,
        test_root,
        home=home,
        failure=failure_point,
    )

    assert completed.returncode != 0
    assert {"token": _snapshot(token), "database": _snapshot(database)} == before
    assert not _candidate(data_root).exists()
    _assert_no_cp4_publish(data_root)
    leftovers = [
        path
        for path in data_root.rglob("*")
        if ".partial." in path.name or ".candidate." in path.name
    ]
    assert leftovers == []
    assert "T" * 48 not in completed.stdout + completed.stderr
    assert "Traceback" not in completed.stderr


def test_failed_install_preserves_existing_current_release(tmp_path: Path) -> None:
    release = _build_release(tmp_path)
    (release / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    test_root = tmp_path / "test-root"
    home = tmp_path / "home"
    home.mkdir()
    data_root = test_root / "LocalVideoTranscriber"
    old_release = data_root / "app/releases/0.1.0"
    old_release.mkdir(parents=True)
    marker = old_release / "preserve-me"
    marker.write_bytes(b"old release")
    current = data_root / "app/current"
    current.symlink_to(old_release)
    before = _snapshot(marker)

    completed = _run_install(
        release,
        test_root,
        home=home,
        failure="before-extension-candidate",
    )

    assert completed.returncode != 0
    assert current.is_symlink()
    assert current.resolve(strict=True) == old_release
    assert _snapshot(marker) == before
    assert not _candidate(data_root, "0.2.0").exists()
    assert not (data_root / "extension/manifest.json").exists()


def test_failure_after_each_mutation_boundary_is_also_non_publishing(
    tmp_path: Path,
) -> None:
    for checkpoint in (
        "after-uv",
        "after-python",
        "after-venv-sync",
        "after-token",
        "after-extension-candidate",
    ):
        case_root = tmp_path / checkpoint
        case_root.mkdir()
        release = _build_release(case_root)
        test_root = case_root / "test-root"
        home = case_root / "home"
        home.mkdir()
        data_root = test_root / "LocalVideoTranscriber"

        completed = _run_install(
            release,
            test_root,
            home=home,
            failure=checkpoint,
        )

        assert completed.returncode != 0
        assert not _candidate(data_root).exists()
        _assert_no_cp4_publish(data_root)
