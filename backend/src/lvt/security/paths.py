from __future__ import annotations

import re
import unicodedata
from pathlib import Path

INVALID_FILENAME_CHARS = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")


def safe_filename(value: str, *, fallback: str = "untitled", max_length: int = 120) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = INVALID_FILENAME_CHARS.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if normalized in {"", ".", ".."}:
        normalized = fallback
    return normalized[:max_length].rstrip(" .") or fallback


def ensure_within_root(path: Path, root: Path) -> Path:
    resolved_root = root.expanduser().resolve()
    resolved_path = path.expanduser().resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise ValueError("path escapes application root")
    return resolved_path
