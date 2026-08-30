#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from lifecycle_lock import LifecycleLock
from runtime_layout import RuntimeLayout, path_is_link_like, runtime_layout

DATA_DIRECTORIES = ("config", "db", "runtime", "work", "exports", "logs", "models")
WRITABLE_DIRECTORIES = ("db", "runtime", "work", "exports", "logs")
COPY_PATHS = (
    "VERSION",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/src",
    "scripts",
)
PACKAGING_TOOLS = (
    "doctor.py",
    "install.py",
    "lifecycle_lock.py",
    "process_state.py",
    "reconcile_processes.py",
    "runtime_layout.py",
    "tool_supervisor.py",
    "verify_install.py",
)
WINDOWS_PACKAGING_TOOLS = (
    "provision.py",
    "transaction_journal.py",
    "windows_job.py",
    "windows_lifecycle.py",
    "windows_process.py",
    "windows_publication.py",
    "windows_publish_install.py",
    "windows_service.py",
    "windows_supervisor.py",
    "windows_tool_supervisor.py",
)
WINDOWS_INSTALL_TOOLS = (
    "install.py",
    "lifecycle_lock.py",
    "runtime_layout.py",
    "verify_install.py",
    *WINDOWS_PACKAGING_TOOLS,
)
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
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    access = os.O_RDWR if sys.platform == "win32" else os.O_RDONLY
    descriptor = os.open(path, access | getattr(os, "O_BINARY", 0))
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
            if path_is_link_like(path):
                resolved = path.resolve(strict=True)
                if not any(_is_relative_to(resolved, allowed) for allowed in resolved_allowed):
                    raise InstallError("candidate contains an unsafe symlink")
                continue
            if not path.is_file():
                raise InstallError("candidate contains an unsafe file")
            _fsync_file(path)
        for name in directories:
            path = current_path / name
            if path_is_link_like(path):
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
        if path_is_link_like(current):
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


def _layout_path(root: Path, relative: str) -> Path:
    return root.joinpath(*relative.split("/"))


def _owned_by_current_user(metadata: os.stat_result, layout: RuntimeLayout) -> bool:
    getuid = getattr(os, "getuid", None)
    return layout.system == "win32" or getuid is None or metadata.st_uid == getuid()


def _executable_metadata_valid(metadata: os.stat_result, layout: RuntimeLayout) -> bool:
    return layout.system == "win32" or metadata.st_mode & 0o111 != 0


def _assert_controlled(path: Path, root: Path) -> None:
    if not path.is_absolute() or not root.is_absolute():
        raise InstallError("installation path is unsafe")
    resolved_root = root.resolve(strict=True)
    resolved_parent = path.parent.resolve(strict=True)
    if not _is_relative_to(resolved_parent, resolved_root):
        raise InstallError("installation path escapes the data root")
    if path_is_link_like(path):
        raise InstallError("installation path is a symlink")


def _prepare_data_root(data_root: Path) -> None:
    if not data_root.is_absolute() or _has_symlink_component(data_root):
        raise InstallError("data root is unsafe")
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path_is_link_like(data_root) or not data_root.is_dir():
        raise InstallError("data root is unsafe")
    for relative in ("app", *DATA_DIRECTORIES):
        path = data_root / relative
        if path.exists() or path_is_link_like(path):
            if path_is_link_like(path) or not path.is_dir():
                raise InstallError("data directory is unsafe")
        else:
            path.mkdir(mode=0o700)
            _fsync_directory(path.parent)
    for relative in WRITABLE_DIRECTORIES:
        metadata = (data_root / relative).stat()
        if metadata.st_mode & 0o200 == 0:
            raise InstallError("data directory is not writable")


