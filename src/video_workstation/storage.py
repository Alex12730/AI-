from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable


class InsufficientStorage(RuntimeError):
    pass


class StorageGuard:
    def __init__(self, root: Path, *, minimum_free_bytes: int, usage: Callable = shutil.disk_usage):
        self.root = Path(root).resolve()
        self.minimum_free_bytes = minimum_free_bytes
        self.usage = usage

    def ensure_capacity(self, *, estimated_temp_bytes: int) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        free = int(self.usage(self.root).free)
        if free < self.minimum_free_bytes:
            raise InsufficientStorage("磁盘剩余空间低于最低保留空间")
        if free < estimated_temp_bytes * 2:
            raise InsufficientStorage("磁盘剩余空间低于预计临时空间的 2 倍")
        return free


SAFE_ID = re.compile(r"^[a-f0-9]{32}$")
ALLOWED_SUFFIXES = {".mp4", ".mov", ".webm", ".wav", ".srt", ".json", ".png", ".jpg"}


def generated_asset_path(root: Path, project_id: str, task_id: str, suffix: str) -> Path:
    if not SAFE_ID.fullmatch(project_id) or not SAFE_ID.fullmatch(task_id):
        raise ValueError("项目和任务 ID 必须由系统生成")
    normalized_suffix = suffix.lower()
    if normalized_suffix not in ALLOWED_SUFFIXES:
        raise ValueError("不支持的资产类型")
    root = Path(root).resolve()
    candidate = (root / project_id / f"{task_id}{normalized_suffix}").resolve()
    if root not in candidate.parents:
        raise ValueError("资产路径越界")
    return candidate
