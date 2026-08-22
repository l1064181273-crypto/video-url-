from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    worker_concurrency: int = 1

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "::1"}:
            raise ValueError("LVT_HOST must be 127.0.0.1 or ::1")
        if type(self.worker_concurrency) is not int or self.worker_concurrency not in {1, 2}:
            raise ValueError("LVT_WORKER_CONCURRENCY must be 1 or 2")

    @classmethod
    def from_env(cls) -> Settings:
        default_root = Path.home() / "Library" / "Application Support" / "LocalVideoTranscriber"
        return cls(
            data_root=Path(os.environ.get("LVT_DATA_ROOT", default_root)).expanduser(),
            host=os.environ.get("LVT_HOST", "127.0.0.1"),
            port=int(os.environ.get("LVT_PORT", "8765")),
            worker_concurrency=int(os.environ.get("LVT_WORKER_CONCURRENCY", "1")),
        )

    def ensure_directories(self) -> None:
        for name in ("config", "db", "models", "work", "exports", "logs"):
            (self.data_root / name).mkdir(parents=True, exist_ok=True)
