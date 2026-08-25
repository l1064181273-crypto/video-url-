#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

from lifecycle_lock import LifecycleLock

DATA_DIRECTORIES = ("config", "db", "runtime", "work", "exports", "logs", "models")
WRITABLE_DIRECTORIES = ("db", "runtime", "work", "exports", "logs")
COPY_PATHS = (
    "VERSION",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/src",
    "packaging/dependencies.json",
    "scripts",
)
PACKAGING_TOOLS = ("doctor.py", "install.py", "lifecycle_lock.py", "verify_install.py")
FAILURE_POINTS = {
    "before-uv",
    "after-uv",
    "before-python",
    "after-python",
    "before-venv-sync",
    "after-venv-sync",
    "before-token",
    "after-token",
    "before-extension-candidate",
    "after-extension-candidate",
}


class InstallError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path, *, allowed_symlink_roots: tuple[Path, ...] = ()) -> None:
    resolved_allowed = tuple(path.resolve(strict=True) for path in allowed_symlink_roots)
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if not any(_is_relative_to(resolved, allowed) for allowed in resolved_allowed):
                    raise InstallError("candidate contains an unsafe symlink")
                continue
            if not path.is_file():
                raise InstallError("candidate contains an unsafe file")
            _fsync_file(path)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if not any(_is_relative_to(resolved, allowed) for allowed in resolved_allowed):
                    raise InstallError("candidate contains an unsafe symlink")
                continue
            if not path.is_dir():
                raise InstallError("candidate contains an unsafe directory")
            _fsync_directory(path)
        _fsync_directory(current_path)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_controlled(path: Path, root: Path) -> None:
    if not path.is_absolute() or not root.is_absolute():
        raise InstallError("installation path is unsafe")
    resolved_root = root.resolve(strict=True)
    resolved_parent = path.parent.resolve(strict=True)
    if not _is_relative_to(resolved_parent, resolved_root):
        raise InstallError("installation path escapes the data root")
    if path.is_symlink():
        raise InstallError("installation path is a symlink")


def _prepare_data_root(data_root: Path) -> None:
    if not data_root.is_absolute() or _has_symlink_component(data_root):
        raise InstallError("data root is unsafe")
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data_root.is_symlink() or not data_root.is_dir():
        raise InstallError("data root is unsafe")
    for relative in ("app", *DATA_DIRECTORIES):
        path = data_root / relative
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise InstallError("data directory is unsafe")
        else:
            path.mkdir(mode=0o700)
            _fsync_directory(path.parent)
    for relative in WRITABLE_DIRECTORIES:
        metadata = (data_root / relative).stat()
        if metadata.st_mode & 0o200 == 0:
            raise InstallError("data directory is not writable")


def _load_dependencies(source_root: Path) -> dict[str, Any]:
    path = source_root / "packaging/dependencies.json"
    if path.is_symlink() or not path.is_file():
        raise InstallError("dependency manifest is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        artifacts = payload["artifacts"]
        if not isinstance(payload, dict) or not isinstance(artifacts, list):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError("dependency manifest is invalid") from exc
    return payload


def _artifact(dependencies: dict[str, Any], identifier: str) -> dict[str, Any]:
    for item in dependencies["artifacts"]:
        if isinstance(item, dict) and item.get("id") == identifier:
            required = ("url", "sha256", "size", "version", "expected_files")
            if not all(key in item for key in required):
                break
            return item
    raise InstallError("required tool metadata is unavailable")


def _test_injection(name: str) -> str | None:
    if not os.environ.get("LVT_TEST_ROOT"):
        return None
    return os.environ.get(name)


def _checkpoint(name: str) -> None:
    requested = _test_injection("LVT_TEST_FAIL_AT")
    if requested is not None and requested not in FAILURE_POINTS:
        raise InstallError("unknown failure checkpoint")
    if requested == name:
        raise InstallError("injected installation failure")


def _copy_regular_file(source: Path, destination: Path, mode: int | None = None) -> None:
    metadata = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("release source contains an unsafe file")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination, follow_symlinks=False)
    if mode is not None:
        destination.chmod(mode)


def _assert_tree_has_no_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise InstallError("release source contains an unsafe directory")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            if (current_path / name).is_symlink():
                raise InstallError("release source contains a symlink")


def _copy_source_path(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.exists():
        raise InstallError("release source is incomplete")
    if source.is_dir():
        _assert_tree_has_no_symlinks(source)
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    elif source.is_file():
        _copy_regular_file(source, destination)
    else:
        raise InstallError("release source is unsafe")


def _copy_release_core(source_root: Path, candidate: Path) -> None:
    candidate.mkdir(mode=0o700)
    for relative in COPY_PATHS:
        _copy_source_path(source_root / relative, candidate / relative)
    for name in PACKAGING_TOOLS:
        _copy_source_path(
            source_root / "packaging/tools" / name,
            candidate / "packaging/tools" / name,
        )


def _validate_extension_candidate(candidate: Path, version: str) -> None:
    manifest = candidate / "extension/manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            manifest.is_symlink()
            or not manifest.is_file()
            or not isinstance(payload, dict)
            or payload.get("version") != version
        ):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError("extension candidate is incomplete") from exc


