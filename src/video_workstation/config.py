from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    return Path(os.getenv("VIDEO_WORKSTATION_DATA_DIR", "data")).resolve()


@dataclass(slots=True)
class Settings:
    data_dir: Path = field(default_factory=_default_data_dir)
    database_url: str | None = None
    asset_dir: Path | None = None
    archive_dir: Path | None = None
    session_secret: str = field(default_factory=lambda: os.getenv("VIDEO_WORKSTATION_SESSION_SECRET", ""))
    secure_cookies: bool = field(
        default_factory=lambda: os.getenv("VIDEO_WORKSTATION_SECURE_COOKIES", "0") == "1"
    )
    minimum_free_bytes: int = 100 * 1024**3
    worker_id: str = field(default_factory=lambda: os.getenv("VIDEO_WORKSTATION_WORKER_ID", "gpu-worker-1"))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.asset_dir = Path(self.asset_dir or self.data_dir / "assets").resolve()
        self.archive_dir = Path(self.archive_dir or self.data_dir / "archive").resolve()
        if self.database_url is None:
            self.database_url = f"sqlite:///{(self.data_dir / 'workstation.db').as_posix()}"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
