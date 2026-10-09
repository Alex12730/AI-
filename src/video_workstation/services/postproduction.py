from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings
from ..media import resolve_asset_path
from ..models import Asset, AuditLog, ModelProfile, Project, Task, User
from ..storage import StorageGuard, generated_asset_path
from .projects import assert_project_access, require_admin, validate_priority
from .models import video2x_is_admitted


def create_compose_task(
    session: Session,
    actor: User,
    project: Project,
    *,
    asset_ids: list[str],
    subtitle_asset_id: str | None,
    aspect_ratio: str,
    priority: int,
    settings: Settings,
    upscale: bool = False,
) -> Task:
    require_admin(actor)
    assert_project_access(actor, project)
    if aspect_ratio not in {"16:9", "9:16"}:
        raise ValueError("只支持 16:9 或 9:16 交付画幅")
    if not project.storyboards:
        raise ValueError("项目没有分镜")
    storyboard = max(project.storyboards, key=lambda item: item.version)
    if storyboard.status != "approved":
        raise ValueError("分镜尚未审批")

    approved_shots = sorted(
        (shot for shot in storyboard.shots if shot.status == "approved"),
        key=lambda item: item.sequence_no,
    )
    if not approved_shots:
        raise ValueError("没有已批准镜头")
    selected: dict[str, Asset] = {}
    for asset_id in asset_ids:
        asset = session.get(Asset, asset_id)
        if asset is None or asset.project_id != project.id or asset.kind != "video" or asset.shot_id is None:
            raise ValueError("合成输入素材无效或不属于当前项目")
        if not asset.metadata_json.get("qc", {}).get("passed"):
            raise ValueError("合成输入素材尚未通过质检")
        if float(asset.metadata_json.get("qc", {}).get("duration_seconds") or 0) <= 0:
            raise ValueError("合成输入素材缺少有效时长")
        resolve_asset_path(asset, settings)
        if asset.shot_id in selected:
            raise ValueError("每个镜头只能选择一个素材")
        selected[asset.shot_id] = asset

    missing = [shot.title for shot in approved_shots if shot.id not in selected]
    if missing:
        raise ValueError("以下镜头缺少合格素材: " + "、".join(missing))
    ordered_assets = [selected[shot.id] for shot in approved_shots]

    subtitle_asset = None
    if subtitle_asset_id:
        subtitle_asset = session.get(Asset, subtitle_asset_id)
        if (
            subtitle_asset is None
            or subtitle_asset.project_id != project.id
            or subtitle_asset.kind != "subtitle"
            or not subtitle_asset.metadata_json.get("confirmed")
        ):
            raise ValueError("字幕素材无效或尚未确认")
        resolve_asset_path(subtitle_asset, settings)

    upscale_profile = None
    if upscale:
        from sqlalchemy import select
        upscale_profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == "video2x"))
        if upscale_profile is None or not video2x_is_admitted(upscale_profile, scale=2):
            raise ValueError("Video2X 尚未配置并通过 2 倍超分准入")

    estimated_temp_bytes = max(sum(Path(asset.path).stat().st_size for asset in ordered_assets) * 3, 1)
    StorageGuard(settings.asset_dir, minimum_free_bytes=settings.minimum_free_bytes).ensure_capacity(
        estimated_temp_bytes=estimated_temp_bytes
    )
    task = Task(
        project_id=project.id,
        created_by_id=actor.id,
        task_type="compose_project",
        status="queued",
        priority=validate_priority(actor, priority),
        payload_json={},
        model_version_snapshot="ffmpeg-local",
        quantization_snapshot="not-applicable",
        estimated_temp_bytes=estimated_temp_bytes,
    )
    session.add(task)
    session.flush()
    output_path = generated_asset_path(settings.asset_dir, project.id, task.id, ".mp4")
    task.payload_json = {
        "asset_ids": [asset.id for asset in ordered_assets],
        "subtitle_asset_id": subtitle_asset.id if subtitle_asset else None,
        "aspect_ratio": aspect_ratio,
        "output_path": str(output_path),
        "expected_duration": sum(float(asset.metadata_json["qc"].get("duration_seconds") or 0) for asset in ordered_assets),
        "minimum_free_bytes": settings.minimum_free_bytes,
        "upscale_requested": bool(upscale),
        "upscale_profile_id": upscale_profile.id if upscale_profile else None,
    }
    session.add(AuditLog(
        actor_id=actor.id,
        action="task.compose_enqueue",
        entity_type="task",
        entity_id=task.id,
        details_json={"project_id": project.id, "asset_ids": task.payload_json["asset_ids"]},
    ))
    session.flush()
    return task