def _copy_extension_candidate(source_root: Path, candidate: Path, version: str) -> None:
    source = source_root / "extension/dist"
    destination = candidate / "extension"
    _copy_source_path(source, destination)
    _validate_extension_candidate(candidate, version)


def _valid_uv(path: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            not path.is_symlink()
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_mode & 0o111 != 0
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1
        )
    except OSError:
        return False


def _valid_python(root: Path) -> bool:
    executable = root / "bin/python3"
    try:
        root_metadata = root.lstat()
        executable_metadata = executable.stat()
        resolved = executable.resolve(strict=True)
        return (
            not root.is_symlink()
            and stat.S_ISDIR(root_metadata.st_mode)
            and root_metadata.st_uid == os.getuid()
            and stat.S_ISREG(executable_metadata.st_mode)
            and executable_metadata.st_mode & 0o111 != 0
            and executable_metadata.st_uid == os.getuid()
            and executable_metadata.st_nlink == 1
            and _is_relative_to(resolved, root.resolve(strict=True))
        )
    except OSError:
        return False


def _download_archive(
    source_root: Path,
    tools_root: Path,
    artifact: dict[str, Any],
    identifier: str,
) -> Path:
    library = source_root / "scripts/lib/download.zsh"
    if library.is_symlink() or not library.is_file():
        raise InstallError("download helper is unavailable")
    relative = f".downloads/{identifier}-{artifact['version']}.archive"
    completed = subprocess.run(
        [
            "/bin/zsh",
            "-c",
            'source "$1"; lvt_download_verified "$2" "$3" "$4" "$5" "$6"',
            "lvt-download",
            str(library),
            str(artifact["url"]),
            str(tools_root),
            relative,
            str(artifact["sha256"]),
            str(artifact["size"]),
        ],
        close_fds=True,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise InstallError("verified tool download failed")
    return tools_root / relative


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, mode="r:*") as bundle:
            members = bundle.getmembers()
            for member in members:
                member_path = Path(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or member.islnk()
                    or member.isdev()
                ):
                    raise InstallError("tool archive contains an unsafe member")
                if member.issym():
                    link_target = Path(member.linkname)
                    if link_target.is_absolute():
                        raise InstallError("tool archive contains an unsafe symlink")
                    resolved_target = (
                        (destination / member.name)
                        .parent.joinpath(link_target)
                        .resolve(strict=False)
                    )
                    if not _is_relative_to(resolved_target, destination.resolve(strict=True)):
                        raise InstallError("tool archive contains an unsafe symlink")
            bundle.extractall(destination, members=members)
    except (OSError, tarfile.TarError) as exc:
        raise InstallError("tool archive extraction failed") from exc


def _install_uv(
    source_root: Path,
    tools_root: Path,
    artifact: dict[str, Any],
) -> bool:
    destination = tools_root / "uv"
    if destination.exists() or destination.is_symlink():
        if not _valid_uv(destination):
            raise InstallError("existing uv installation is unsafe")
        return False
    candidate = tools_root / f".uv.candidate.{uuid.uuid4().hex}"
    extract_root = tools_root / f".uv.extract.{uuid.uuid4().hex}"
    archive: Path | None = None
    published = False
    try:
        injected = _test_injection("LVT_TEST_UV_SOURCE")
        if injected is not None:
            _copy_regular_file(Path(injected), candidate, mode=0o755)
        else:
            archive = _download_archive(source_root, tools_root, artifact, "uv")
            _safe_extract_tar(archive, extract_root)
            expected_files = artifact["expected_files"]
            if not isinstance(expected_files, list) or not expected_files:
                raise InstallError("uv metadata is invalid")
            _copy_regular_file(extract_root / str(expected_files[0]), candidate, mode=0o755)
        _fsync_file(candidate)
        candidate.rename(destination)
        published = True
        _fsync_directory(tools_root)
        if not _valid_uv(destination):
            raise InstallError("uv installation did not validate")
    except Exception:
        if published and destination.exists():
            destination.unlink()
            _fsync_directory(tools_root)
        raise
    finally:
        if candidate.exists() or candidate.is_symlink():
            candidate.unlink()
        if extract_root.exists():
            shutil.rmtree(extract_root)
        if archive is not None and archive.exists():
            archive.unlink()
    return True


