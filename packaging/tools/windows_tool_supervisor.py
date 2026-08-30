#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid

from windows_job import NativeWindowsJobApi, launch_suspended_in_job

INFINITE = 0xFFFFFFFF
SAFE_KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
SAFE_NONCE = re.compile(r"[0-9a-f]{32}")


def _canonical_uuid(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("ownership identifier is not canonical")
    return value


def supervise(command: list[str], *, nonce: str) -> int:
    api = NativeWindowsJobApi()
    launched = launch_suspended_in_job(
        command,
        dict(os.environ),
        os.getcwd(),
        f"LocalVideoTranscriber-tool-{nonce}",
        api,
    )
    try:
        if not api.wait_process(launched.process_handle, INFINITE):
            raise RuntimeError("tool wait timed out unexpectedly")
        return api.process_exit_code(launched.process_handle)
    finally:
        launched.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise one Windows worker tool")
    parser.add_argument("--job-id", required=True, type=_canonical_uuid)
    parser.add_argument("--run-id", required=True, type=_canonical_uuid)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--ownership-nonce", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if (
        SAFE_KIND.fullmatch(arguments.kind) is None
        or SAFE_NONCE.fullmatch(arguments.ownership_nonce) is None
        or not command
    ):
        return 64
    try:
        return supervise(command, nonce=arguments.ownership_nonce)
    except BaseException:
        return 70


if __name__ == "__main__":
    sys.exit(main())
