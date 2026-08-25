#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

PHASES = (
    "staging-core",
    "dependencies",
    "installed-prerequisites",
    "runtime-full",
)
MODEL_ARTIFACT_IDS = {
    "asr-whisper-small-mlx-config",
    "asr-whisper-small-mlx-weights",
    "diarization-segmentation",
    "diarization-embedding",
    "hy-mt2",
}


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    MISSING = "missing"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class Check:
    identifier: str
    status: CheckStatus
    code: str
    message: str
    suggestion: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.identifier,
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def ok(identifier: str, code: str, message: str) -> Check:
    return Check(identifier, CheckStatus.OK, code, message)


def missing(identifier: str, code: str, message: str, suggestion: str) -> Check:
    return Check(identifier, CheckStatus.MISSING, code, message, suggestion)


def unsafe(identifier: str, code: str, message: str, suggestion: str) -> Check:
    return Check(identifier, CheckStatus.UNSAFE, code, message, suggestion)


def warning(identifier: str, code: str, message: str, suggestion: str) -> Check:
    return Check(identifier, CheckStatus.WARNING, code, message, suggestion)


def build_report(phase: str, checks: list[Check]) -> dict[str, Any]:
    exit_code = report_exit_code(checks)
    status = "healthy" if exit_code == 0 else "warning" if exit_code == 1 else "failed"
    return {
        "schema_version": 1,
        "phase": phase,
        "status": status,
        "exit_code": exit_code,
        "checks": [check.as_dict() for check in checks],
    }


def report_exit_code(checks: list[Check]) -> int:
    if any(check.status is CheckStatus.UNSAFE for check in checks):
        return 2
    if any(check.status in {CheckStatus.WARNING, CheckStatus.MISSING} for check in checks):
        return 1
    return 0