def _install_python(
    source_root: Path,
    tools_root: Path,
    artifact: dict[str, Any],
) -> bool:
    destination = tools_root / "python"
    if destination.exists() or destination.is_symlink():
        if not _valid_python(destination):
            raise InstallError("existing Python installation is unsafe")
        return False
    candidate = tools_root / f".python.candidate.{uuid.uuid4().hex}"
    extract_root = tools_root / f".python.extract.{uuid.uuid4().hex}"
    archive: Path | None = None
    published = False
    try:
        injected = _test_injection("LVT_TEST_PYTHON_SOURCE")
        if injected is not None:
            candidate.mkdir(mode=0o700)
            _copy_regular_file(Path(injected), candidate / "bin/python3", mode=0o755)
        else:
            archive = _download_archive(source_root, tools_root, artifact, "python")
            _safe_extract_tar(archive, extract_root)
            expected_files = artifact["expected_files"]
            if not isinstance(expected_files, list) or not expected_files:
                raise InstallError("Python metadata is invalid")
            first_path = Path(str(expected_files[0]))
            if not first_path.parts or first_path.parts[0] != "python":
                raise InstallError("Python archive layout is invalid")
            extracted = extract_root / "python"
            for path in extracted.rglob("*"):
                if path.is_symlink() and not _is_relative_to(
                    path.resolve(strict=True), extracted.resolve(strict=True)
                ):
                    raise InstallError("Python archive contains an unsafe symlink")
            extracted.rename(candidate)
        _fsync_tree(candidate, allowed_symlink_roots=(candidate,))
        candidate.rename(destination)
        published = True
        _fsync_directory(tools_root)
        if not _valid_python(destination):
            raise InstallError("Python installation did not validate")
    except Exception:
        if published and destination.exists():
            shutil.rmtree(destination)
            _fsync_directory(tools_root)
        raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)
        if extract_root.exists():
            shutil.rmtree(extract_root)
        if archive is not None and archive.exists():
            archive.unlink()
    return True


