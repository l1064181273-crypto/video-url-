#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from verify_install import (
    PHASES,
    Check,
    CheckStatus,
    LocalProbes,
    build_report,
    missing,
    ok,
    unsafe,
    validate_install,
    warning,
)

GIB = 1024**3
MINIMUM_DISK_BYTES = 8 * GIB
MINIMUM_MEMORY_BYTES = 8 * GIB
RECOMMENDED_MEMORY_BYTES = 16 * GIB


class SystemProbes:
    def __init__(self, test_root: Path | None) -> None:
        self.test_root = test_root

    def operating_system(self) -> str:
        return self._override("LVT_TEST_PLATFORM") or platform.system().lower()

    def macos_version(self) -> str:
        override = self._override("LVT_TEST_MACOS_VERSION")
        if override is not None:
            return override
        return platform.mac_ver()[0]

    def architecture(self) -> str:
        return self._override("LVT_TEST_ARCH") or platform.machine()

    def translated(self) -> bool:
        override = self._override("LVT_TEST_ROSETTA")
        if override is not None:
            return override == "1"
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and completed.stdout.strip() == "1"

    def memory_bytes(self) -> int:
        override = self._override("LVT_TEST_MEMORY_BYTES")
        if override is not None:
            return int(override)
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(completed.stdout.strip())

    def disk_bytes(self, path: Path) -> int:
        override = self._override("LVT_TEST_DISK_BYTES")
        if override is not None:
            return int(override)
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        return shutil.disk_usage(existing).free

    def which(self, command: str) -> str | None:
        return shutil.which(command)

    def _override(self, name: str) -> str | None:
        if self.test_root is None:
            return None
        return os.environ.get(name)


def run_doctor(
    phase: str,
    *,
    data_root: Path,
    release_root: Path | None,
) -> dict[str, Any]:
    test_root = _validated_test_root(data_root, release_root)
    system = SystemProbes(test_root)
    checks = _system_checks(system, data_root, phase)
    install_report = validate_install(
        phase,
        data_root=data_root,
        release_root=release_root,
        probes=LocalProbes(data_root),
    )
    checks.extend(_checks_from_report(install_report))
    return build_report(phase, checks)


def _system_checks(probes: SystemProbes, data_root: Path, phase: str) -> list[Check]:
    checks: list[Check] = []
    operating_system = probes.operating_system()
    if operating_system not in {"darwin", "macos"}:
        checks.append(
            unsafe(
                "operating_system",
                "MACOS_REQUIRED",
                "当前系统不受支持",
                "请使用 macOS 13 或更高版本",
            )
        )
    else:
        checks.append(ok("operating_system", "MACOS_DETECTED", "已检测到 macOS"))

    version = probes.macos_version()
    if _version_major(version) < 13:
        checks.append(
            unsafe(
                "macos_version",
                "MACOS_UNSUPPORTED",
                "macOS 版本低于最低要求",
                "请升级到 macOS 13 或更高版本",
            )
        )
    else:
        checks.append(ok("macos_version", "MACOS_SUPPORTED", f"macOS {version} 受支持"))

    architecture = probes.architecture()
    if architecture != "arm64":
        checks.append(
            unsafe(
                "architecture",
                "ARCH_UNSUPPORTED",
                "处理器架构不受支持",
                "请在 Apple Silicon Mac 上运行",
            )
        )
    else:
        checks.append(ok("architecture", "ARCH_SUPPORTED", "Apple Silicon 架构受支持"))

    if probes.translated():
        checks.append(
            unsafe(
                "rosetta",
                "ROSETTA_UNSUPPORTED",
                "当前进程运行于 Rosetta",
                "请使用 arm64 原生终端重新运行",
            )
        )
    else:
        checks.append(ok("rosetta", "ROSETTA_NOT_ACTIVE", "当前进程为原生 arm64"))

    memory = probes.memory_bytes()
    if memory < MINIMUM_MEMORY_BYTES:
        checks.append(
            missing(
                "memory",
                "MEMORY_INSUFFICIENT",
                "可用内存低于最低要求",
                "请使用至少 8 GB 内存的设备",
            )
        )
    elif memory < RECOMMENDED_MEMORY_BYTES:
        checks.append(
            warning(
                "memory",
                "MEMORY_BELOW_RECOMMENDED",
                "内存满足最低要求但低于建议值",
                "建议使用 16 GB 或更多内存",
            )
        )
    else:
        checks.append(ok("memory", "MEMORY_SUPPORTED", "内存满足建议要求"))

    if probes.disk_bytes(data_root) < MINIMUM_DISK_BYTES:
        checks.append(
            missing(
                "disk",
                "DISK_INSUFFICIENT",
                "可用磁盘空间不足",
                "请至少释放 8 GB 磁盘空间",
            )
        )
    else:
        checks.append(ok("disk", "DISK_SUPPORTED", "磁盘空间满足最低要求"))

    commands = {"ollama": ("ollama_command", "OLLAMA_MISSING", "未找到 Ollama")}
    if phase == "staging-core":
        commands = {
            "python3": ("python", "PYTHON_MISSING", "未找到 Python 3"),
            "ffmpeg": ("ffmpeg_command", "FFMPEG_MISSING", "未找到 FFmpeg"),
            "ffprobe": ("ffprobe_command", "FFMPEG_MISSING", "未找到 ffprobe"),
            **commands,
        }
    for command, (identifier, code, message) in commands.items():
        if probes.which(command) is None:
            checks.append(
                missing(
                    identifier,
                    code,
                    message,
                    "完成对应依赖安装后重新检查",
                )
            )
        else:
            checks.append(ok(identifier, f"{identifier.upper()}_READY", f"{command} 可用"))
    return checks


def _version_major(value: str) -> int:
    try:
        return int(value.split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


def _checks_from_report(report: dict[str, Any]) -> list[Check]:
    return [
        Check(
            identifier=payload["id"],
            status=CheckStatus(payload["status"]),
            code=payload["code"],
            message=payload["message"],
            suggestion=payload["suggestion"],
        )
        for payload in report["checks"]
    ]


def _validated_test_root(data_root: Path, release_root: Path | None) -> Path | None:
    raw = os.environ.get("LVT_TEST_ROOT")
    if raw is None:
        return None
    test_root = Path(raw).resolve(strict=True)
    for path in (data_root, release_root):
        if path is None:
            continue
        path.resolve(strict=False).relative_to(test_root)
    return test_root


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
    parser = argparse.ArgumentParser(description="检查 Local Video Transcriber 运行环境")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--phase", choices=PHASES, default="runtime-full")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--release-root", type=Path)
    arguments = parser.parse_args(argv)
    data_root = arguments.data_root or (
        Path.home() / "Library" / "Application Support" / "LocalVideoTranscriber"
    )
    try:
        report = run_doctor(
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
                    "DOCTOR_INTERNAL_ERROR",
                    "环境检查无法完成",
                    "确认应用目录完整后重试",
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
