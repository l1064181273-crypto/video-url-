from __future__ import annotations

import os

import uvicorn

from lvt.api.app import create_app
from lvt.core.config import Settings
from lvt.security.token import load_or_create_token

settings = Settings.from_env()
settings.ensure_directories()
app = create_app(
    db_path=settings.data_root / "db" / "lvt.sqlite3",
    api_token=os.environ.get("LVT_TOKEN")
    or load_or_create_token(settings.data_root / "config" / "api-token"),
)


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
