#!/usr/bin/env python3
from __future__ import annotations

import http.server
import json
import os
import sys
from pathlib import Path


def _state_path() -> Path:
    return Path(os.environ["LVT_TEST_OLLAMA_STATE"])


def _models() -> list[str]:
    path = _state_path()
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _record(model: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models = _models()
    if model not in models:
        models.append(model)
    path.write_text("\n".join(models) + "\n", encoding="utf-8")


def _audit() -> None:
    audit = os.environ.get("LVT_TEST_OLLAMA_AUDIT")
    if audit:
        Path(audit).write_text(
            json.dumps(
                {
                    "argv": sys.argv,
                    "host": os.environ.get("OLLAMA_HOST"),
                    "models": os.environ.get("OLLAMA_MODELS"),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _serve() -> int:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/version":
                payload = {"version": "0.32.15"}
            elif self.path == "/api/tags":
                payload = {"models": [{"name": model} for model in _models()]}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    if os.environ.get("OLLAMA_HOST") != "127.0.0.1:11435":
        return 9
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 11435), Handler)
    server.serve_forever()
    return 0


def main() -> int:
    _audit()
    if sys.argv[1:] == ["serve"]:
        return _serve()
    if len(sys.argv) == 5 and sys.argv[1] == "create" and sys.argv[3] == "-f":
        if os.environ.get("LVT_TEST_OLLAMA_CREATE_FAIL") == "1":
            return 8
        if not Path(sys.argv[4]).is_file():
            return 7
        _record(sys.argv[2])
        return 0
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