def _load_dependencies(
    source_root: Path,
    *,
    system: str | None = None,
) -> dict[str, Any]:
    layout = runtime_layout(system)
    path = _dependency_manifest_path(source_root, layout)
    if path_is_link_like(path) or not path.is_file():
        raise InstallError("dependency manifest is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        artifacts = payload["artifacts"]
        ollama_models = payload["ollama_models"]
        expected_policy = {
            "allowed_schemes": ["https"],
            "allowed_architectures": [layout.architecture],
            "allow_floating_tags": False,
            "allow_runtime_digest_rewrite": False,
        }
        if (
            not isinstance(payload, dict)
            or not isinstance(artifacts, list)
            or not isinstance(ollama_models, list)
            or payload.get("target") != layout.target
            or payload.get("trust_policy") != expected_policy
            or any(
                not isinstance(item, dict) or item.get("architecture") != layout.architecture
                for item in [*artifacts, *ollama_models]
            )
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError("dependency manifest is invalid") from exc
    return payload


def _dependency_manifest_path(source_root: Path, layout: RuntimeLayout) -> Path:
    selected = source_root.joinpath(*layout.dependency_manifest.split("/"))
    if selected.exists() or path_is_link_like(selected):
        return selected
    canonical = source_root / "packaging" / "dependencies.json"
    if layout.dependency_manifest != "packaging/dependencies.json" and canonical.is_file():
        return canonical
    return selected


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
    if path_is_link_like(source) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("release source contains an unsafe file")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination, follow_symlinks=False)
    if mode is not None:
        destination.chmod(mode)


def _assert_tree_has_no_symlinks(root: Path) -> None:
    if path_is_link_like(root) or not root.is_dir():
        raise InstallError("release source contains an unsafe directory")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            if path_is_link_like(current_path / name):
                raise InstallError("release source contains a symlink")


def _copy_source_path(source: Path, destination: Path) -> None:
    if path_is_link_like(source) or not source.exists():
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


def _copy_release_core(
    source_root: Path,
    candidate: Path,
    *,
    system: str | None = None,
) -> None:
    layout = runtime_layout(system)
    candidate.mkdir(mode=0o700)
    for relative in COPY_PATHS:
        _copy_source_path(source_root / relative, candidate / relative)
    _copy_source_path(
        _dependency_manifest_path(source_root, layout),
        candidate / "packaging/dependencies.json",
    )
    tool_names = WINDOWS_INSTALL_TOOLS if layout.system == "win32" else PACKAGING_TOOLS
    for name in tool_names:
        _copy_source_path(
            source_root / "packaging/tools" / name,
            candidate / "packaging/tools" / name,
        )


def _validate_extension_candidate(candidate: Path, version: str) -> None:
    manifest = candidate / "extension/manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            path_is_link_like(manifest)
            or not manifest.is_file()
            or not isinstance(payload, dict)
            or payload.get("version") != version
        ):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError("extension candidate is incomplete") from exc


def _copy_extension_candidate(source_root: Path, candidate: Path, version: str) -> None:
    development_source = source_root / "extension/dist"
    source = development_source if development_source.is_dir() else source_root / "extension"
    destination = candidate / "extension"
    _copy_source_path(source, destination)
    _validate_extension_candidate(candidate, version)


def _valid_uv(path: Path, layout: RuntimeLayout | None = None) -> bool:
    selected = runtime_layout() if layout is None else layout
    try:
        metadata = path.lstat()
        return (
            not path_is_link_like(path)
            and stat.S_ISREG(metadata.st_mode)
            and _executable_metadata_valid(metadata, selected)
            and _owned_by_current_user(metadata, selected)
            and metadata.st_nlink == 1
        )
    except OSError:
        return False


def _valid_python(root: Path, layout: RuntimeLayout | None = None) -> bool:
    selected = runtime_layout() if layout is None else layout
    executable = _layout_path(root, selected.python_executable)
    try:
        root_metadata = root.lstat()
        executable_metadata = executable.stat()
        resolved = executable.resolve(strict=True)
        return (
            not path_is_link_like(root)
            and stat.S_ISDIR(root_metadata.st_mode)
            and _owned_by_current_user(root_metadata, selected)
            and stat.S_ISREG(executable_metadata.st_mode)
            and _executable_metadata_valid(executable_metadata, selected)
            and _owned_by_current_user(executable_metadata, selected)
            and executable_metadata.st_nlink == 1
            and _is_relative_to(resolved, root.resolve(strict=True))
        )
    except OSError:
        return False


def _validate_bootstrap_python_root(
    root: Path,
    layout: RuntimeLayout | None = None,
) -> None:
    selected = runtime_layout() if layout is None else layout
    if not root.is_absolute() or path_is_link_like(root) or not root.is_dir():
        raise InstallError("bootstrap Python root is unsafe")
    try:
        resolved_root = root.resolve(strict=True)
        root_metadata = root.lstat()
        if not _owned_by_current_user(root_metadata, selected):
            raise InstallError("bootstrap Python root is unsafe")
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            current_metadata = current_path.lstat()
            if (
                path_is_link_like(current_path)
                or not stat.S_ISDIR(current_metadata.st_mode)
                or not _owned_by_current_user(current_metadata, selected)
            ):
                raise InstallError("bootstrap Python tree is unsafe")
            for name in [*directories, *files]:
                path = current_path / name
                metadata = path.lstat()
                if not _owned_by_current_user(metadata, selected):
                    raise InstallError("bootstrap Python tree is unsafe")
                if path_is_link_like(path):
                    if not _is_relative_to(path.resolve(strict=True), resolved_root):
                        raise InstallError("bootstrap Python tree is unsafe")
                elif stat.S_ISDIR(metadata.st_mode):
                    continue
                elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise InstallError("bootstrap Python tree is unsafe")
    except OSError as exc:
        raise InstallError("bootstrap Python tree is unsafe") from exc
    if not _valid_python(root, selected):
        raise InstallError("bootstrap Python installation is invalid")


