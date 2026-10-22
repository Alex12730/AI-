"""Deterministic beginner workflows using the existing durable task queue.

No model downloads or model calls occur while planning. Snapshots freeze the
approved inputs; downstream tasks refer to explicit task and asset IDs.
"""
from __future__ import annotations

import copy
import math
import re
import secrets
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..media import resolve_asset_path
from ..models import Asset, AuditLog, CreationPlan, ModelProfile, Shot, Task, User, utcnow
from ..storage import InsufficientStorage, StorageGuard, generated_asset_path
from .assets import sha256_file
from .models import ModelNotAdmitted, admit_generation, model_config_fingerprint, model_readiness
from .projects import PermissionDenied, create_project


RECIPES = {
    "product": {"name": "产品介绍 / 软件教程", "hint": "用真实录屏展示操作与功能。", "scene": "product_ui",
        "beats": ("展示产品或软件的真实全貌", "展示第一步操作", "展示关键功能", "展示操作结果", "回顾使用方法", "展示下一步行动")},
    "knowledge": {"name": "知识讲解", "hint": "讲稿搭配已有画面或生成的环境镜头；配音请先自行录制。", "scene": "broll",
        "beats": ("提出主题", "交代背景", "解释第一个要点", "解释第二个要点", "展示应用情境", "回顾主题")},
    "talking": {"name": "真人口播剪辑", "hint": "导入自己拍摄的视频，保留原声。", "scene": "product_ui",
        "beats": ("开场介绍", "说明观点", "补充解释", "展示案例", "回顾重点", "结束语")},
    "acting": {"name": "AI 人物表演", "hint": "生成带声音的表演短段，不保证逐字台词或精确口型。", "scene": "character",
        "beats": ("人物出场", "人物观察环境", "人物开始行动", "人物作出反应", "人物完成动作", "场景收尾")},
    "story": {"name": "故事短片", "hint": "按短镜头讲故事；当前不保证跨镜头人物是同一张脸。", "scene": "broll",
        "beats": ("交代故事环境", "引出变化", "呈现进展", "展现转折", "呈现结果", "结束画面")},
    "advert": {"name": "创意广告", "hint": "真实产品画面与生成环境镜头组合；价格和商标用真实素材。", "scene": "broll",
        "beats": ("展示使用环境", "展示真实产品", "展示使用情境", "展示真实产品细节", "呈现氛围", "展示真实品牌与行动信息")},
}


def create_guided_project(session: Session, actor: User, *, name: str, script: str,
                          recipe: str, target_seconds: int, aspect_ratio: str) -> CreationPlan:
    if recipe not in RECIPES:
        raise ValueError("请选择内置的视频类型")
    if type(target_seconds) is not int or not 5 <= target_seconds <= 180 or target_seconds % 5:
        raise ValueError("本阶段整片时长请选择 5 秒的倍数，范围 5–180 秒")
    if aspect_ratio not in {"16:9", "9:16"}:
        raise ValueError("请选择横屏或竖屏")
    if not script.strip() or len(script) > 20_000 or not name.strip() or len(name) > 160:
        raise ValueError("请填写作品名称（160 字以内）和视频内容（20000 字以内）")
    sentences = [s.strip() for s in re.split(r"[。！？!?\n]+", script) if s.strip()]
    count = target_seconds // 5
    if len(sentences) > count:
        raise ValueError(f"内容已拆为 {len(sentences)} 段，超过 {count} 个镜头；请增加时长或精简内容")
    project = create_project(session, actor, name, script)
    board = project.storyboards[0]
    # Keep no ambiguous leftover shots from the legacy punctuation splitter.
    board.shots.clear()
    session.flush()
    recipe_data = RECIPES[recipe]
    for i in range(count):
        beat = recipe_data["beats"][min(5, math.floor(i * 6 / count))]
        sentence = sentences[i] if i < len(sentences) else f"{script.strip()}；画面安排：{beat}"
        scene = recipe_data["scene"]
        if recipe == "advert" and i in {1, 3, count - 1}:
            scene = "product_ui"
        if any(word in sentence for word in ("界面", "按钮", "录屏", "商标", "包装", "价格")):
            scene = "product_ui"
        if recipe == "story" and scene != "product_ui" and any(word in sentence for word in ("人物", "对白", "表演")):
            scene = "character"
        board.shots.append(Shot(sequence_no=i + 1, title=beat, prompt=sentence,
            scene_type=scene, duration_seconds=5, aspect_ratio=aspect_ratio, status="draft"))
    plan = CreationPlan(project=project, storyboard=board, created_by_id=actor.id,
        recipe=recipe, target_seconds=target_seconds, aspect_ratio=aspect_ratio)
    session.add(plan)
    session.flush()
    return plan


