from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    data_root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    worker_concurrency: int = 1
    model_root: Path | None = None
    installed_mode: bool = False
    ffmpeg_dir: Path | None = None
    install_state: Path | None = None
    ollama_url: str = "http://127.0.0.1:11435"

    def __post_init__(self) -> None:
        if self.model_root is None:
            object.__setattr__(self, "model_root", self.data_root / "models")
        if self.install_state is None:
            object.__setattr__(
                self,
                "install_state",
                self.data_root / "runtime" / "install-state.json",
            )
        if self.host not in {"127.0.0.1", "::1"}:
            raise ValueError("LVT_HOST must be 127.0.0.1 or ::1")
        if type(self.worker_concurrency) is not int or self.worker_concurrency not in {1, 2}:
            raise ValueError("LVT_WORKER_CONCURRENCY must be 1 or 2")
        if type(self.installed_mode) is not bool:
            raise ValueError("LVT_INSTALLED_MODE must be 0 or 1")
        if self.installed_mode and self.ffmpeg_dir is None:
            raise ValueError("LVT_FFMPEG_DIR is required in installed mode")
        if self.installed_mode:
            assert self.model_root is not None
            try:
                self.model_root.resolve().relative_to(self.data_root.resolve())
            except ValueError as exc:
                raise ValueError("LVT_MODEL_ROOT must be inside LVT_DATA_ROOT") from exc
        parsed_ollama = urlsplit(self.ollama_url)
        if (
            parsed_ollama.scheme != "http"
            or parsed_ollama.hostname not in {"127.0.0.1", "::1"}
            or parsed_ollama.username is not None
            or parsed_ollama.password is not None
            or parsed_ollama.query
            or parsed_ollama.fragment
            or parsed_ollama.path not in {"", "/"}
        ):
            raise ValueError("LVT_OLLAMA_URL must be a local HTTP origin")
        if self.installed_mode and self.ollama_url != "http://127.0.0.1:11435":
            raise ValueError("installed LVT_OLLAMA_URL must be http://127.0.0.1:11435")

    @classmethod
    def from_env(cls) -> Settings:
        default_root = Path.home() / "Library" / "Application Support" / "LocalVideoTranscriber"
        data_root = Path(os.environ.get("LVT_DATA_ROOT", default_root)).expanduser()
        model_root = Path(os.environ.get("LVT_MODEL_ROOT", data_root / "models")).expanduser()
        ffmpeg_dir_value = os.environ.get("LVT_FFMPEG_DIR")
        return cls(
            data_root=data_root,
            host=os.environ.get("LVT_HOST", "127.0.0.1"),
            port=int(os.environ.get("LVT_PORT", "8765")),
            worker_concurrency=int(os.environ.get("LVT_WORKER_CONCURRENCY", "1")),
            model_root=model_root,
            installed_mode=_env_flag("LVT_INSTALLED_MODE", default=False),
            ffmpeg_dir=Path(ffmpeg_dir_value).expanduser() if ffmpeg_dir_value else None,
            ollama_url=os.environ.get("LVT_OLLAMA_URL", "http://127.0.0.1:11435"),
        )

    def ensure_directories(self) -> None:
        for name in ("config", "db", "runtime", "work", "exports", "logs"):
            (self.data_root / name).mkdir(parents=True, exist_ok=True)
        assert self.model_root is not None
        self.model_root.mkdir(parents=True, exist_ok=True)

    def configure_model_environment(self) -> None:
        assert self.model_root is not None
        huggingface_root = self.model_root / "huggingface"
        os.environ["HF_HOME"] = os.fspath(huggingface_root)
        os.environ["HF_HUB_CACHE"] = os.fspath(huggingface_root)
        os.environ["OLLAMA_MODELS"] = os.fspath(self.model_root / "ollama")


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value == "1":
        return True
    if value == "0":
        return False
    raise ValueError(f"{name} must be 0 or 1")
