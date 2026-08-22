import stat
from pathlib import Path

import pytest

from lvt.security.paths import ensure_within_root, safe_filename
from lvt.security.token import load_or_create_token
from lvt.security.urls import sanitize_display_url, validate_public_media_url


@pytest.mark.parametrize("url", ["file:///tmp/a.mp4", "javascript:alert(1)", "data:text/plain,x"])
def test_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_media_url(url)


def test_sanitizes_sensitive_query_from_display_url() -> None:
    url = "https://example.test/video?id=secret#fragment"
    assert validate_public_media_url(url) == url
    assert sanitize_display_url(url) == "https://example.test/video"


def test_safe_filename_supports_chinese_and_blocks_traversal() -> None:
    assert safe_filename("../中文 标题?.mp4") == "_中文 标题_.mp4"


def test_ensure_within_root_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ensure_within_root(tmp_path / ".." / "outside", tmp_path)


def test_token_is_high_entropy_persistent_and_private(tmp_path: Path) -> None:
    path = tmp_path / "config" / "api-token"
    first = load_or_create_token(path)
    second = load_or_create_token(path)

    assert first == second
    assert len(first) >= 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