def recommend_shot(shot: Shot, profiles: list[ModelProfile]) -> dict:
    if shot.scene_type == "product_ui":
        return {"source": "material", "model_slug": None,
            "reason": "需要真实视频素材，避免生成错误的界面、产品或口播。", "alternatives": []}
    slugs = ("minimax-h3-fl2va", "ltx-2.3") if shot.scene_type == "character" else ("wan-2.2", "ltx-2.3", "minimax-h3-fl2va")
    by_slug = {p.slug: p for p in profiles}
    candidates = []
    for slug in slugs:
        p = by_slug.get(slug)
        if p is None or model_readiness(p) != "admitted":
            continue
        # Current H3 graph has no negative prompt input.
        if slug == "minimax-h3-fl2va" and shot.negative_prompt.strip():
            continue
        try:
            if slug == "minimax-h3-fl2va" and shot.duration_seconds > 15:
                continue
            admit_generation(p, duration_seconds=shot.duration_seconds, aspect_ratio=shot.aspect_ratio)
        except ModelNotAdmitted:
            continue
        candidates.append(slug)
    if not candidates:
        return {"source": "generate", "model_slug": None, "alternatives": [],
            "reason": f"这个镜头的 {shot.duration_seconds:g} 秒 {shot.aspect_ratio} 档位还没有可用的正式验证；请先验证模型，或导入真实视频。"}
    selected = candidates[0]
    reason = "人物表演优先使用可输出声音的已验证模型；不保证精确台词。" if shot.scene_type == "character" else "环境镜头不要求对白，优先选择已验证的画面生成模型。"
    return {"source": "generate", "model_slug": selected, "alternatives": candidates[1:], "reason": reason}


def assert_plan_owner(actor: User, plan: CreationPlan) -> None:
    if not actor.is_active or actor.id != plan.created_by_id:
        raise PermissionDenied("只有本作品的创建者可以确认和控制制作")


def valid_bound_asset(session: Session, plan: CreationPlan, shot: Shot, asset_id: str, settings: Settings) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != plan.project_id or asset.shot_id != shot.id or asset.kind != "video":
        raise ValueError("请选择当前镜头的真实视频素材")
    if not asset.metadata_json.get("qc", {}).get("passed"):
        raise ValueError("素材尚未通过视频质检")
    path = resolve_asset_path(asset, settings)
    if sha256_file(path) != asset.sha256:
        raise ValueError("素材文件已经改变，请重新导入")
    duration = float(asset.metadata_json.get("qc", {}).get("duration_seconds") or 0)
    if abs(duration - shot.duration_seconds) > .5:
        raise ValueError("请先导入与镜头时长相符的视频；当前不自动裁剪或变速")
    return asset


def creation_choices(session: Session, plan: CreationPlan, settings: Settings) -> list[dict]:
    profiles = list(session.scalars(select(ModelProfile)))
    choices = []
    bindings = plan.snapshot_json.get("bindings", {})
    for shot in sorted(plan.storyboard.shots, key=lambda s: s.sequence_no):
        decision = recommend_shot(shot, profiles)
        assets = list(session.scalars(select(Asset).where(Asset.shot_id == shot.id,
            Asset.project_id == plan.project_id, Asset.kind == "video").order_by(Asset.created_at.desc())))
        asset_id = bindings.get(shot.id)
        issue = None
        if asset_id:
            try:
                valid_bound_asset(session, plan, shot, asset_id, settings)
                decision = {"source": "material", "model_slug": None, "alternatives": [], "reason": "使用你选定的本地视频，保留原声。"}
            except ValueError as exc:
                issue = str(exc)
        elif not decision["model_slug"]:
            issue = decision["reason"]
        choices.append({"shot": shot, **decision, "assets": assets, "asset_id": asset_id, "issue": issue})
    return choices