def _download_archive(
    source_root: Path,
    tools_root: Path,
    artifact: dict[str, Any],
    identifier: str,
    *,
    layout: RuntimeLayout | None = None,
) -> Path:
    selected = runtime_layout() if layout is None else layout
    if selected.system == "win32":
        return _download_archive_with_python(tools_root, artifact, identifier)
    library = source_root / "scripts/lib/download.zsh"
    if path_is_link_like(library) or not library.is_file():
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


def _download_archive_with_python(
    tools_root: Path,
    artifact: dict[str, Any],
    identifier: str,
) -> Path:
    url = artifact.get("url")
    version = artifact.get("version")
    expected_size = artifact.get("size")
    expected_sha256 = artifact.get("sha256")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not isinstance(version, str)
        or not version
        or type(expected_size) is not int
        or expected_size <= 0
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise InstallError("verified tool download failed")
    destination = tools_root / ".downloads" / f"{identifier}-{version}.archive"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = destination.parent / f".{destination.name}.partial.{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            partial,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "LocalVideoTranscriber-Installer/1"},
        )
        digest = hashlib.sha256()
        observed_size = 0
        with urllib.request.urlopen(request, timeout=60) as response:
            final_url = urlsplit(response.geturl())
            if (
                final_url.scheme != "https"
                or not final_url.netloc
                or final_url.username is not None
                or final_url.password is not None
            ):
                raise InstallError("verified tool download failed")
            while chunk := response.read(1024 * 1024):
                observed_size += len(chunk)
                if observed_size > expected_size:
                    raise InstallError("verified tool download failed")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise InstallError("verified tool download failed")
                    view = view[written:]
        if observed_size != expected_size or digest.hexdigest() != expected_sha256:
            raise InstallError("verified tool download failed")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        partial.replace(destination)
        _fsync_directory(destination.parent)
        return destination
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise InstallError("verified tool download failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        partial.unlink(missing_ok=True)


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


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                name = info.filename
                path = PurePosixPath(name)
                windows_path = PureWindowsPath(name)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (
                    not name
                    or "\\" in name
                    or ":" in name
                    or path.is_absolute()
                    or windows_path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise InstallError("tool archive contains an unsafe member")
            bundle.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError("tool archive extraction failed") from exc


def _install_uv(
    source_root: Path,
    tools_root: Path,
    artifact: dict[str, Any],
    *,
    layout: RuntimeLayout | None = None,
) -> bool:
    selected = runtime_layout() if layout is None else layout
    destination = tools_root / selected.uv_executable
    if destination.exists() or path_is_link_like(destination):
        if not _valid_uv(destination, selected):
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
            archive = _download_archive(
                source_root,
                tools_root,
                artifact,
                "uv",
                layout=selected,
            )
            if artifact.get("media_type") == "application/zip":
                _safe_extract_zip(archive, extract_root)
            else:
                _safe_extract_tar(archive, extract_root)
            expected_files = artifact["expected_files"]
            if not isinstance(expected_files, list) or not expected_files:
                raise InstallError("uv metadata is invalid")
            _copy_regular_file(
                _layout_path(extract_root, str(expected_files[0])),
                candidate,
                mode=0o755,
            )
        _fsync_file(candidate)
        candidate.rename(destination)
        published = True
        _fsync_directory(tools_root)
        if not _valid_uv(destination, selected):
            raise InstallError("uv installation did not validate")
    except Exception:
        if published and destination.exists():
            destination.unlink()
            _fsync_directory(tools_root)
        raise
    finally:
        if candidate.exists() or path_is_link_like(candidate):
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
    bootstrap_python_root: Path | None = None,
    *,
    layout: RuntimeLayout | None = None,
) -> bool:
    selected = runtime_layout() if layout is None else layout
    destination = tools_root / "python"
    if destination.exists() or path_is_link_like(destination):
        if not _valid_python(destination, selected):
            raise InstallError("existing Python installation is unsafe")
        return False
    candidate = tools_root / f".python.candidate.{uuid.uuid4().hex}"
    extract_root = tools_root / f".python.extract.{uuid.uuid4().hex}"
    archive: Path | None = None
    published = False
    try:
        injected = _test_injection("LVT_TEST_PYTHON_SOURCE")
        if bootstrap_python_root is not None:
            _validate_bootstrap_python_root(bootstrap_python_root, selected)
            shutil.copytree(bootstrap_python_root, candidate, symlinks=True)
        elif injected is not None:
            candidate.mkdir(mode=0o700)
            _copy_regular_file(
                Path(injected),
                _layout_path(candidate, selected.python_executable),
                mode=0o755,
            )
        else:
            archive = _download_archive(
                source_root,
                tools_root,
                artifact,
                "python",
                layout=selected,
            )
            _safe_extract_tar(archive, extract_root)
            expected_files = artifact["expected_files"]
            if not isinstance(expected_files, list) or not expected_files:
                raise InstallError("Python metadata is invalid")
            first_path = Path(str(expected_files[0]))
            if not first_path.parts or first_path.parts[0] != "python":
                raise InstallError("Python archive layout is invalid")
            extracted = extract_root / "python"
            for path in extracted.rglob("*"):
                if path_is_link_like(path) and not _is_relative_to(
                    path.resolve(strict=True), extracted.resolve(strict=True)
                ):
                    raise InstallError("Python archive contains an unsafe symlink")
            extracted.rename(candidate)
        _fsync_tree(candidate, allowed_symlink_roots=(candidate,))
        candidate.rename(destination)
        published = True
        _fsync_directory(tools_root)
        if not _valid_python(destination, selected):
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


def _sync_venv(
    candidate: Path,
    uv_path: Path,
    python_path: Path,
    layout: RuntimeLayout | None = None,
) -> None:
    selected = runtime_layout() if layout is None else layout
    command = [str(uv_path)]
    test_runtime_python = _test_injection("LVT_TEST_RUNTIME_PYTHON")
    if test_runtime_python is not None:
        command = [test_runtime_python, str(uv_path)]
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
            *command,
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
    executable = _layout_path(candidate, selected.venv_python)
    try:
        metadata = executable.stat()
        resolved = executable.resolve(strict=True)
        allowed = _is_relative_to(resolved, candidate.resolve(strict=True)) or _is_relative_to(
            resolved, python_path.parents[1].resolve(strict=True)
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not _executable_metadata_valid(metadata, selected)
            or not allowed
        ):
            raise InstallError("release Python is unsafe")
    except OSError as exc:
        raise InstallError("release Python is unavailable") from exc


def _ensure_token(
    data_root: Path,
    layout: RuntimeLayout | None = None,
) -> bool:
    selected = runtime_layout() if layout is None else layout
    token = data_root / "config/api-token"
    if token.exists() or path_is_link_like(token):
        try:
            metadata = token.lstat()
            if (
                path_is_link_like(token)
                or not stat.S_ISREG(metadata.st_mode)
                or not _owned_by_current_user(metadata, selected)
                or (selected.system != "win32" and metadata.st_mode & 0o777 != 0o600)
                or metadata.st_nlink != 1
                or not 32 <= metadata.st_size <= 4096
            ):
                raise InstallError("existing token metadata is unsafe")
        except OSError as exc:
            raise InstallError("existing token cannot be validated") from exc
        return False
    partial = token.parent / f".api-token.partial.{uuid.uuid4().hex}"
    encoded = (secrets.token_urlsafe(32) + "\n").encode("ascii")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
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
    if state_path.exists() or path_is_link_like(state_path):
        if path_is_link_like(state_path) or not state_path.is_file():
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
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0),
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
    if path_is_link_like(path) or not path.is_file():
        raise InstallError("release version is unavailable")
    version = path.read_text(encoding="utf-8").strip()
    if not version or not all(part.isdigit() for part in version.split(".")):
        raise InstallError("release version is invalid")
    return version


