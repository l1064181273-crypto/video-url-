from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def validate_public_media_url(value: str, *, max_length: int = 4096) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > max_length:
        raise ValueError("URL 为空或过长")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只支持公开的 HTTP/HTTPS URL")
    if not parsed.hostname:
        raise ValueError("URL 缺少有效主机名")
    if parsed.username or parsed.password:
        raise ValueError("URL 不得包含用户名或密码")
    return candidate


def sanitize_display_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