def start_creation(session: Session, actor: User, plan: CreationPlan, settings: Settings) -> None:
    assert_plan_owner(actor, plan)
    # Write first to serialize concurrent confirmations before inspecting state.
    session.execute(update(CreationPlan).where(CreationPlan.id == plan.id).values(status=CreationPlan.status))
    session.refresh(plan)
    if plan.status != "draft":
        return
    choices = creation_choices(session, plan, settings)
    issues = [f"第 {c['shot'].sequence_no} 段：{c['issue']}" for c in choices if c["issue"]]
    if issues:
        raise ValueError("\n".join(issues))
    nodes = []
    estimated = 1
    for c in choices:
        shot = c["shot"]
        node = {"shot_id": shot.id, "prompt": shot.prompt, "negative_prompt": shot.negative_prompt,
            "duration_seconds": shot.duration_seconds, "aspect_ratio": shot.aspect_ratio,
            "asset_id": c["asset_id"], "task_id": None, "model_slug": c["model_slug"], "seed": secrets.randbits(32)}
        if c["model_slug"]:
            profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == c["model_slug"]))
            node.update(profile_id=profile.id, fingerprint=model_config_fingerprint(profile))
            estimated = max(estimated, int(shot.duration_seconds * 4 * 1024**3))
        nodes.append(node)
    StorageGuard(settings.asset_dir, minimum_free_bytes=settings.minimum_free_bytes).ensure_capacity(estimated_temp_bytes=estimated)
    now = utcnow()
    board = plan.storyboard
    if board.status != "draft" or len(nodes) == 0:
        raise ValueError("请从个人制作草稿确认分镜")
    board.status = "approved"
    board.submitted_at = now
    board.approved_at = now
    board.approved_by_id = actor.id
    for shot in board.shots:
        shot.status = "approved"
    plan.project.status = "approved"
    plan.snapshot_json = {"schema_version": 1, "storyboard_version": board.version,
        "nodes": nodes, "compose_task_id": None, "bindings": plan.snapshot_json.get("bindings", {})}
    plan.status = "running"
    session.add(AuditLog(actor_id=actor.id, action="creation.confirm", entity_type="creation",
        entity_id=plan.id, details_json=copy.deepcopy(plan.snapshot_json)))
    session.flush()
    _advance_one(session, plan, settings)


def set_creation_paused(session: Session, actor: User, plan: CreationPlan, paused: bool):
    assert_plan_owner(actor, plan)
    session.execute(update(CreationPlan).where(CreationPlan.id == plan.id).values(status=CreationPlan.status))
    session.refresh(plan)
    if paused and plan.status == "running":
        plan.status = "pause_requested"
    elif not paused and plan.status in {"pause_requested", "paused"}:
        plan.status = "running"
    else:
        raise ValueError("当前阶段不能执行此操作")
    session.add(AuditLog(actor_id=actor.id, action="creation.pause" if paused else "creation.resume",
        entity_type="creation", entity_id=plan.id))


def advance_creations(session: Session, settings: Settings) -> None:
    # SQLite writes serialize competing schedulers, including a web confirmation.
    # Acquire the write transaction BEFORE reading plans to avoid stale snapshots.
    session.execute(update(CreationPlan).where(CreationPlan.status.in_(("running", "pause_requested"))).values(status=CreationPlan.status))
    plans = list(session.scalars(select(CreationPlan).where(CreationPlan.status.in_(("running", "pause_requested"))).execution_options(populate_existing=True)))
    for plan in plans:
        try:
            _advance_one(session, plan, settings)
        except (ValueError, InsufficientStorage, OSError) as exc:
            plan.status = "blocked"
            plan.error_message = str(exc)
    session.flush()


