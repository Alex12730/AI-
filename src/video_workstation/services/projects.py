from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Project, Shot, Storyboard, User
from ..security import PasswordService


class PermissionDenied(RuntimeError):
    pass


SHOT_DURATION_PRESETS = (5, 10, 15, 20)
SHOT_ASPECT_RATIOS = ("16:9", "9:16")
SHOT_SCENE_TYPES = ("product_ui", "broll", "character", "series_drama")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def bootstrap_admin(session: Session, username: str, password: str) -> User:
    if session.scalar(select(User).where(User.role == "admin")) is not None:
        raise ValueError("管理员已经存在")
    if len(username.strip()) < 3:
        raise ValueError("用户名至少 3 个字符")
    password_hash = PasswordService().hash(password)
    admin = User(username=username.strip(), password_hash=password_hash, role="admin")
    session.add(admin)
    session.flush()
    session.add(
        AuditLog(
            actor_id=admin.id,
            action="user.bootstrap_admin",
            entity_type="user",
            entity_id=admin.id,
        )
    )
    return admin


def create_member(session: Session, actor: User, username: str, password: str) -> User:
    require_admin(actor)
    normalized = username.strip()
    if len(normalized) < 3:
        raise ValueError("用户名至少 3 个字符")
    if session.scalar(select(User).where(User.username == normalized)) is not None:
        raise ValueError("用户名已存在")
    member = User(username=normalized, password_hash=PasswordService().hash(password), role="member")
    session.add(member)
    session.flush()
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="user.create",
            entity_type="user",
            entity_id=member.id,
            details_json={"role": "member"},
        )
    )
    return member


def require_admin(actor: User) -> None:
    if not actor.is_active or actor.role != "admin":
        raise PermissionDenied("仅管理员可以执行此操作")


def assert_project_access(actor: User, project: Project) -> None:
    if actor.role == "admin" or actor.id == project.created_by_id:
        return
    if any(member.id == actor.id for member in project.members):
        return
    raise PermissionDenied("无权访问该项目")


def validate_priority(actor: User, priority: int) -> int:
    if priority not in (0, 1, 2):
        raise ValueError("优先级必须是 0、1 或 2")
    if priority == 0 and actor.role != "admin":
        raise PermissionDenied("P0 仅限管理员")
    return priority


def _split_script(script: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[。！？!?\n]+", script) if part.strip()]
    return parts or ["待补充分镜内容"]


def _scene_type(sentence: str) -> str:
    if any(word in sentence for word in ("界面", "录屏", "按钮", "文字", "功能")):
        return "product_ui"
    if any(word in sentence for word in ("人物", "对白", "口播", "表演")):
        return "character"
    return "broll"


def create_project(session: Session, actor: User, name: str, source_script: str) -> Project:
    if not actor.is_active:
        raise PermissionDenied("账号已停用")
    if not name.strip():
        raise ValueError("项目名称不能为空")
    project = Project(name=name.strip(), source_script=source_script.strip(), created_by_id=actor.id)
    project.members.append(actor)
    storyboard = Storyboard(created_by_id=actor.id, status="draft", version=1)
    for index, sentence in enumerate(_split_script(source_script), start=1):
        scene_type = _scene_type(sentence)
        storyboard.shots.append(
            Shot(
                sequence_no=index,
                title=f"镜头 {index}",
                prompt=sentence,
                scene_type=scene_type,
                duration_seconds=5,
                aspect_ratio="16:9",
                status="draft",
            )
        )
    project.storyboards.append(storyboard)
    session.add(project)
    session.flush()
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="project.create",
            entity_type="project",
            entity_id=project.id,
            details_json={"storyboard_id": storyboard.id},
        )
    )
    return project


