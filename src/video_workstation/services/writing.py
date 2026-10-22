"""Personal writing drafts, immutable candidates and explicit publication."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import (AuditLog, CreationPlan, ModelProfile, Project, Shot, Storyboard,
    Task, User, WritingDocument, WritingPublication, WritingRevision)
from ..writing_contracts import WritingBody, WritingRequest
from .models import model_config_fingerprint
from .projects import PermissionDenied, create_project

TEMPLATE_VERSION = "local-writing-v1"


class WritingConflict(ValueError):
    pass


def assert_writing_owner(actor: User, document: WritingDocument):
    if not actor.is_active or actor.id != document.created_by_id:
        raise PermissionDenied("只有作品创建者可以编辑和确认这份文案")


def _lock(session: Session, document: WritingDocument):
    session.execute(update(Project).where(Project.id == document.project_id).values(status=Project.status))
    session.refresh(document)


def _version_check(document: WritingDocument, expected_latest: int):
    if document.latest_version != expected_latest:
        raise WritingConflict("文案已有新版本；你的输入已保留，请比较最新版本后重新保存")


def get_writing_revision(session: Session, document: WritingDocument, version: int) -> WritingRevision:
    revision = session.scalar(select(WritingRevision).where(WritingRevision.document_id == document.id,
        WritingRevision.version == version))
    if revision is None:
        raise ValueError("文案版本不存在")
    return revision


def create_writing_document(session: Session, actor: User, request: WritingRequest) -> WritingDocument:
    request = WritingRequest.model_validate(request.model_dump())
    project = create_project(session, actor, request.idea[:80], request.idea)
    document = WritingDocument(project=project, created_by_id=actor.id, idea=request.idea,
        recipe=request.recipe, target_seconds=request.target_seconds, aspect_ratio=request.aspect_ratio)
    session.add(document);session.flush()
    session.add(AuditLog(actor_id=actor.id,action="writing.create",entity_type="writing",entity_id=document.id))
    return document


def _append(session, document, actor_id, body, *, kind, parent_version=None, task_id=None):
    body.validate_for_target(document.target_seconds)
    version=document.latest_version+1
    revision=WritingRevision(document_id=document.id,version=version,parent_version=parent_version,
        kind=kind,body_json=body.model_dump(),created_by_id=actor_id,task_id=task_id)
    document.latest_version=version
    session.add(revision);session.flush()
    return revision


def save_writing_revision(session: Session, actor: User, document: WritingDocument, body: WritingBody,
                          *, expected_latest: int) -> WritingRevision:
    assert_writing_owner(actor,document);_lock(session,document);_version_check(document,expected_latest)
    revision=_append(session,document,actor.id,WritingBody.model_validate(body.model_dump()),
        kind="manual",parent_version=document.selected_version)
    document.selected_version=revision.version
    session.add(AuditLog(actor_id=actor.id,action="writing.save",entity_type="writing",entity_id=document.id,
        details_json={"version":revision.version}))
    return revision


def writer_is_ready(profile: ModelProfile | None) -> bool:
    ready=bool(profile and profile.enabled and profile.provider=="local" and
        (profile.capabilities_json or {}).get("text_ready") is True and
        profile.capabilities_json.get("text_ready_fingerprint")==model_config_fingerprint(profile))
    if not ready:return False
    # Fast page/queue check. Full file hashes are checked again by the adapter.
    from ..writer_deployment import file_sha256,local_package_path
    try:
        runtime=profile.runtime_config_json;manifest=Path(runtime['writer_manifest']).resolve()
        if not manifest.is_file() or manifest.stat().st_size>1024*1024 or file_sha256(manifest)!=runtime['manifest_sha256']:
            return False
        data=json.loads(manifest.read_text(encoding='utf-8-sig'));root=manifest.parent
        for row in [*data['engine']['files'],{'path':data['model']['filename'],'bytes':data['model']['bytes']}]:
            path=local_package_path(root,row['path'])
            if not path.is_file() or path.stat().st_size!=row['bytes']:return False
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def enqueue_writing(session: Session, actor: User, document: WritingDocument, request: WritingRequest,
                    *, expected_latest: int, request_key: str, expected_selected: int | None = None) -> Task:
    assert_writing_owner(actor,document);_lock(session,document)
    if not re.fullmatch(r"[a-f0-9]{32}",request_key):
        raise ValueError("生成请求标识无效，请刷新页面后重试")
    if (request.recipe,request.target_seconds,request.aspect_ratio,request.idea)!=(document.recipe,
        document.target_seconds,document.aspect_ratio,document.idea):
        raise ValueError("写作请求必须匹配当前作品的类型、时长、画幅和想法")
    request_json=request.model_dump()
    digest=hashlib.sha256(json.dumps(request_json,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    key=f"write:{document.id}:{request_key}"
    existing=session.scalar(select(Task).where(Task.idempotency_key==key))
    if existing:
        if existing.payload_json.get("request_sha256")!=digest:
            raise WritingConflict("同一个生成请求包含不同内容，请重新提交")
        return existing
    _version_check(document,expected_latest)
    if expected_selected is not None and expected_selected!=(document.selected_version or 0):
        raise WritingConflict("另一个页面切换了文案版本；请先核对当前版本后再改写")
    base_version=document.selected_version if request.base_body else None
    if request.base_body:
        if base_version is None or request.base_body.model_dump()!=get_writing_revision(session,document,base_version).body_json:
            raise WritingConflict("请先保存并选择要改写的文案版本")
    profile=session.scalar(select(ModelProfile).where(ModelProfile.slug=="qwen-local"))
    if not writer_is_ready(profile):
        raise ValueError("本地写作模型尚未准备好；请先完成模型安装与写作验证，可继续保存自己的文案")
    task=Task(project_id=document.project_id,created_by_id=actor.id,model_profile_id=profile.id,
        task_type="write_script",priority=1,idempotency_key=key,
        model_version_snapshot=profile.model_version,quantization_snapshot=profile.quantization,
        model_config_fingerprint=model_config_fingerprint(profile),runtime_config_snapshot=profile.runtime_config_json,
        payload_json={"document_id":document.id,"request":request_json,"request_sha256":digest,
            "base_version":base_version,"template_version":TEMPLATE_VERSION})
    session.add(task);session.flush()
    session.add(AuditLog(actor_id=actor.id,action="writing.enqueue",entity_type="task",entity_id=task.id,
        details_json={"document_id":document.id,"operation":request.operation,"base_version":base_version}))
    return task


def store_writing_candidate(session: Session, task: Task, body: WritingBody, *, lease_token: str) -> WritingRevision:
    session.refresh(task)
    expires=task.lease_expires_at
    if expires and expires.tzinfo is None:
        expires=expires.replace(tzinfo=timezone.utc)
    if task.status!="running" or task.lease_token!=lease_token or (expires and expires<datetime.now(timezone.utc)):
        raise WritingConflict("写作任务租约已失效，不能写回结果")
    document=session.get(WritingDocument,task.payload_json["document_id"])
    if document is None or task.project_id!=document.project_id or task.created_by_id!=document.created_by_id:
        raise ValueError("写作任务与作品不匹配")
    _lock(session,document)
    existing=session.scalar(select(WritingRevision).where(WritingRevision.task_id==task.id))
    if existing:
        return existing
    return _append(session,document,task.created_by_id,body,task_id=task.id,
        kind="generated" if task.payload_json["request"]["operation"]=="generate" else "rewrite",
        parent_version=task.payload_json.get("base_version"))


def select_writing_revision(session: Session, actor: User, document: WritingDocument, version: int,
                            *, expected_latest: int) -> WritingRevision:
    assert_writing_owner(actor,document);_lock(session,document);_version_check(document,expected_latest)
    revision=get_writing_revision(session,document,version)
    document.selected_version=version
    session.add(AuditLog(actor_id=actor.id,action="writing.select",entity_type="writing",entity_id=document.id,
        details_json={"version":version}))
    return revision


def publish_writing_revision(session: Session, actor: User, document: WritingDocument, version: int) -> CreationPlan:
    assert_writing_owner(actor,document);_lock(session,document)
    revision=get_writing_revision(session,document,version)
    existing=session.scalar(select(WritingPublication).where(WritingPublication.revision_id==revision.id))
    if existing:
        return session.get(CreationPlan,existing.creation_plan_id)
    body=WritingBody.model_validate(revision.body_json);body.validate_for_target(document.target_seconds)
    next_version=(session.scalar(select(func.max(Storyboard.version)).where(Storyboard.project_id==document.project_id)) or 0)+1
    board=Storyboard(project_id=document.project_id,version=next_version,created_by_id=actor.id,status="draft")
    for i,part in enumerate(body.shots,1):
        scene=part.scene_type
        if document.recipe in {"product","talking"} or any(w in part.visual for w in ("界面","按钮","录屏","商标","包装","价格")):
            scene="product_ui"
        board.shots.append(Shot(sequence_no=i,title=part.title,prompt=part.visual,scene_type=scene,
            duration_seconds=5,aspect_ratio=document.aspect_ratio,status="draft"))
    session.add(board);session.flush()
    plan=CreationPlan(project_id=document.project_id,storyboard=board,created_by_id=actor.id,
        recipe=document.recipe,target_seconds=document.target_seconds,aspect_ratio=document.aspect_ratio)
    session.add(plan);session.flush()
    session.add(WritingPublication(revision_id=revision.id,creation_plan_id=plan.id,confirmed_by_id=actor.id))
    session.add(AuditLog(actor_id=actor.id,action="writing.publish",entity_type="creation",entity_id=plan.id,
        details_json={"document_id":document.id,"revision_id":revision.id,"version":version}))
    return plan