def _advance_one(session: Session, plan: CreationPlan, settings: Settings):
    snap = copy.deepcopy(plan.snapshot_json)
    required = {"shot_id", "prompt", "negative_prompt", "duration_seconds", "aspect_ratio", "asset_id", "task_id", "model_slug", "seed"}
    if (not isinstance(snap, dict) or snap.get("schema_version") != 1
        or "compose_task_id" not in snap or not isinstance(snap.get("nodes"), list)
        or not snap["nodes"] or any(not isinstance(n, dict) or not required <= n.keys()
            or (n.get("model_slug") and not {"profile_id", "fingerprint"} <= n.keys()) for n in snap["nodes"])):
        raise ValueError("制作快照不完整；已保留完成的素材，请检查制作记录")
    nodes = snap.get("nodes", [])
    for node in nodes:
        if node["asset_id"]:
            # Frozen asset IDs are never replaced with the newest upload.
            valid_bound_asset(session, plan, session.get(Shot, node["shot_id"]), node["asset_id"], settings)
            continue
        task = session.get(Task, node["task_id"]) if node["task_id"] else None
        if task:
            if task.status in {"terminal_failed", "cancelled"}:
                plan.status = "failed"
                plan.error_message = task.error_message or "镜头任务已停止，请到高级项目检查后重新制作。"
                return
            if task.status != "succeeded":
                return  # Includes paused queued tasks: no downstream dispatch.
            asset = session.scalar(select(Asset).where(Asset.task_id == task.id, Asset.kind == "video"))
            if asset is None:
                raise ValueError("镜头任务已结束，但缺少通过质检的视频；请检查任务")
            valid_bound_asset(session, plan, session.get(Shot, node["shot_id"]), asset.id, settings)
            node["asset_id"] = asset.id
            continue
        if plan.status == "pause_requested":
            plan.snapshot_json = snap
            plan.status = "paused"
            return
        profile = session.get(ModelProfile, node["profile_id"])
        if profile is None or model_config_fingerprint(profile) != node["fingerprint"]:
            raise ValueError("模型配置已变化，不能继续使用本次确认的分镜，请重新检查")
        preset = admit_generation(profile, duration_seconds=node["duration_seconds"], aspect_ratio=node["aspect_ratio"])
        key = f"creation:{plan.id}:{node['shot_id']}"
        task = session.scalar(select(Task).where(Task.idempotency_key == key))
        if task is None:
            estimated = int(node["duration_seconds"] * 4 * 1024**3)
            StorageGuard(settings.asset_dir, minimum_free_bytes=settings.minimum_free_bytes).ensure_capacity(estimated_temp_bytes=estimated)
            task = Task(project_id=plan.project_id, shot_id=node["shot_id"], created_by_id=plan.created_by_id,
                model_profile_id=profile.id, task_type="generate", status="queued", priority=1,
                idempotency_key=key, model_version_snapshot=profile.model_version,
                quantization_snapshot=profile.quantization, model_config_fingerprint=node["fingerprint"],
                runtime_config_snapshot=copy.deepcopy(profile.runtime_config_json), estimated_temp_bytes=estimated)
            session.add(task)
            session.flush()
            task.payload_json = {"prompt": node["prompt"], "negative_prompt": node["negative_prompt"],
                "duration_seconds": node["duration_seconds"], "aspect_ratio": node["aspect_ratio"],
                "seed": node["seed"], "validated_preset": preset, "minimum_free_bytes": settings.minimum_free_bytes,
                "output_path": str(generated_asset_path(settings.asset_dir, plan.project_id, task.id, ".mp4"))}
        node["task_id"] = task.id
        plan.snapshot_json = snap
        return
    plan.snapshot_json = copy.deepcopy(snap)
    compose = session.get(Task, snap["compose_task_id"]) if snap["compose_task_id"] else None
    if compose:
        if compose.status == "succeeded":
            asset = session.scalar(select(Asset).where(Asset.task_id == compose.id, Asset.kind == "composite"))
            if asset is None:
                raise ValueError("合成任务缺少成片文件，请检查任务")
            plan.status = "preview_ready"
        elif compose.status in {"terminal_failed", "cancelled"}:
            plan.status = "failed"
            plan.error_message = compose.error_message or "合成任务已停止"
        return
    if plan.status == "pause_requested":
        plan.status = "paused"
        return
    if not nodes:
        raise ValueError("制作计划缺少镜头")
    key = f"creation:{plan.id}:compose"
    compose = session.scalar(select(Task).where(Task.idempotency_key == key))
    if compose is None:
        assets = [session.get(Asset, n["asset_id"]) for n in nodes]
        estimated = max(1, sum(Path(a.path).stat().st_size for a in assets) * 3)
        StorageGuard(settings.asset_dir, minimum_free_bytes=settings.minimum_free_bytes).ensure_capacity(estimated_temp_bytes=estimated)
        compose = Task(project_id=plan.project_id, created_by_id=plan.created_by_id,
            task_type="compose_project", priority=1, idempotency_key=key,
            model_version_snapshot="ffmpeg-local", quantization_snapshot="not-applicable", estimated_temp_bytes=estimated)
        session.add(compose)
        session.flush()
        compose.payload_json = {"asset_ids": [n["asset_id"] for n in nodes],
            "subtitle_asset_id": None, "aspect_ratio": plan.aspect_ratio,
            "expected_duration": sum(float(a.metadata_json["qc"]["duration_seconds"]) for a in assets),
            "minimum_free_bytes": settings.minimum_free_bytes, "upscale_requested": False,
            "output_path": str(generated_asset_path(settings.asset_dir, plan.project_id, compose.id, ".mp4"))}
    snap["compose_task_id"] = compose.id
    plan.snapshot_json = snap