def _default_data_root(layout: RuntimeLayout) -> Path:
    if layout.system == "darwin":
        return Path.home() / "Library/Application Support/LocalVideoTranscriber"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise InstallError("LOCALAPPDATA is unavailable")
    root = Path(local_app_data)
    if not root.is_absolute() or str(root).startswith("\\\\"):
        raise InstallError("LOCALAPPDATA is unsafe")
    return root / "LocalVideoTranscriber"


def _validation_report(
    release: Path,
    data_root: Path,
    layout: RuntimeLayout | None = None,
) -> dict[str, Any]:
    selected = runtime_layout() if layout is None else layout
    validator_python = str(_layout_path(release, selected.venv_python))
    test_runtime_python = _test_injection("LVT_TEST_RUNTIME_PYTHON")
    if selected.system == "win32" and test_runtime_python is not None:
        validator_python = test_runtime_python
    completed = subprocess.run(
        [
            validator_python,
            str(release / "packaging/tools/verify_install.py"),
            "--phase",
            "staging-core",
            "--data-root",
            str(data_root),
            "--release-root",
            str(release),
            "--target",
            selected.target,
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
        checks = report.get("checks") if isinstance(report, dict) else None
        failed_codes = (
            [
                str(check.get("code"))
                for check in checks
                if isinstance(check, dict) and check.get("status") != "ok"
            ]
            if isinstance(checks, list)
            else []
        )
        suffix = f": {','.join(failed_codes)}" if failed_codes else ""
        raise InstallError(f"staging validation failed{suffix}")
    return report


def _validate_candidate(
    candidate: Path,
    data_root: Path,
    layout: RuntimeLayout | None = None,
) -> None:
    _validation_report(candidate, data_root, layout)


def _existing_release_valid(
    release: Path,
    data_root: Path,
    layout: RuntimeLayout | None = None,
) -> bool:
    if path_is_link_like(release) or not release.exists():
        return False
    try:
        _validation_report(release, data_root, layout)
        version = (release / "VERSION").read_text(encoding="utf-8").strip()
        _validate_extension_candidate(release, version)
    except (InstallError, OSError):
        return False
    return True


def _remove_created_path(path: Path, controlled_root: Path) -> None:
    _assert_controlled(path, controlled_root)
    if path.is_dir() and not path_is_link_like(path):
        shutil.rmtree(path)
    elif path.exists() or path_is_link_like(path):
        path.unlink()
    _fsync_directory(path.parent)


def install_staging_core(
    source_root: Path,
    data_root: Path,
    bootstrap_python_root: Path | None = None,
    *,
    system: str | None = None,
) -> Path:
    layout = runtime_layout(system)
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
            if directory.exists() or path_is_link_like(directory):
                if path_is_link_like(directory) or not directory.is_dir():
                    raise InstallError("application layout is unsafe")
            else:
                directory.mkdir(mode=0o700)
                _fsync_directory(directory.parent)

        dependencies = _load_dependencies(source_root, system=layout.system)
        _checkpoint("before-uv")
        uv_path = tools_root / layout.uv_executable
        if _install_uv(
            source_root,
            tools_root,
            _artifact(dependencies, "uv"),
            layout=layout,
        ):
            created.append((uv_path, data_root))
        _checkpoint("after-uv")

        _checkpoint("before-python")
        python_root = tools_root / "python"
        if _install_python(
            source_root,
            tools_root,
            _artifact(dependencies, "python"),
            bootstrap_python_root,
            layout=layout,
        ):
            created.append((python_root, data_root))
        python_path = _layout_path(python_root, layout.python_executable)
        _checkpoint("after-python")

        version = _release_version(source_root)
        release = releases_root / version
        if release.exists() or path_is_link_like(release):
            if not _existing_release_valid(release, data_root, layout):
                raise InstallError("existing release is unsafe")
        else:
            candidate = releases_root / f".{version}.candidate.{uuid.uuid4().hex}"
            _copy_release_core(source_root, candidate, system=layout.system)
            _checkpoint("before-venv-sync")
            _sync_venv(candidate, uv_path, python_path, layout)
            _checkpoint("after-venv-sync")

        _checkpoint("before-token")
        if _ensure_token(data_root, layout):
            created.append((data_root / "config/api-token", data_root))
        _checkpoint("after-token")

        if candidate is not None:
            _checkpoint("before-extension-candidate")
            _copy_extension_candidate(source_root, candidate, version)
            _checkpoint("after-extension-candidate")
            _validate_candidate(candidate, data_root, layout)
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
            if path.exists() or path_is_link_like(path):
                _remove_created_path(path, controlled_root)
        raise
    finally:
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 Local Video Transcriber 核心组件")
    parser.add_argument("--phase", required=True, choices=("staging-core",))
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--bootstrap-python-root", type=Path)
    arguments = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parents[2]
    try:
        layout = runtime_layout()
        data_root = arguments.data_root or _default_data_root(layout)
        install_staging_core(source_root, data_root, arguments.bootstrap_python_root)
    except Exception as exc:
        detail = " ".join(str(exc).split())[:512]
        print(
            f"[ERROR] INSTALL_FAILED: {type(exc).__name__}: {detail}",
            file=sys.stderr,
        )
        return 2
    print("[INFO] INSTALL_STAGING_READY: Core candidate verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