def update_draft_shot(
    session: Session,
    actor: User,
    shot: Shot,
    *,
    title: str,
    prompt: str,
    duration_seconds: float,
    aspect_ratio: str,
    negative_prompt: str | None = None,
    scene_type: str | None = None,
) -> Shot:
    assert_project_access(actor, shot.storyboard.project)
    if shot.storyboard.status != "draft":
        raise ValueError("只有草稿分镜可以编辑")
    if duration_seconds not in SHOT_DURATION_PRESETS:
        raise ValueError("时长只能选择 5、10、15、20 秒")
    if aspect_ratio not in SHOT_ASPECT_RATIOS:
        raise ValueError("画幅只能选择 16:9、9:16")
    normalized_title = title.strip()
    normalized_prompt = prompt.strip()
    normalized_scene_type = scene_type or shot.scene_type
    if not normalized_title:
        raise ValueError("镜头标题不能为空")
    if not normalized_prompt:
        raise ValueError("正向提示词不能为空")
    if normalized_scene_type not in SHOT_SCENE_TYPES:
        raise ValueError("镜头类型不合法")

    shot.title = normalized_title
    shot.prompt = normalized_prompt
    shot.negative_prompt = (negative_prompt if negative_prompt is not None else shot.negative_prompt).strip()
    shot.scene_type = normalized_scene_type
    shot.duration_seconds = duration_seconds
    shot.aspect_ratio = aspect_ratio
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="shot.update",
            entity_type="shot",
            entity_id=shot.id,
            details_json={
                "duration_seconds": duration_seconds,
                "aspect_ratio": aspect_ratio,
                "scene_type": normalized_scene_type,
                "negative_prompt_updated": negative_prompt is not None,
            },
        )
    )
    return shot


def bulk_update_draft_shots(
    session: Session,
    actor: User,
    storyboard: Storyboard,
    *,
    duration_seconds: float,
    aspect_ratio: str,
) -> int:
    assert_project_access(actor, storyboard.project)
    if storyboard.status != "draft":
        raise ValueError("只有草稿分镜可以编辑")
    if duration_seconds not in SHOT_DURATION_PRESETS:
        raise ValueError("时长只能选择 5、10、15、20 秒")
    if aspect_ratio not in SHOT_ASPECT_RATIOS:
        raise ValueError("画幅只能选择 16:9、9:16")

    for shot in storyboard.shots:
        shot.duration_seconds = duration_seconds
        shot.aspect_ratio = aspect_ratio

    shot_count = len(storyboard.shots)
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="storyboard.shots.bulk_update",
            entity_type="storyboard",
            entity_id=storyboard.id,
            details_json={
                "duration_seconds": duration_seconds,
                "aspect_ratio": aspect_ratio,
                "shot_count": shot_count,
            },
        )
    )
    return shot_count


def submit_storyboard(session: Session, actor: User, storyboard: Storyboard) -> Storyboard:
    assert_project_access(actor, storyboard.project)
    if storyboard.status != "draft":
        raise ValueError("只有草稿分镜可以提交")
    if not storyboard.shots:
        raise ValueError("分镜不能为空")
    storyboard.status = "pending_approval"
    storyboard.project.status = "pending_approval"
    storyboard.submitted_at = _now()
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="storyboard.submit",
            entity_type="storyboard",
            entity_id=storyboard.id,
        )
    )
    return storyboard


def withdraw_storyboard(session: Session, actor: User, storyboard: Storyboard) -> Storyboard:
    assert_project_access(actor, storyboard.project)
    if actor.role != "admin" and actor.id != storyboard.project.created_by_id:
        raise PermissionDenied("只有项目创建者或管理员可以退回修改")
    if storyboard.status != "pending_approval":
        raise ValueError("只有待审批分镜可以退回修改")

    storyboard.status = "draft"
    storyboard.project.status = "draft"
    storyboard.submitted_at = None
    storyboard.approved_by_id = None
    storyboard.approved_at = None
    for shot in storyboard.shots:
        shot.status = "draft"
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="storyboard.withdraw",
            entity_type="storyboard",
            entity_id=storyboard.id,
        )
    )
    return storyboard


def approve_storyboard(session: Session, actor: User, storyboard: Storyboard) -> Storyboard:
    require_admin(actor)
    if storyboard.status != "pending_approval":
        raise ValueError("只有待审批分镜可以审批")
    storyboard.status = "approved"
    storyboard.project.status = "approved"
    storyboard.approved_by_id = actor.id
    storyboard.approved_at = _now()
    for shot in storyboard.shots:
        shot.status = "approved"
    session.add(
        AuditLog(
            actor_id=actor.id,
            action="storyboard.approve",
            entity_type="storyboard",
            entity_id=storyboard.id,
        )
    )
    return storyboard
