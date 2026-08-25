from __future__ import annotations

import http.server
import socket
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class DownloadFixture:
    files: Mapping[str, bytes]
    counts: Counter[str] = field(default_factory=Counter)
    ranges: list[tuple[str, str | None]] = field(default_factory=list)
    _server: http.server.ThreadingHTTPServer | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def __enter__(self) -> DownloadFixture:
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fixture._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def origin(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def _handle(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        path = handler.path
        self.counts[path] += 1
        range_header = handler.headers.get("Range")
        self.ranges.append((path, range_header))
        behavior, _, key = path.lstrip("/").partition("/")
        content = self.files.get(key)
        if content is None:
            handler.send_error(404)
            return
        if behavior == "redirect-http":
            handler.send_response(302)
            handler.send_header("Location", f"{self.origin}/normal/{key}")
            handler.end_headers()
            return
        if behavior == "corrupt":
            content = bytes([content[0] ^ 1]) + content[1:] if content else b"x"
        if behavior == "resume" and self.counts[path] == 1 and range_header is None:
            midpoint = max(1, len(content) // 2)
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(content)))
            handler.end_headers()
            handler.wfile.write(content[:midpoint])
            handler.wfile.flush()
            assert handler.connection is not None
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
            return
        if behavior == "truncate":
            midpoint = max(1, len(content) // 2)
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(content)))
            handler.end_headers()
            handler.wfile.write(content[:midpoint])
            return
        if range_header is not None:
            prefix = "bytes="
            if not range_header.startswith(prefix) or not range_header.endswith("-"):
                handler.send_error(416)
                return
            offset = int(range_header[len(prefix) : -1])
            if offset >= len(content):
                handler.send_error(416)
                return
            body = content[offset:]
            handler.send_response(206)
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header(
                "Content-Range",
                f"bytes {offset}-{len(content) - 1}/{len(content)}",
            )
        else:
            body = content
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
