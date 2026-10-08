from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Project, Shot, Storyboard, User
from ..security import PasswordService


class PermissionDenied(RuntimeError):
    pass


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
