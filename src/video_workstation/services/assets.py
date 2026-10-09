from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Asset, AuditLog, Shot, Task, User, new_id
from ..qc import QCResult, probe_media
from ..storage import generated_asset_path
from .projects import assert_project_access


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def register_video_asset(session: Session, task: Task, path: Path, qc: QCResult, *, kind: str = "video") -> Asset:
    if task.shot_id is None and kind == "video":
        raise ValueError("镜头视频任务缺少镜头关联")
    path = Path(path)
    if not path.is_file():
        raise ValueError("媒体输出文件不存在")
    if not qc.passed:
        raise ValueError("媒体质检未通过")
    existing = session.scalar(select(Asset).where(Asset.task_id == task.id, Asset.kind == kind))
    if existing is not None:
        return existing
    metadata = asdict(qc)
    asset = Asset(
        project_id=task.project_id,
        shot_id=task.shot_id,
        task_id=task.id,
        kind=kind,
        path=str(path.resolve()),
        sha256=sha256_file(path),
        metadata_json={"qc": metadata},
    )
    session.add(asset)
    session.flush()
    return asset


def store_uploaded_video(
    session: Session,
    actor: User,
    shot: Shot,
    filename: str,
    stream: BinaryIO,
    settings: Settings,
) -> Asset:
    assert_project_access(actor, shot.storyboard.project)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise ValueError("只支持 MP4、MOV 或 WebM 视频格式")
    asset_id = new_id()
    final_path = generated_asset_path(settings.asset_dir, shot.storyboard.project_id, asset_id, suffix)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = final_path.with_name(f".{final_path.name}.tmp")
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary_path.open("wb") as handle:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > settings.maximum_video_upload_bytes:
                    raise ValueError("上传视频过大")
                digest.update(chunk)
                handle.write(chunk)
        if total == 0:
            raise ValueError("上传视频为空")
        qc = probe_media(temporary_path, expected_duration=shot.duration_seconds, require_audio=False)
        if not qc.passed:
            raise ValueError("上传视频质检失败: " + ",".join(qc.errors))
        temporary_path.replace(final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    asset = Asset(
        id=asset_id,
        project_id=shot.storyboard.project_id,
        shot_id=shot.id,
        kind="video",
        path=str(final_path),
        sha256=digest.hexdigest(),
        metadata_json={
            "source": "upload",
            "original_filename": Path(filename).name,
            "size_bytes": total,
            "qc": asdict(qc),
        },
    )
    session.add(asset)
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="asset.video_upload",
            entity_type="asset",
            entity_id=asset.id,
            details_json={"project_id": asset.project_id, "shot_id": shot.id, "size_bytes": total},
        )
    )
    session.flush()
    return asset