def _sync_venv(candidate: Path, uv_path: Path, python_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "UV_PROJECT_ENVIRONMENT": str(candidate / ".venv"),
            "UV_PYTHON": str(python_path),
            "UV_PYTHON_INSTALL_DIR": str(python_path.parents[1]),
            "LVT_INSTALL_PYTHON": str(python_path),
        }
    )
    completed = subprocess.run(
        [
            str(uv_path),
            "sync",
            "--project",
            str(candidate / "backend"),
            "--frozen",
            "--no-install-project",
        ],
        env=environment,
        close_fds=True,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise InstallError("frozen environment sync failed")
    executable = candidate / ".venv/bin/python"
    try:
        metadata = executable.stat()
        resolved = executable.resolve(strict=True)
        allowed = _is_relative_to(resolved, candidate.resolve(strict=True)) or _is_relative_to(
            resolved, python_path.parents[1].resolve(strict=True)
        )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0 or not allowed:
            raise InstallError("release Python is unsafe")
    except OSError as exc:
        raise InstallError("release Python is unavailable") from exc


def _ensure_token(data_root: Path) -> bool:
    token = data_root / "config/api-token"
    if token.exists() or token.is_symlink():
        try:
            metadata = token.lstat()
            if (
                token.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o777 != 0o600
                or metadata.st_nlink != 1
                or not 32 <= metadata.st_size <= 4096
            ):
                raise InstallError("existing token metadata is unsafe")
        except OSError as exc:
            raise InstallError("existing token cannot be validated") from exc
        return False
    partial = token.parent / f".api-token.partial.{uuid.uuid4().hex}"
    encoded = (secrets.token_urlsafe(32) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(partial, flags, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    published = False
    try:
        partial.rename(token)
        published = True
        _fsync_directory(token.parent)
    except Exception:
        if published and token.exists():
            token.unlink()
            _fsync_directory(token.parent)
        raise
    finally:
        if partial.exists():
            partial.unlink()
    return True


def _write_install_state(data_root: Path, version: str) -> None:
    state_path = data_root / "runtime/install-state.json"
    previous: bytes | None = None
    if state_path.exists() or state_path.is_symlink():
        if state_path.is_symlink() or not state_path.is_file():
            raise InstallError("install state is unsafe")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError
            previous = state_path.read_bytes()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise InstallError("install state is invalid") from exc
    else:
        state = {"schema_version": 1}
    state["core"] = {
        "release": f"app/releases/{version}",
        "verified": True,
        "version": version,
    }
    partial = state_path.parent / f".install-state.partial.{uuid.uuid4().hex}"
    encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        partial,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    published = False
    try:
        partial.rename(state_path)
        published = True
        _fsync_directory(state_path.parent)
    except Exception:
        if published:
            if previous is None:
                state_path.unlink(missing_ok=True)
            else:
                restore = state_path.parent / f".install-state.restore.{uuid.uuid4().hex}"
                restore.write_bytes(previous)
                restore.chmod(0o600)
                _fsync_file(restore)
                restore.replace(state_path)
            _fsync_directory(state_path.parent)
        raise
    finally:
        if partial.exists():
            partial.unlink()


def _release_version(source_root: Path) -> str:
    path = source_root / "VERSION"
    if path.is_symlink() or not path.is_file():
        raise InstallError("release version is unavailable")
    version = path.read_text(encoding="utf-8").strip()
    if not version or not all(part.isdigit() for part in version.split(".")):
        raise InstallError("release version is invalid")
    return version


def _validation_report(release: Path, data_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(release / ".venv/bin/python"),
            str(release / "packaging/tools/verify_install.py"),
            "--phase",
            "staging-core",
            "--data-root",
            str(data_root),
            "--release-root",
            str(release),
            "--json",
        ],
        close_fds=True,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError("staging validator did not return JSON") from exc
    if not isinstance(report, dict) or completed.returncode != 0 or report.get("exit_code") != 0:
        raise InstallError("staging validation failed")
    return report


def _validate_candidate(candidate: Path, data_root: Path) -> None:
    _validation_report(candidate, data_root)


def _existing_release_valid(release: Path, data_root: Path) -> bool:
    if release.is_symlink() or not release.exists():
        return False
    try:
        _validation_report(release, data_root)
        version = (release / "VERSION").read_text(encoding="utf-8").strip()
        _validate_extension_candidate(release, version)
    except (InstallError, OSError):
        return False
    return True


def _remove_created_path(path: Path, controlled_root: Path) -> None:
    _assert_controlled(path, controlled_root)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
    _fsync_directory(path.parent)


def install_staging_core(source_root: Path, data_root: Path) -> Path:
    source_root = source_root.resolve(strict=True)
    _prepare_data_root(data_root)
    app_root = data_root / "app"
    lock = LifecycleLock(app_root, operation="install")
    lock.acquire_bootstrap_then_flock()
    created: list[tuple[Path, Path]] = []
    candidate: Path | None = None
    published: Path | None = None
    try:
        tools_root = app_root / "tools"
        releases_root = app_root / "releases"
        for directory in (tools_root, releases_root):
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or not directory.is_dir():
                    raise InstallError("application layout is unsafe")
            else:
                directory.mkdir(mode=0o700)
                _fsync_directory(directory.parent)

        dependencies = _load_dependencies(source_root)
        _checkpoint("before-uv")
        uv_path = tools_root / "uv"
        if _install_uv(source_root, tools_root, _artifact(dependencies, "uv")):
            created.append((uv_path, data_root))
        _checkpoint("after-uv")

        _checkpoint("before-python")
        python_root = tools_root / "python"
        if _install_python(source_root, tools_root, _artifact(dependencies, "python")):
            created.append((python_root, data_root))
        python_path = python_root / "bin/python3"
        _checkpoint("after-python")

        version = _release_version(source_root)
        release = releases_root / version
        if release.exists() or release.is_symlink():
            if not _existing_release_valid(release, data_root):
                raise InstallError("existing release is unsafe")
        else:
            candidate = releases_root / f".{version}.candidate.{uuid.uuid4().hex}"
            _copy_release_core(source_root, candidate)
            _checkpoint("before-venv-sync")
            _sync_venv(candidate, uv_path, python_path)
            _checkpoint("after-venv-sync")

        _checkpoint("before-token")
        if _ensure_token(data_root):
            created.append((data_root / "config/api-token", data_root))
        _checkpoint("after-token")

        if candidate is not None:
            _checkpoint("before-extension-candidate")
            _copy_extension_candidate(source_root, candidate, version)
            _checkpoint("after-extension-candidate")
            _validate_candidate(candidate, data_root)
            _fsync_tree(candidate, allowed_symlink_roots=(candidate, tools_root))
            candidate.rename(release)
            published = release
            candidate = None
            _fsync_directory(releases_root)

        _write_install_state(data_root, version)
        published = None
        return release
    except Exception:
        if candidate is not None and candidate.exists():
            _remove_created_path(candidate, data_root)
        if published is not None and published.exists():
            _remove_created_path(published, data_root)
        for path, controlled_root in reversed(created):
            if path.exists() or path.is_symlink():
                _remove_created_path(path, controlled_root)
        raise
    finally:
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 Local Video Transcriber 核心组件")
    parser.add_argument("--phase", required=True, choices=("staging-core",))
    parser.add_argument("--data-root", type=Path)
    arguments = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parents[2]
    data_root = arguments.data_root or (
        Path.home() / "Library/Application Support/LocalVideoTranscriber"
    )
    try:
        install_staging_core(source_root, data_root)
    except Exception:
        print("[ERROR] INSTALL_FAILED：核心安装未完成", file=sys.stderr)
        return 2
    print("[INFO] INSTALL_STAGING_READY：核心候选版本已验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
