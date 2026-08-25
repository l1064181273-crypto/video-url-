#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from lifecycle_lock import LifecycleLock

MODEL_ARTIFACT_IDS = (
    "asr-whisper-small-mlx-config",
    "asr-whisper-small-mlx-weights",
    "diarization-segmentation",
    "diarization-embedding",
    "hy-mt2",
)
REQUIRED_PACKAGES = ("mlx_whisper", "sherpa_onnx")
QWEN_ID = "qwen2.5-1.5b"
HY_MODEL = "hy-mt2:1.8b-q4km-fixed"
OLLAMA_ORIGIN = "http://127.0.0.1:11435"
ARM64_CPU_TYPE = 0x0100000C


class ProvisionError(RuntimeError):
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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _safe_join(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or PureWindowsPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ProvisionError("unsafe relative path")
    candidate = root.joinpath(*relative.split("/"))
    try:
        candidate.parent.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProvisionError("path escapes controlled root") from exc
    return candidate


def _prepare_roots(data_root: Path, release_root: Path) -> None:
    if (
        not data_root.is_absolute()
        or not release_root.is_absolute()
        or _has_symlink_component(data_root)
        or _has_symlink_component(release_root)
        or data_root.is_symlink()
        or release_root.is_symlink()
        or not data_root.is_dir()
        or not release_root.is_dir()
    ):
        raise ProvisionError("installation roots are unsafe")
    try:
        release_root.resolve(strict=True).relative_to(
            (data_root / "app/releases").resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise ProvisionError("release candidate is outside the application root") from exc
    for relative in ("app/downloads", "app/tools", "models", "models/quarantine", "runtime"):
        path = _safe_join(data_root, relative)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                raise ProvisionError("installation directory is unsafe")
        else:
            path.mkdir(mode=0o700, parents=True)
            _fsync_directory(path.parent)


def _load_dependencies(release_root: Path) -> dict[str, Any]:
    path = _safe_join(release_root, "packaging/dependencies.json")
    if path.is_symlink() or not path.is_file():
        raise ProvisionError("dependency manifest is unavailable")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError("dependency manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise ProvisionError("dependency manifest is invalid")
    policy = payload.get("trust_policy")
    expected_policy = {
        "allowed_schemes": ["https"],
        "allowed_architectures": ["arm64"],
        "allow_floating_tags": False,
        "allow_runtime_digest_rewrite": False,
    }
    if policy != expected_policy or payload.get("target") != "macos-arm64":
        raise ProvisionError("dependency trust policy is invalid")
    items = [*payload.get("artifacts", []), *payload.get("ollama_models", [])]
    if any(not isinstance(item, dict) or item.get("architecture") != "arm64" for item in items):
        raise ProvisionError("dependency architecture is invalid")
    return payload


def _artifact(dependencies: dict[str, Any], identifier: str) -> dict[str, Any]:
    for item in dependencies.get("artifacts", []):
        if isinstance(item, dict) and item.get("id") == identifier:
            return item
    raise ProvisionError("required dependency metadata is unavailable")


def _qwen_model(dependencies: dict[str, Any]) -> dict[str, Any]:
    for item in dependencies.get("ollama_models", []):
        if isinstance(item, dict) and item.get("id") == QWEN_ID:
            return item
    raise ProvisionError("qwen dependency metadata is unavailable")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _verified_file(
    path: Path,
    expected_size: Any,
    expected_sha256: Any,
    *,
    executable: bool = False,
    arm64: bool = False,
) -> bool:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or type(expected_size) is not int
            or expected_size <= 0
            or metadata.st_size != expected_size
            or not _valid_digest(expected_sha256)
            or _sha256(path) != expected_sha256
            or (executable and metadata.st_mode & 0o111 == 0)
            or (arm64 and not _is_arm64_macho(path))
        ):
            return False
    except OSError:
        return False
    return True


def _is_arm64_macho(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(4096)
    except OSError:
        return False
    if len(header) < 8:
        return False
    magic = header[:4]
    if magic == b"\xcf\xfa\xed\xfe":
        return int(struct.unpack("<I", header[4:8])[0]) == ARM64_CPU_TYPE
    if magic == b"\xfe\xed\xfa\xcf":
        return int(struct.unpack(">I", header[4:8])[0]) == ARM64_CPU_TYPE
    if magic not in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"} or len(header) < 8:
        return False
    count = struct.unpack(">I", header[4:8])[0]
    entry_size = 32 if magic == b"\xca\xfe\xba\xbf" else 20
    for index in range(count):
        offset = 8 + index * entry_size
        if offset + 4 > len(header):
            return False
        if struct.unpack(">I", header[offset : offset + 4])[0] == ARM64_CPU_TYPE:
            return True
    return False


def _test_setting(name: str) -> str | None:
    if not os.environ.get("LVT_TEST_ROOT"):
        return None
    return os.environ.get(name)


def _download_library(source_root: Path) -> Path:
    injected = _test_setting("LVT_TEST_DOWNLOAD_LIBRARY")
    library = Path(injected) if injected else source_root / "scripts/lib/download.zsh"
    if library.is_symlink() or not library.is_file():
        raise ProvisionError("download helper is unavailable")
    return library


def _effective_url(url: Any, identifier: str) -> str:
    if not isinstance(url, str):
        raise ProvisionError("dependency URL is invalid")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisionError("dependency URL violates trust policy")
    origin = _test_setting("LVT_TEST_DOWNLOAD_ORIGIN")
    if origin is None:
        return url
    test_origin = urlsplit(origin)
    if (
        test_origin.scheme != "http"
        or test_origin.hostname != "127.0.0.1"
        or test_origin.username is not None
        or test_origin.password is not None
        or test_origin.query
        or test_origin.fragment
        or test_origin.path not in {"", "/"}
    ):
        raise ProvisionError("test download origin is unsafe")
    try:
        behaviors = json.loads(_test_setting("LVT_TEST_DOWNLOAD_BEHAVIOR") or "{}")
    except json.JSONDecodeError as exc:
        raise ProvisionError("test download behavior is invalid") from exc
    behavior = behaviors.get(identifier, "normal") if isinstance(behaviors, dict) else None
    if behavior not in {"normal", "resume", "truncate", "corrupt", "redirect-http"}:
        raise ProvisionError("test download behavior is invalid")
    path = f"/{behavior}{parsed.path}"
    return urlunsplit((test_origin.scheme, test_origin.netloc, path, "", ""))


def _download_verified(
    source_root: Path,
    url: Any,
    controlled_root: Path,
    relative_destination: str,
    expected_sha256: Any,
    expected_size: Any,
    identifier: str,
) -> Path:
    if not _valid_digest(expected_sha256) or type(expected_size) is not int or expected_size <= 0:
        raise ProvisionError("dependency integrity metadata is invalid")
    destination = _safe_join(controlled_root, relative_destination)
    effective_url = _effective_url(url, identifier)
    completed = subprocess.run(
        [
            "/bin/zsh",
            "-c",
            'source "$1"; lvt_download_verified "$2" "$3" "$4" "$5" "$6"',
            "lvt-download",
            str(_download_library(source_root)),
            effective_url,
            str(controlled_root),
            relative_destination,
            expected_sha256,
            str(expected_size),
        ],
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not _verified_file(
        destination,
        expected_size,
        expected_sha256,
    ):
        raise ProvisionError("verified dependency download failed")
    return destination


def _quarantine(data_root: Path, path: Path, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    models_root = _safe_join(data_root, "models")
    resolved_parent = path.parent.resolve(strict=True)
    allowed_roots = (models_root.resolve(strict=True), (data_root / "app").resolve(strict=True))
    if not any(_is_relative_to(resolved_parent, root) for root in allowed_roots):
        raise ProvisionError("quarantine source is outside controlled roots")
    destination_root = _safe_join(
        models_root,
        f"quarantine/{label}-{uuid.uuid4().hex}",
    )
    destination_root.mkdir(mode=0o700, parents=True)
    destination = destination_root / path.name
    path.rename(destination)
    _fsync_directory(path.parent)
    _fsync_directory(destination_root)


def _publish_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = path.parent / f".{path.name}.candidate.{uuid.uuid4().hex}"
    descriptor = os.open(
        partial,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        partial.replace(path)
        _fsync_directory(path.parent)
    finally:
        partial.unlink(missing_ok=True)


def _ensure_archive(
    source_root: Path,
    data_root: Path,
    artifact: dict[str, Any],
) -> Path:
    identifier = artifact.get("id")
    version = artifact.get("version")
    if not isinstance(identifier, str) or not isinstance(version, str):
        raise ProvisionError("archive metadata is invalid")
    media_type = artifact.get("media_type")
    extension = {
        "application/zip": "zip",
        "application/x-bzip2": "tar.bz2",
    }.get(media_type if isinstance(media_type, str) else "", "archive")
    relative = f"downloads/{identifier}-{version}.{extension}"
    cache_root = _safe_join(data_root, "app")
    destination = _safe_join(cache_root, relative)
    if _verified_file(destination, artifact.get("size"), artifact.get("sha256")):
        return destination
    if destination.exists() or destination.is_symlink():
        _quarantine(data_root, destination, identifier)
    return _download_verified(
        source_root,
        artifact.get("url"),
        cache_root,
        relative,
        artifact.get("sha256"),
        artifact.get("size"),
        identifier,
    )


def _zip_members(archive: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (
                    path.is_absolute()
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or stat.S_ISLNK(mode)
                ):
                    raise ProvisionError("archive contains an unsafe member")
                if info.is_dir():
                    continue
                members[path.as_posix()] = bundle.read(info)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProvisionError("dependency archive is invalid") from exc
    return members


def _install_archive_tool(
    source_root: Path,
    data_root: Path,
    artifact: dict[str, Any],
    *,
    tool_name: str,
) -> tuple[Path, dict[str, str]]:
    archive = _ensure_archive(source_root, data_root, artifact)
    members = _zip_members(archive)
    expected_files = artifact.get("expected_files")
    version = artifact.get("version")
    if not isinstance(expected_files, list) or not isinstance(version, str):
        raise ProvisionError("tool archive metadata is invalid")
    if tool_name == "ffmpeg":
        sources = {
            name: next(
                (
                    member
                    for member in expected_files
                    if isinstance(member, str) and PurePosixPath(member).name == name
                ),
                None,
            )
            for name in ("ffmpeg", "ffprobe")
        }
    else:
        executable = artifact.get("executable")
        sources = {"ollama": executable if isinstance(executable, str) else None}
    if any(source not in members for source in sources.values()):
        raise ProvisionError("tool archive is missing a required executable")
    destination_dir = _safe_join(
        data_root,
        f"app/tools/{tool_name}/{version}/bin",
    )
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for name, source in sources.items():
        assert source is not None
        content = members[source]
        digest = hashlib.sha256(content).hexdigest()
        destination = destination_dir / name
        if not _verified_file(
            destination,
            len(content),
            digest,
            executable=True,
            arm64=True,
        ):
            if destination.exists() or destination.is_symlink():
                _quarantine(data_root, destination, f"{tool_name}-{name}")
            _publish_bytes(destination, content, mode=0o700)
            if not _verified_file(
                destination,
                len(content),
                digest,
                executable=True,
                arm64=True,
            ):
                _quarantine(data_root, destination, f"{tool_name}-{name}")
                raise ProvisionError("installed executable failed verification")
        digests[name] = digest
    return destination_dir, digests


def _installed_ffmpeg(
    data_root: Path,
    artifact: dict[str, Any],
) -> tuple[Path, dict[str, str]] | None:
    state_path = _safe_join(data_root, "runtime/install-state.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        metadata = state["ffmpeg"]
        version = artifact["version"]
        directory = metadata["directory"]
        digests = metadata["sha256"]
        if (
            not isinstance(version, str)
            or not isinstance(directory, str)
            or Path(directory).parts[-2:] != (version, "bin")
            or not isinstance(digests, dict)
            or set(digests) != {"ffmpeg", "ffprobe"}
        ):
            return None
        ffmpeg_dir = _safe_join(_safe_join(data_root, "app"), directory)
        for name in ("ffmpeg", "ffprobe"):
            path = ffmpeg_dir / name
            size = path.lstat().st_size
            if not _verified_file(
                path,
                size,
                digests.get(name),
                executable=True,
                arm64=True,
            ):
                return None
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return ffmpeg_dir, digests


def _install_direct_model(
    source_root: Path,
    data_root: Path,
    artifact: dict[str, Any],
) -> Path:
    expected_files = artifact.get("expected_files")
    identifier = artifact.get("id")
    if (
        not isinstance(expected_files, list)
        or len(expected_files) != 1
        or not isinstance(expected_files[0], str)
        or not isinstance(identifier, str)
    ):
        raise ProvisionError("model metadata is invalid")
    destination = _safe_join(data_root, expected_files[0])
    expected_size = artifact.get("expected_file_size", artifact.get("size"))
    expected_sha256 = artifact.get("expected_file_sha256", artifact.get("sha256"))
    if _verified_file(destination, expected_size, expected_sha256):
        return destination
    if destination.exists() or destination.is_symlink():
        _quarantine(data_root, destination, identifier)
    candidate_root = _safe_join(
        data_root,
        f"models/.{identifier}.candidate.{uuid.uuid4().hex}",
    )
    candidate_root.mkdir(mode=0o700)
    try:
        candidate = _download_verified(
            source_root,
            artifact.get("url"),
            candidate_root,
            "payload",
            artifact.get("sha256"),
            artifact.get("size"),
            identifier,
        )
        if not _verified_file(candidate, expected_size, expected_sha256):
            raise ProvisionError("downloaded model failed final verification")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        candidate.replace(destination)
        _fsync_directory(destination.parent)
    except Exception:
        _quarantine(data_root, candidate_root, identifier)
        raise
    finally:
        if candidate_root.exists():
            shutil.rmtree(candidate_root)
    return destination


def _install_segmentation(
    source_root: Path,
    data_root: Path,
    artifact: dict[str, Any],
) -> Path:
    expected_files = artifact.get("expected_files")
    if (
        artifact.get("media_type") != "application/x-bzip2"
        or not isinstance(expected_files, list)
        or len(expected_files) != 1
        or not isinstance(expected_files[0], str)
    ):
        raise ProvisionError("segmentation archive metadata is invalid")
    destination = _safe_join(data_root, expected_files[0])
    expected_size = artifact.get("expected_file_size")
    expected_sha256 = artifact.get("expected_file_sha256")
    if _verified_file(destination, expected_size, expected_sha256):
        return destination
    if destination.exists() or destination.is_symlink():
        _quarantine(data_root, destination, "diarization-segmentation")
    archive = _ensure_archive(source_root, data_root, artifact)
    candidate_root = _safe_join(
        data_root,
        f"models/.diarization-segmentation.candidate.{uuid.uuid4().hex}",
    )
    candidate_root.mkdir(mode=0o700)
    try:
        matching: list[tarfile.TarInfo] = []
        with tarfile.open(archive, mode="r:*") as bundle:
            for member in bundle.getmembers():
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or any(part in {"", ".", ".."} for part in member_path.parts)
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                    or not (member.isfile() or member.isdir())
                ):
                    raise ProvisionError("segmentation archive contains an unsafe member")
                if member.isfile() and member_path.name == "model.onnx":
                    matching.append(member)
            if len(matching) != 1:
                raise ProvisionError("segmentation archive model is ambiguous")
            stream = bundle.extractfile(matching[0])
            if stream is None:
                raise ProvisionError("segmentation archive model is unavailable")
            content = stream.read()
        candidate = candidate_root / "model.onnx"
        _publish_bytes(candidate, content)
        if not _verified_file(candidate, expected_size, expected_sha256):
            raise ProvisionError("extracted segmentation model failed verification")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        candidate.replace(destination)
        _fsync_directory(destination.parent)
    except Exception:
        _quarantine(data_root, candidate_root, "diarization-segmentation")
        if archive.exists():
            _quarantine(data_root, archive, "diarization-segmentation-archive")
        raise
    finally:
        if candidate_root.exists():
            shutil.rmtree(candidate_root)
    return destination


def _qwen_blob_url(manifest_url: str, digest: str) -> str:
    parsed = urlsplit(manifest_url)
    marker = "/manifests/"
    if marker not in parsed.path:
        raise ProvisionError("qwen manifest URL is invalid")
    prefix = parsed.path.split(marker, 1)[0]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{prefix}/blobs/{digest}", "", ""))


def _validated_qwen_blobs(model: dict[str, Any]) -> list[dict[str, Any]]:
    blobs = model.get("blobs")
    if not isinstance(blobs, list) or len(blobs) != 5:
        raise ProvisionError("qwen blob contract is invalid")
    for blob in blobs:
        if not isinstance(blob, dict):
            raise ProvisionError("qwen blob contract is invalid")
        digest = blob.get("digest")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not _valid_digest(digest[7:])
            or type(blob.get("size")) is not int
            or blob["size"] <= 0
            or not isinstance(blob.get("media_type"), str)
        ):
            raise ProvisionError("qwen blob contract is invalid")
    return blobs


def _manifest_descriptors(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise ProvisionError("qwen manifest is invalid")
    media_type = payload.get("mediaType")
    config = payload.get("config")
    layers = payload.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list):
        raise ProvisionError("qwen manifest is invalid")
    descriptors: list[dict[str, Any]] = []
    for descriptor in [config, *layers]:
        if not isinstance(descriptor, dict):
            raise ProvisionError("qwen manifest is invalid")
        descriptors.append(
            {
                "digest": descriptor.get("digest"),
                "media_type": descriptor.get("mediaType"),
                "size": descriptor.get("size"),
            }
        )
    return str(media_type), descriptors


def _install_qwen(
    source_root: Path,
    data_root: Path,
    model: dict[str, Any],
) -> dict[str, Any]:
    blobs = _validated_qwen_blobs(model)
    expected_files = model.get("expected_files")
    manifest_url = model.get("manifest_url")
    if (
        not isinstance(expected_files, list)
        or len(expected_files) != 1
        or not isinstance(expected_files[0], str)
        or not isinstance(manifest_url, str)
    ):
        raise ProvisionError("qwen manifest contract is invalid")
    manifest_path = _safe_join(data_root, expected_files[0])
    valid_manifest = _verified_file(
        manifest_path,
        model.get("manifest_size"),
        model.get("manifest_sha256"),
    )
    valid_blobs: dict[str, Path] = {}
    for index, blob in enumerate(blobs):
        digest = blob["digest"][7:]
        path = _safe_join(data_root, f"models/ollama/blobs/sha256-{digest}")
        if _verified_file(path, blob["size"], digest):
            valid_blobs[str(index)] = path
    if valid_manifest and len(valid_blobs) == len(blobs):
        return {
            "verified": True,
            "manifest_sha256": model["manifest_sha256"],
            "manifest_size": model["manifest_size"],
            "manifest_media_type": model["manifest_media_type"],
            "blobs": blobs,
        }
    if manifest_path.exists() or manifest_path.is_symlink():
        _quarantine(data_root, manifest_path, "qwen-manifest")
    for index, blob in enumerate(blobs):
        if str(index) in valid_blobs:
            continue
        digest = blob["digest"][7:]
        path = _safe_join(data_root, f"models/ollama/blobs/sha256-{digest}")
        if path.exists() or path.is_symlink():
            _quarantine(data_root, path, f"qwen-blob-{index}")

    candidate_root = _safe_join(
        data_root,
        f"models/.qwen.candidate.{uuid.uuid4().hex}",
    )
    candidate_root.mkdir(mode=0o700)
    try:
        if valid_manifest:
            candidate_manifest = manifest_path
        else:
            candidate_manifest = _download_verified(
                source_root,
                manifest_url,
                candidate_root,
                "manifest",
                model.get("manifest_sha256"),
                model.get("manifest_size"),
                "qwen-manifest",
            )
        try:
            payload = json.loads(candidate_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvisionError("qwen manifest is invalid") from exc
        media_type, actual_blobs = _manifest_descriptors(payload)
        if media_type != model.get("manifest_media_type") or actual_blobs != blobs:
            raise ProvisionError("qwen manifest differs from the pinned contract")

        candidates: dict[int, Path] = {}
        for index, blob in enumerate(blobs):
            if str(index) in valid_blobs:
                continue
            digest = blob["digest"]
            candidates[index] = _download_verified(
                source_root,
                _qwen_blob_url(manifest_url, digest),
                candidate_root,
                f"blobs/sha256-{digest[7:]}",
                digest[7:],
                blob["size"],
                f"qwen-blob-{index}",
            )

        if not valid_manifest:
            manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            candidate_manifest.replace(manifest_path)
            _fsync_directory(manifest_path.parent)
        for index, candidate in candidates.items():
            digest = blobs[index]["digest"][7:]
            destination = _safe_join(
                data_root,
                f"models/ollama/blobs/sha256-{digest}",
            )
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            candidate.replace(destination)
            _fsync_directory(destination.parent)
    except Exception:
        _quarantine(data_root, candidate_root, "qwen")
        raise
    finally:
        if candidate_root.exists():
            shutil.rmtree(candidate_root)
    return {
        "verified": True,
        "manifest_sha256": model["manifest_sha256"],
        "manifest_size": model["manifest_size"],
        "manifest_media_type": model["manifest_media_type"],
        "blobs": blobs,
    }


def _verify_python_packages(release_root: Path) -> None:
    missing_injection = _test_setting("LVT_TEST_MISSING_PACKAGE")
    packages = [package for package in REQUIRED_PACKAGES if package != missing_injection]
    script = (
        "import importlib.util,sys;"
        f"sys.exit(0 if all(importlib.util.find_spec(name) for name in {packages!r}) else 1)"
    )
    completed = subprocess.run(
        [str(_safe_join(release_root, ".venv/bin/python")), "-c", script],
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or missing_injection in REQUIRED_PACKAGES:
        raise ProvisionError("required model runtime package is unavailable")


def _project_port_in_use(source_root: Path) -> bool:
    library = source_root / "scripts/lib/process.zsh"
    if library.is_symlink() or not library.is_file():
        raise ProvisionError("process helper is unavailable")
    completed = subprocess.run(
        [
            "/bin/zsh",
            "-c",
            'source "$1"; lvt_project_port_in_use 11435',
            "lvt-port",
            str(library),
        ],
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _ollama_json(path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_ORIGIN}{path}", timeout=1) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise ProvisionError("project Ollama is unavailable") from exc
    if not isinstance(payload, dict):
        raise ProvisionError("project Ollama returned invalid data")
    return payload


def _ollama_environment(data_root: Path) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", "/"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "OLLAMA_HOST": "127.0.0.1:11435",
        "OLLAMA_MODELS": str(_safe_join(data_root, "models/ollama")),
    }
    if tmpdir := os.environ.get("TMPDIR"):
        environment["TMPDIR"] = tmpdir
    for name in (
        "LVT_TEST_OLLAMA_STATE",
        "LVT_TEST_OLLAMA_AUDIT",
        "LVT_TEST_OLLAMA_CREATE_FAIL",
    ):
        if value := _test_setting(name):
            environment[name] = value
    return environment


class _OllamaSession:
    def __init__(self, source_root: Path, data_root: Path, executable: Path) -> None:
        self.source_root = source_root
        self.data_root = data_root
        self.executable = executable
        self.process: subprocess.Popen[bytes] | None = None
        self.environment = _ollama_environment(data_root)

    def __enter__(self) -> _OllamaSession:
        if _project_port_in_use(self.source_root):
            payload = _ollama_json("/api/version")
            if not isinstance(payload.get("version"), str):
                raise ProvisionError("port 11435 is occupied by another process")
            return self
        self.process = subprocess.Popen(
            [str(self.executable), "serve"],
            close_fds=True,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                _ollama_json("/api/version")
            except ProvisionError:
                time.sleep(0.05)
            else:
                return self
        raise ProvisionError("project Ollama failed to start")

    def __exit__(self, *_args: object) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def has_model(self, name: str) -> bool:
        payload = _ollama_json("/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProvisionError("project Ollama model list is invalid")
        return any(
            isinstance(model, dict) and model.get("name") in {name, f"{name}:latest"}
            for model in models
        )

    def create(self, name: str, modelfile: Path) -> None:
        completed = subprocess.run(
            [str(self.executable), "create", name, "-f", str(modelfile)],
            close_fds=True,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ProvisionError("primary translation model creation failed")


def _ollama_executable(installed: Path) -> Path:
    injected = _test_setting("LVT_TEST_OLLAMA_EXECUTABLE")
    executable = Path(injected) if injected else installed
    if executable.is_symlink() or not executable.is_file():
        raise ProvisionError("Ollama executable is unavailable")
    if executable.stat().st_mode & 0o111 == 0:
        raise ProvisionError("Ollama executable is not executable")
    return executable


def _ensure_hy_model(
    source_root: Path,
    data_root: Path,
    executable: Path,
    gguf: Path,
) -> None:
    modelfile = source_root / "packaging/ollama/Modelfile.hy-mt2-1.8b-q4km"
    if modelfile.is_symlink() or not modelfile.is_file():
        raise ProvisionError("Hy-MT2 Modelfile is unavailable")
    with _OllamaSession(source_root, data_root, executable) as ollama:
        if ollama.has_model(HY_MODEL):
            return
        build_root = _safe_join(
            data_root,
            f"models/.hy-create.{uuid.uuid4().hex}",
        )
        build_root.mkdir(mode=0o700)
        try:
            build_modelfile = build_root / "Modelfile"
            content = modelfile.read_text(encoding="utf-8")
            _publish_bytes(build_modelfile, content.encode("utf-8"))
            build_gguf = build_root / "Hy-MT2-1.8B-Q4_K_M.gguf"
            try:
                os.link(gguf, build_gguf)
            except OSError:
                shutil.copyfile(gguf, build_gguf)
            ollama.create(HY_MODEL, build_modelfile)
            if not ollama.has_model(HY_MODEL):
                raise ProvisionError("primary translation model is unavailable")
        finally:
            shutil.rmtree(build_root, ignore_errors=True)


def _write_install_state(
    data_root: Path,
    ffmpeg: dict[str, Any],
    ollama_models: dict[str, Any] | None,
) -> None:
    state_path = _safe_join(data_root, "runtime/install-state.json")
    if state_path.is_symlink() or not state_path.is_file():
        raise ProvisionError("install state is unavailable")
    previous = state_path.read_bytes()
    try:
        state = json.loads(previous)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProvisionError("install state is invalid") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or not isinstance(state.get("core"), dict)
    ):
        raise ProvisionError("core install state is invalid")
    state["ffmpeg"] = ffmpeg
    if ollama_models is not None:
        state["ollama_models"] = ollama_models
    encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode()
    partial = state_path.parent / f".install-state.partial.{uuid.uuid4().hex}"
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
        partial.replace(state_path)
        published = True
        _fsync_directory(state_path.parent)
    except Exception:
        if published:
            restore = state_path.parent / f".install-state.restore.{uuid.uuid4().hex}"
            _publish_bytes(restore, previous)
            restore.replace(state_path)
            _fsync_directory(state_path.parent)
        raise
    finally:
        partial.unlink(missing_ok=True)


def _validate_dependencies(release_root: Path, data_root: Path) -> None:
    python = _safe_join(release_root, ".venv/bin/python")
    validator = _safe_join(release_root, "packaging/tools/verify_install.py")
    completed = subprocess.run(
        [
            str(python),
            str(validator),
            "--phase",
            "dependencies",
            "--data-root",
            str(data_root),
            "--release-root",
            str(release_root),
            "--json",
        ],
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProvisionError("dependencies validator did not return JSON") from exc
    if (
        completed.returncode != 0
        or not isinstance(report, dict)
        or report.get("exit_code") != 0
        or report.get("status") != "healthy"
    ):
        raise ProvisionError("dependencies validation failed")


def provision_dependencies(
    source_root: Path,
    data_root: Path,
    release_root: Path,
    *,
    skip_models: bool = False,
) -> bool:
    source_root = source_root.resolve(strict=True)
    _prepare_roots(data_root, release_root)
    data_root = data_root.resolve(strict=True)
    release_root = release_root.resolve(strict=True)
    dependencies = _load_dependencies(release_root)
    lock = LifecycleLock(data_root / "app", operation="install")
    lock.acquire_bootstrap_then_flock()
    try:
        ffmpeg_artifact = _artifact(dependencies, "ffmpeg")
        existing_ffmpeg = _installed_ffmpeg(data_root, ffmpeg_artifact)
        if existing_ffmpeg is None:
            ffmpeg_dir, ffmpeg_digests = _install_archive_tool(
                source_root,
                data_root,
                ffmpeg_artifact,
                tool_name="ffmpeg",
            )
        else:
            ffmpeg_dir, ffmpeg_digests = existing_ffmpeg
        ffmpeg_state = {
            "version": ffmpeg_artifact["version"],
            "directory": ffmpeg_dir.relative_to(data_root / "app").as_posix(),
            "sha256": ffmpeg_digests,
        }
        if skip_models:
            _write_install_state(data_root, ffmpeg_state, None)
            return False

        _verify_python_packages(release_root)
        ollama_artifact = _artifact(dependencies, "ollama")
        ollama_dir, _ = _install_archive_tool(
            source_root,
            data_root,
            ollama_artifact,
            tool_name="ollama",
        )
        for identifier in MODEL_ARTIFACT_IDS:
            artifact = _artifact(dependencies, identifier)
            if identifier == "diarization-segmentation":
                _install_segmentation(source_root, data_root, artifact)
            else:
                _install_direct_model(source_root, data_root, artifact)
        qwen_state = _install_qwen(source_root, data_root, _qwen_model(dependencies))
        hy_artifact = _artifact(dependencies, "hy-mt2")
        hy_path = _safe_join(data_root, hy_artifact["expected_files"][0])
        _ensure_hy_model(
            source_root,
            data_root,
            _ollama_executable(ollama_dir / "ollama"),
            hy_path,
        )
        _write_install_state(
            data_root,
            ffmpeg_state,
            {QWEN_ID: qwen_state},
        )
        _validate_dependencies(release_root, data_root)
        return True
    finally:
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="供应 Local Video Transcriber 本地引擎和模型")
    parser.add_argument("--phase", required=True, choices=("dependencies",))
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--skip-models", action="store_true")
    arguments = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parents[2]
    try:
        complete = provision_dependencies(
            source_root,
            arguments.data_root,
            arguments.release_root,
            skip_models=arguments.skip_models,
        )
    except Exception:
        print("[ERROR] INSTALL_DEPENDENCIES_FAILED：依赖供应未完成", file=sys.stderr)
        return 2
    if not complete:
        print("[WARN] INSTALL_DEPENDENCIES_INCOMPLETE：模型供应已跳过")
        return 1
    print("[INFO] INSTALL_DEPENDENCIES_READY：依赖候选版本已验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
