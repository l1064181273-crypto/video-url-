from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "scripts" / "lib" / "common.zsh"
PROCESS = ROOT / "scripts" / "lib" / "process.zsh"
DOWNLOAD = ROOT / "scripts" / "lib" / "download.zsh"
DOCTOR_COMMAND = ROOT / "scripts" / "doctor.command"
LOCK_MODULE = ROOT / "packaging" / "tools" / "lifecycle_lock.py"


def _run_zsh(
    script: str, *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/zsh", "-c", script, "--", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _load_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lifecycle_lock_under_test", LOCK_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute/path",
        ".",
        "..",
        "../escape",
        "nested/../escape",
        "nested/./file",
        "nested//file",
        r"nested\file",
    ],
)
def test_common_library_rejects_unsafe_relative_paths(value: str) -> None:
    completed = _run_zsh(
        'source "$1"; lvt_validate_relative_path "$2"',
        str(COMMON),
        value,
    )

    assert completed.returncode != 0


def test_common_library_accepts_unicode_relative_path() -> None:
    completed = _run_zsh(
        'source "$1"; lvt_validate_relative_path "$2"; print -r -- ok',
        str(COMMON),
        "目录 with spaces/file.txt",
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_common_library_redacts_tokens_queries_and_absolute_paths(tmp_path: Path) -> None:
    secret = "LVT_TEST_SECRET_" + "x" * 48
    value = (
        f"X-LVT-Token: {secret} "
        "https://example.invalid/file?token=hidden&key=value "
        f"{tmp_path}/private/file"
    )

    completed = _run_zsh(
        'source "$1"; lvt_redact "$2"',
        str(COMMON),
        value,
    )

    assert completed.returncode == 0
    assert secret not in completed.stdout
    assert "hidden" not in completed.stdout
    assert str(tmp_path) not in completed.stdout
    assert "[REDACTED]" in completed.stdout


def test_download_library_verifies_size_and_sha256_without_network(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"verified artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    valid = _run_zsh(
        'source "$1"; lvt_verify_file "$2" "$3" "$4"',
        str(DOWNLOAD),
        str(artifact),
        digest,
        str(artifact.stat().st_size),
    )
    bad_digest = _run_zsh(
        'source "$1"; lvt_verify_file "$2" "$3" "$4"',
        str(DOWNLOAD),
        str(artifact),
        "0" * 64,
        str(artifact.stat().st_size),
    )
    bad_size = _run_zsh(
        'source "$1"; lvt_verify_file "$2" "$3" "$4"',
        str(DOWNLOAD),
        str(artifact),
        digest,
        str(artifact.stat().st_size + 1),
    )

    assert valid.returncode == 0
    assert bad_digest.returncode != 0
    assert bad_size.returncode != 0


def test_download_library_rejects_http_and_unsafe_destination_before_curl(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "curl-called"
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/zsh\n: > {marker!s}\nexit 99\n", encoding="utf-8")
    curl.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    controlled = tmp_path / "downloads"
    controlled.mkdir()

    http = _run_zsh(
        'source "$1"; lvt_download_verified "$2" "$3" "$4" "$5" "$6"',
        str(DOWNLOAD),
        "http://example.invalid/file",
        str(controlled),
        "file.bin",
        "0" * 64,
        "1",
        environment=environment,
    )
    traversal = _run_zsh(
        'source "$1"; lvt_download_verified "$2" "$3" "$4" "$5" "$6"',
        str(DOWNLOAD),
        "https://example.invalid/file",
        str(controlled),
        "../escape",
        "0" * 64,
        "1",
        environment=environment,
    )

    assert http.returncode != 0
    assert traversal.returncode != 0
    assert not marker.exists()


def test_process_library_reports_current_process_identity() -> None:
    completed = _run_zsh(
        'source "$1"; value="$(lvt_process_start_time "$$")"; [[ -n "$value" ]]; print -r -- ok',
        str(PROCESS),
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"


def test_doctor_command_resolves_relocated_release_with_spaces_and_unicode(
    tmp_path: Path,
) -> None:
    release = tmp_path / "发布 目录" / "Local Video Transcriber"
    shutil.copytree(ROOT / "scripts", release / "scripts")
    shutil.copytree(ROOT / "packaging" / "tools", release / "packaging" / "tools")
    shutil.copytree(ROOT / "packaging" / "schemas", release / "packaging" / "schemas")
    (release / "packaging" / "dependencies.json").write_bytes(
        (ROOT / "packaging" / "dependencies.json").read_bytes()
    )
    (release / "backend" / "src" / "lvt").mkdir(parents=True)
    (release / "backend" / "pyproject.toml").write_text(
        '[project]\nname="local-video-transcriber"\nversion="0.1.0"\n',
        encoding="utf-8",
    )
    (release / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    python = release / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f'#!/bin/zsh\nexec {str(sys.executable)!r} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for command in ("python3", "ffmpeg", "ffprobe", "ollama"):
        path = fake_bin / command
        path.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    environment = os.environ.copy()
    home = tmp_path / "临时 HOME"
    home.mkdir()
    environment.update(
        {
            "HOME": str(home),
            "PATH": str(fake_bin),
            "LVT_TEST_ROOT": str(tmp_path),
            "LVT_TEST_PLATFORM": "macos",
            "LVT_TEST_MACOS_VERSION": "13.0",
            "LVT_TEST_ARCH": "arm64",
            "LVT_TEST_ROSETTA": "0",
            "LVT_TEST_MEMORY_BYTES": str(16 * 1024**3),
            "LVT_TEST_DISK_BYTES": str(12 * 1024**3),
            "LVT_TEST_OLLAMA_PORT": "free",
            "LVT_TEST_BACKEND_HEALTH": "forbidden",
        }
    )

    completed = subprocess.run(
        [
            "/bin/zsh",
            str(release / "scripts" / "doctor.command"),
            "--json",
            "--phase",
            "staging-core",
            "--release-root",
            str(release),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "healthy"
    assert str(tmp_path) not in completed.stdout + completed.stderr


def test_two_concurrent_bootstrap_attempts_have_one_winner(tmp_path: Path) -> None:
    parent = tmp_path / "Application Support"
    parent.mkdir()
    code = f"""
import sys
sys.path.insert(0, {str(LOCK_MODULE.parent)!r})
from pathlib import Path
from lifecycle_lock import LifecycleLock, LockBusyError
lock = LifecycleLock(Path({str(parent)!r}), operation="install")
print("armed", flush=True)
sys.stdin.buffer.read(1)
try:
    lock.acquire_bootstrap()
except LockBusyError:
    print("busy", flush=True)
    raise SystemExit(0)
print("locked", flush=True)
sys.stdin.buffer.read(1)
lock.close()
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        for _ in range(2)
    ]
    for process in processes:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == b"armed"
    for process in processes:
        assert process.stdin is not None
        process.stdin.write(b"G")
        process.stdin.flush()
    results = [process.stdout.readline().strip() for process in processes]
    assert sorted(results) == [b"busy", b"locked"]
    winner = processes[results.index(b"locked")]
    assert winner.stdin is not None
    winner.stdin.write(b"R")
    winner.stdin.flush()
    for process in processes:
        assert process.wait(timeout=5) == 0


def test_bootstrap_to_flock_transition_has_no_unlocked_window(tmp_path: Path) -> None:
    module = _load_lock_module()
    parent = tmp_path / "Application Support"
    parent.mkdir()
    owner = module.LifecycleLock(parent, operation="install")
    owner.acquire_bootstrap()

    with pytest.raises(module.LockBusyError):
        module.LifecycleLock(parent, operation="install").acquire_bootstrap()

    owner.acquire_flock()
    assert owner.lock_fd is not None
    assert os.get_inheritable(owner.lock_fd) is False
    with pytest.raises(module.LockBusyError):
        module.LifecycleLock(parent, operation="start").acquire_flock()

    owner.release_bootstrap()
    with pytest.raises(module.LockBusyError):
        module.LifecycleLock(parent, operation="upgrade").acquire_flock()
    owner.close()


@pytest.mark.parametrize("operation", ["install", "start", "stop", "upgrade", "uninstall"])
def test_lifecycle_operations_share_one_permanent_flock(tmp_path: Path, operation: str) -> None:
    module = _load_lock_module()
    parent = tmp_path / "Application Support"
    parent.mkdir()
    owner = module.LifecycleLock(parent, operation="install")
    owner.acquire_flock()
    inode = owner.lock_path.stat().st_ino

    contender = module.LifecycleLock(parent, operation=operation)
    with pytest.raises(module.LockBusyError):
        contender.acquire_flock()
    contender.close()
    owner.close()

    assert owner.lock_path.exists()
    assert owner.lock_path.stat().st_ino == inode
    assert owner.lock_path.stat().st_mode & 0o777 == 0o600


def _lock_subprocess(parent: Path, action: str) -> subprocess.Popen[str]:
    code = f"""
import sys
sys.path.insert(0, {str(LOCK_MODULE.parent)!r})
from pathlib import Path
from lifecycle_lock import LifecycleLock
lock = LifecycleLock(Path({str(parent)!r}), operation="install")
getattr(lock, {action!r})()
print("ready", flush=True)
sys.stdin.buffer.read(1)
"""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_sigkill_releases_flock_without_replacing_lock_inode(tmp_path: Path) -> None:
    module = _load_lock_module()
    parent = tmp_path / "Application Support"
    parent.mkdir()
    process = _lock_subprocess(parent, "acquire_flock")
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    lock_path = parent / ".LocalVideoTranscriber.lifecycle" / "lock"
    inode = lock_path.stat().st_ino

    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    recovered = module.LifecycleLock(parent, operation="start")
    recovered.acquire_flock()
    recovered.close()

    assert lock_path.stat().st_ino == inode


def test_sigkill_stale_bootstrap_is_recovered_by_process_identity(tmp_path: Path) -> None:
    module = _load_lock_module()
    parent = tmp_path / "Application Support"
    parent.mkdir()
    process = _lock_subprocess(parent, "acquire_bootstrap")
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"

    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    recovered = module.LifecycleLock(parent, operation="install")
    recovered.acquire_bootstrap()
    recovered.close()

    assert not (parent / ".LocalVideoTranscriber.lifecycle" / "bootstrap.lock").exists()


def test_pid_reuse_recovers_stale_lease_but_live_owner_is_not_removed(
    tmp_path: Path,
) -> None:
    module = _load_lock_module()
    parent = tmp_path / "Application Support"
    parent.mkdir()
    owner = module.LifecycleLock(parent, operation="install")
    owner.acquire_bootstrap()
    lease_inode = owner.bootstrap_path.stat().st_ino

    live_contender = module.LifecycleLock(parent, operation="install")
    with pytest.raises(module.LockBusyError):
        live_contender.acquire_bootstrap()
    assert owner.bootstrap_path.stat().st_ino == lease_inode

    metadata_path = owner.bootstrap_path / "owner.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    owner.close()
    owner.bootstrap_path.mkdir()
    owner.bootstrap_path.chmod(0o700)
    metadata["start_time"] = "reused-old-start"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    metadata_path.chmod(0o600)

    class ReusedInspector:
        def identity(self, pid: int) -> str | None:
            assert pid == metadata["pid"]
            return "different-live-process-start"

    reused = module.LifecycleLock(
        parent,
        operation="install",
        process_inspector=ReusedInspector(),
    )
    reused.acquire_bootstrap()
    reused.close()


def test_corrupt_bootstrap_metadata_fails_closed(tmp_path: Path) -> None:
    module = _load_lock_module()
    parent = tmp_path / "Application Support"
    bootstrap = parent / ".LocalVideoTranscriber.lifecycle" / "bootstrap.lock"
    bootstrap.mkdir(parents=True)
    (bootstrap / "owner.json").write_text("{broken", encoding="utf-8")

    lock = module.LifecycleLock(parent, operation="install")
    with pytest.raises(module.LockUnsafeError):
        lock.acquire_bootstrap()
    assert bootstrap.exists()


def test_lifecycle_lock_rejects_symlinked_application_parent(tmp_path: Path) -> None:
    module = _load_lock_module()
    real_parent = tmp_path / "real Application Support"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked Application Support"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    lock = module.LifecycleLock(linked_parent, operation="install")
    with pytest.raises(module.LockUnsafeError):
        lock.acquire_flock()