class LocalProbes:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self._test_root = _test_root_for(data_root)

    def ollama_port_state(self) -> str:
        if self._test_root is not None:
            forbidden = os.environ.get("LVT_TEST_FORBID_PORT")
            if forbidden == "11435":
                raise RuntimeError("forbidden test port was accessed")
            return os.environ.get("LVT_TEST_OLLAMA_PORT", "free")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            occupied = probe.connect_ex(("127.0.0.1", 11435)) == 0
        if not occupied:
            return "free"
        return "owned" if _owned_ollama_metadata_valid(self.data_root) else "occupied"

    def backend_health(self) -> bool:
        if self._test_root is not None:
            value = os.environ.get("LVT_TEST_BACKEND_HEALTH", "down")
            if value == "forbidden":
                raise RuntimeError("backend health probe crossed a phase boundary")
            return value == "healthy"
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=0.5)
        try:
            connection.request("GET", "/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(65_537)
            if response.status != 200 or len(body) > 65_536:
                return False
            payload = json.loads(body)
            return isinstance(payload, dict) and payload.get("status") == "healthy"
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        finally:
            connection.close()


def validate_install(
    phase: str,
    *,
    data_root: Path,
    release_root: Path | None = None,
    probes: LocalProbes | None = None,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError("unsupported validation phase")
    active_probes = probes or LocalProbes(data_root)
    if phase == "staging-core":
        checks = _validate_staging_core(release_root, data_root)
    elif phase == "dependencies":
        checks = _validate_dependencies(data_root, release_root)
    elif phase == "installed-prerequisites":
        checks = _validate_installed_prerequisites(
            data_root,
            release_root,
            active_probes,
        )
    else:
        checks = _validate_runtime_full(data_root, release_root, active_probes)
    return build_report(phase, checks)


def _validate_staging_core(
    release_root: Path | None,
    data_root: Path | None = None,
) -> list[Check]:
    root_check = _validate_root(release_root, "release")
    if root_check.status is not CheckStatus.OK or release_root is None:
        return [root_check]
    checks = [root_check]
    required_files = {
        "version": "VERSION",
        "backend_metadata": "backend/pyproject.toml",
        "backend_package": "backend/src/lvt",
        "dependencies_manifest": "packaging/dependencies.json",
        "doctor_command": "scripts/doctor.command",
    }
    for identifier, relative in required_files.items():
        checks.append(_required_path(release_root, relative, identifier))
    checks.append(_validate_release_python(release_root, data_root))
    checks.extend(_validate_release_version(release_root))
    checks.extend(_validate_dependency_metadata(release_root))
    return checks


def _validate_dependencies(data_root: Path, release_root: Path | None) -> list[Check]:
    checks = _validate_staging_core(release_root, data_root)
    if report_exit_code(checks) == 2 or release_root is None:
        return checks
    data_check = _validate_root(data_root, "data_root")
    checks.append(data_check)
    if data_check.status is not CheckStatus.OK:
        return checks
    dependencies = _load_dependencies(release_root)
    if dependencies is None:
        return checks
    for item in dependencies.get("artifacts", []):
        if not isinstance(item, dict) or item.get("id") not in MODEL_ARTIFACT_IDS:
            continue
        for relative in item.get("expected_files", []):
            checks.append(
                _required_model_path(
                    data_root,
                    relative,
                    f"model_{item['id']}",
                    item.get("expected_file_size", item.get("size")),
                    item.get("expected_file_sha256"),
                )
            )
    for model in dependencies.get("ollama_models", []):
        if not isinstance(model, dict):
            continue
        for relative in model.get("expected_files", []):
            checks.append(
                _required_sized_path(
                    data_root,
                    relative,
                    f"model_{model.get('id', 'ollama')}",
                    model.get("manifest_size"),
                )
            )
        if model.get("id") == "qwen2.5-1.5b":
            checks.extend(_validate_qwen_blobs(data_root, model))
    checks.extend(_validate_ffmpeg_install(data_root))
    return checks


def _validate_installed_prerequisites(
    data_root: Path,
    release_root: Path | None,
    probes: LocalProbes,
) -> list[Check]:
    data_check = _validate_root(data_root, "data_root")
    if data_check.status is not CheckStatus.OK:
        return [data_check]
    current_root, current_checks = _resolve_current_release(data_root, release_root)
    checks = [data_check, *current_checks]
    if current_root is None:
        return checks
    checks.extend(_validate_dependencies(data_root, current_root))
    checks.append(_validate_stable_extension(data_root, current_root))
    checks.append(_validate_token_metadata(data_root))
    for relative in ("db", "exports", "logs", "work", "runtime"):
        checks.append(_writable_directory(data_root, relative))
    port_state = probes.ollama_port_state()
    if port_state == "occupied":
        checks.append(
            unsafe(
                "ollama_port",
                "OLLAMA_PORT_UNOWNED",
                "项目端口已被非本应用进程占用",
                "释放项目专用端口 11435 后重试",
            )
        )
    else:
        checks.append(ok("ollama_port", "OLLAMA_PORT_READY", "项目端口所有权可用"))
    return checks


def _validate_runtime_full(
    data_root: Path,
    release_root: Path | None,
    probes: LocalProbes,
) -> list[Check]:
    checks = _validate_installed_prerequisites(data_root, release_root, probes)
    if report_exit_code(checks) == 2:
        return checks
    if probes.ollama_port_state() != "owned":
        checks.append(
            missing(
                "ollama_runtime",
                "OLLAMA_NOT_RUNNING",
                "项目 Ollama 尚未运行",
                "启动项目服务后重新检查",
            )
        )
    else:
        checks.append(ok("ollama_runtime", "OLLAMA_HEALTHY", "项目 Ollama 正常"))
    if probes.backend_health():
        checks.append(ok("backend_health", "BACKEND_HEALTHY", "本地后端健康"))
    else:
        checks.append(
            missing(
                "backend_health",
                "BACKEND_NOT_RUNNING",
                "本地后端尚未运行",
                "运行启动命令后重新检查",
            )
        )
    checks.append(_database_quick_check(data_root))
    return checks


def _validate_root(path: Path | None, identifier: str) -> Check:
    if path is None:
        return missing(
            identifier,
            f"{identifier.upper()}_MISSING",
            "所需目录不存在",
            "完成对应安装阶段后重试",
        )
    if not path.is_absolute() or _has_symlink_component(path):
        return unsafe(
            identifier,
            "ROOT_PATH_UNSAFE",
            "目录路径不安全",
            "使用真实且非符号链接的应用目录",
        )
    if not path.exists():
        return missing(
            identifier,
            f"{identifier.upper()}_MISSING",
            "所需目录不存在",
            "完成对应安装阶段后重试",
        )
    if not path.is_dir():
        return unsafe(
            identifier,
            "ROOT_PATH_UNSAFE",
            "目录路径不安全",
            "使用真实且非符号链接的应用目录",
        )
    try:
        path.resolve(strict=True)
    except OSError:
        return unsafe(identifier, "ROOT_PATH_UNSAFE", "目录路径不安全", "检查目录权限")
    return ok(identifier, f"{identifier.upper()}_READY", "目录可用")


def _required_path(root: Path, relative: str, identifier: str) -> Check:
    try:
        path = _safe_join(root, relative)
        if path.is_symlink():
            raise ValueError
        if not path.exists():
            raise FileNotFoundError(relative)
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except FileNotFoundError:
        return missing(
            identifier,
            "REQUIRED_FILE_MISSING",
            "必需文件缺失",
            "重新运行对应安装阶段",
        )
    except (OSError, ValueError, TypeError):
        return unsafe(
            identifier,
            "REQUIRED_PATH_UNSAFE",
            "必需文件路径不安全",
            "移除符号链接或越界路径后重试",
        )
    return ok(identifier, "REQUIRED_FILE_READY", "必需文件存在")


def _required_sized_path(
    root: Path,
    relative: str,
    identifier: str,
    expected_size: Any,
) -> Check:
    check = _required_path(root, relative, identifier)
    if check.status is not CheckStatus.OK:
        return check
    try:
        if (
            type(expected_size) is not int
            or expected_size <= 0
            or _safe_join(root, relative).stat().st_size != expected_size
        ):
            raise ValueError
    except (OSError, ValueError):
        return unsafe(
            identifier,
            "MODEL_SIZE_INVALID",
            "模型文件大小与依赖合同不一致",
            "隔离文件并重新运行依赖安装",
        )
    return ok(identifier, "MODEL_FILE_READY", "模型文件存在且大小匹配")


def _required_model_path(
    root: Path,
    relative: str,
    identifier: str,
    expected_size: Any,
    expected_sha256: Any,
) -> Check:
    check = _required_sized_path(root, relative, identifier, expected_size)
    if check.status is not CheckStatus.OK or expected_sha256 is None:
        return check
    try:
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or _sha256(_safe_join(root, relative)) != expected_sha256
        ):
            raise ValueError
    except (OSError, ValueError):
        return unsafe(
            identifier,
            "MODEL_DIGEST_INVALID",
            "模型文件摘要与依赖合同不一致",
            "隔离文件并重新运行依赖安装",
        )
    return ok(identifier, "MODEL_FILE_READY", "模型文件大小和摘要匹配")


def _validate_qwen_blobs(data_root: Path, model: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    try:
        blobs = model["blobs"]
        if not isinstance(blobs, list) or len(blobs) != 5:
            raise ValueError
        for index, blob in enumerate(blobs):
            if not isinstance(blob, dict):
                raise ValueError
            digest = blob["digest"]
            if (
                not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or len(digest) != 71
                or any(character not in "0123456789abcdef" for character in digest[7:])
            ):
                raise ValueError
            relative = f"models/ollama/blobs/sha256-{digest[7:]}"
            checks.append(
                _required_model_path(
                    data_root,
                    relative,
                    f"qwen_blob_{index}",
                    blob.get("size"),
                    digest[7:],
                )
            )
        install_state = json.loads(
            _safe_join(data_root, "runtime/install-state.json").read_text(encoding="utf-8")
        )
        recorded = install_state["ollama_models"]["qwen2.5-1.5b"]
        expected = {
            "verified": True,
            "manifest_sha256": model["manifest_sha256"],
            "manifest_size": model["manifest_size"],
            "manifest_media_type": model["manifest_media_type"],
            "blobs": blobs,
        }
        if recorded != expected:
            raise ValueError
    except FileNotFoundError:
        checks.append(
            missing(
                "qwen_integrity",
                "QWEN_BLOBS_MISSING",
                "备用翻译模型 blob 缺失",
                "重新运行依赖安装阶段",
            )
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        checks.append(
            unsafe(
                "qwen_integrity",
                "QWEN_INTEGRITY_FAILED",
                "备用翻译模型元数据与依赖合同不一致",
                "隔离模型并重新运行依赖安装",
            )
        )
    else:
        checks.append(ok("qwen_integrity", "QWEN_VERIFIED", "备用翻译模型元数据已验证"))
    return checks


def _safe_join(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or PureWindowsPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("unsafe relative path")
    candidate = root.joinpath(*relative.split("/"))
    candidate.parent.resolve(strict=False).relative_to(root.resolve(strict=True))
    return candidate


def _validate_release_python(release_root: Path, data_root: Path | None) -> Check:
    try:
        python_path = _safe_join(release_root, ".venv/bin/python")
        metadata = python_path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
            raise ValueError
        resolved = python_path.resolve(strict=True)
        allowed_roots = [release_root.resolve(strict=True)]
        if data_root is not None:
            allowed_roots.append((data_root / "app" / "tools" / "python").resolve(strict=False))
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            raise ValueError
    except FileNotFoundError:
        return missing(
            "release_python",
            "PYTHON_MISSING",
            "发布 Python 缺失",
            "重新安装应用自带 Python",
        )
    except (OSError, ValueError):
        return unsafe(
            "release_python",
            "PYTHON_PERMISSION_UNSAFE",
            "发布 Python 路径或权限不安全",
            "重新安装应用自带 Python",
        )
    return ok("release_python", "PYTHON_VERIFIED", "发布 Python 可执行")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_release_version(release_root: Path) -> list[Check]:
    try:
        version = _safe_join(release_root, "VERSION").read_text(encoding="utf-8").strip()
        if not version or not all(part.isdigit() for part in version.split(".")):
            raise ValueError
    except (OSError, ValueError):
        return [
            unsafe(
                "release_version",
                "RELEASE_VERSION_INVALID",
                "发布版本信息无效",
                "重新获取完整发布包",
            )
        ]
    return [ok("release_version", "RELEASE_VERSION_VALID", f"应用版本 {version}")]


def _load_dependencies(release_root: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            _safe_join(release_root, "packaging/dependencies.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_dependency_metadata(release_root: Path) -> list[Check]:
    payload = _load_dependencies(release_root)
    if payload is None:
        return [
            unsafe(
                "dependencies_manifest",
                "DEPENDENCIES_MANIFEST_INVALID",
                "依赖合同无法读取",
                "重新获取完整发布包",
            )
        ]
    try:
        for item in [*payload["artifacts"], *payload["ollama_models"]]:
            expected = item["expected_files"]
            if not isinstance(expected, list) or not expected:
                raise ValueError
            for relative in expected:
                _safe_join(release_root, relative)
    except (KeyError, TypeError, ValueError):
        return [
            unsafe(
                "dependencies_manifest",
                "DEPENDENCIES_MANIFEST_INVALID",
                "依赖合同包含不安全路径",
                "重新获取完整发布包",
            )
        ]
    return [ok("dependencies_manifest", "DEPENDENCIES_MANIFEST_VALID", "依赖合同有效")]


def _validate_ffmpeg_install(data_root: Path) -> list[Check]:
    try:
        state_path = _safe_join(data_root, "runtime/install-state.json")
        if state_path.is_symlink():
            raise ValueError
        state = json.loads(state_path.read_text(encoding="utf-8"))
        metadata = state["ffmpeg"]
        directory = metadata["directory"]
        ffmpeg_dir = _safe_join(_safe_join(data_root, "app"), directory)
        digests = metadata["sha256"]
        version = metadata["version"]
        if Path(directory).parts[-2:] != (version, "bin"):
            raise ValueError
        for name in ("ffmpeg", "ffprobe"):
            path = _safe_join(ffmpeg_dir, name)
            file_stat = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
                raise ValueError
            if file_stat.st_mode & 0o111 == 0:
                raise ValueError
            if _sha256(path) != digests[name]:
                raise ValueError
    except FileNotFoundError:
        return [
            missing(
                "ffmpeg",
                "FFMPEG_MISSING",
                "应用自带 FFmpeg 缺失",
                "重新运行依赖安装阶段",
            )
        ]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [
            unsafe(
                "ffmpeg",
                "FFMPEG_INTEGRITY_FAILED",
                "应用自带 FFmpeg 完整性校验失败",
                "隔离损坏文件并重新安装依赖",
            )
        ]
    return [ok("ffmpeg", "FFMPEG_VERIFIED", "应用自带 FFmpeg 已验证")]


def _resolve_current_release(
    data_root: Path,
    supplied_release: Path | None,
) -> tuple[Path | None, list[Check]]:
    current = data_root / "app" / "current"
    try:
        if not current.is_symlink():
            raise FileNotFoundError
        resolved = current.resolve(strict=True)
        resolved.relative_to((data_root / "app" / "releases").resolve(strict=True))
        if supplied_release is not None and supplied_release.resolve(strict=True) != resolved:
            raise ValueError
    except FileNotFoundError:
        return None, [
            missing(
                "current_release",
                "CURRENT_RELEASE_MISSING",
                "当前发布版本不存在",
                "完成安装发布后重试",
            )
        ]
    except (OSError, ValueError):
        return None, [
            unsafe(
                "current_release",
                "CURRENT_RELEASE_UNSAFE",
                "当前发布链接不安全",
                "修复应用发布链接后重试",
            )
        ]
    return resolved, [ok("current_release", "CURRENT_RELEASE_VALID", "当前发布版本有效")]


def _validate_token_metadata(data_root: Path) -> Check:
    token = _safe_join(data_root, "config/api-token")
    try:
        metadata = token.lstat()
        if token.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o777 != 0o600
            or not 32 <= metadata.st_size <= 4096
        ):
            raise ValueError
    except FileNotFoundError:
        return missing(
            "token_metadata",
            "TOKEN_MISSING",
            "连接凭据文件缺失",
            "重新运行核心安装阶段",
        )
    except (OSError, ValueError):
        return unsafe(
            "token_metadata",
            "TOKEN_METADATA_UNSAFE",
            "连接凭据文件权限不安全",
            "修复文件所有权和 0600 权限",
        )
    return ok("token_metadata", "TOKEN_METADATA_VALID", "连接凭据元数据有效")


def _validate_stable_extension(data_root: Path, release_root: Path) -> Check:
    manifest = _required_path(data_root, "extension/manifest.json", "stable_extension")
    if manifest.status is not CheckStatus.OK:
        return manifest
    try:
        extension = json.loads(
            _safe_join(data_root, "extension/manifest.json").read_text(encoding="utf-8")
        )
        release_version = _safe_join(release_root, "VERSION").read_text(encoding="utf-8").strip()
        if not isinstance(extension, dict) or extension.get("version") != release_version:
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        return unsafe(
            "stable_extension",
            "EXTENSION_VERSION_INVALID",
            "稳定扩展版本与当前发布不一致",
            "重新发布稳定扩展",
        )
    return ok("stable_extension", "EXTENSION_VERSION_VALID", "稳定扩展版本有效")


def _writable_directory(data_root: Path, relative: str) -> Check:
    try:
        path = _safe_join(data_root, relative)
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError
        path.resolve(strict=True).relative_to(data_root.resolve(strict=True))
        if metadata.st_mode & 0o200 == 0:
            raise PermissionError
    except FileNotFoundError:
        return missing(
            f"directory_{relative}",
            "DIRECTORY_MISSING",
            "运行目录缺失",
            "重新运行核心安装阶段",
        )
    except PermissionError:
        return unsafe(
            f"directory_{relative}",
            "DIRECTORY_PERMISSION_UNSAFE",
            "运行目录不可写",
            "修复应用数据目录权限",
        )
    except (OSError, ValueError):
        return unsafe(
            f"directory_{relative}",
            "DIRECTORY_PATH_UNSAFE",
            "运行目录路径不安全",
            "移除符号链接或越界路径",
        )
    return ok(f"directory_{relative}", "DIRECTORY_READY", "运行目录可写")


def _database_quick_check(data_root: Path) -> Check:
    database = _safe_join(data_root, "db/lvt.sqlite3")
    if not database.is_file() or database.is_symlink():
        return missing(
            "database",
            "DATABASE_MISSING",
            "任务数据库不存在",
            "完成首次启动后重试",
        )
    try:
        uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise ValueError
    except (sqlite3.Error, ValueError):
        return unsafe(
            "database",
            "DATABASE_INTEGRITY_FAILED",
            "任务数据库完整性检查失败",
            "停止服务并从备份恢复",
        )
    return ok("database", "DATABASE_HEALTHY", "任务数据库可读")


def _owned_ollama_metadata_valid(data_root: Path) -> bool:
    metadata_path = data_root / "runtime" / "ollama.pid"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        valid = (
            isinstance(payload, dict)
            and payload.get("port") == 11435
            and type(payload.get("pid")) is int
            and isinstance(payload.get("start_time"), str)
            and bool(payload["start_time"])
            and isinstance(payload.get("nonce"), str)
            and bool(payload["nonce"])
            and isinstance(payload.get("executable"), str)
            and bool(payload["executable"])
        )
        if not valid:
            return False
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(payload["pid"])],
            capture_output=True,
            text=True,
            check=False,
        )
        current_start = " ".join(completed.stdout.split())
        executable = Path(payload["executable"])
        expected_executable = shutil.which("ollama")
        return (
            completed.returncode == 0
            and current_start == payload["start_time"]
            and expected_executable is not None
            and executable.is_absolute()
            and executable.is_file()
            and not executable.is_symlink()
            and executable.resolve(strict=True) == Path(expected_executable).resolve(strict=True)
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _test_root_for(path: Path) -> Path | None:
    raw = os.environ.get("LVT_TEST_ROOT")
    if raw is None:
        return None
    test_root = Path(raw)
    try:
        path.resolve(strict=False).relative_to(test_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("test root does not contain application data") from exc
    return test_root


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _render_human(report: dict[str, Any]) -> str:
    labels = {"healthy": "健康", "warning": "需要处理", "failed": "不安全"}
    lines = [f"状态：{labels[report['status']]}"]
    for check in report["checks"]:
        marker = "通过" if check["status"] == "ok" else "检查"
        line = f"[{marker}] {check['code']}：{check['message']}"
        if check["suggestion"]:
            line += f"；建议：{check['suggestion']}"
        lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 Local Video Transcriber 安装状态")
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    data_root = arguments.data_root or (
        Path.home() / "Library" / "Application Support" / "LocalVideoTranscriber"
    )
    try:
        report = validate_install(
            arguments.phase,
            data_root=data_root,
            release_root=arguments.release_root,
        )
    except Exception:
        report = build_report(
            arguments.phase,
            [
                unsafe(
                    "internal",
                    "VALIDATION_INTERNAL_ERROR",
                    "安装检查无法完成",
                    "确认安装目录完整后重试",
                )
            ],
        )
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_human(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
