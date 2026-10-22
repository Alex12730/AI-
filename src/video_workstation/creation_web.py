"""Beginner-facing routes, kept separate from the legacy management UI."""
from __future__ import annotations

import copy

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Asset, CreationPlan, Shot, Task, ModelProfile, WritingDocument
from .services.assets import store_uploaded_video
from .services.creation import (RECIPES, assert_plan_owner, create_guided_project,
    creation_choices, set_creation_paused, start_creation, valid_bound_asset)
from .services.projects import update_draft_shot
from .storage import InsufficientStorage


STATE_LABELS = {"draft": "确认分镜", "running": "正在制作", "pause_requested": "当前段结束后暂停",
    "paused": "后续制作已暂停", "blocked": "需要处理", "failed": "制作遇到问题", "preview_ready": "预览和保存"}
TASK_LABELS = {"queued": "等待制作", "running": "正在制作", "succeeded": "已完成",
    "terminal_failed": "需要处理", "paused": "已暂停", "cancelled": "已停止"}


def lock_creation(session: Session, plan: CreationPlan):
    """All draft writes share the confirmation's database write lock."""
    session.execute(update(CreationPlan).where(CreationPlan.id == plan.id).values(status=CreationPlan.status))
    session.refresh(plan)


def register_creation_routes(app, *, settings, templates, session_dependency, html_user, validate_csrf):
    def owned(session, actor, plan_id):
        plan = session.get(CreationPlan, plan_id)
        if plan is None:
            raise HTTPException(404, "作品不存在")
        assert_plan_owner(actor, plan)
        return plan

    def page(request, session, actor, plan, *, error="", status_code=200):
        tasks = list(session.scalars(select(Task).where(Task.project_id == plan.project_id).order_by(Task.created_at)))
        compose_id = plan.snapshot_json.get("compose_task_id")
        delivery = session.scalar(select(Asset).where(Asset.task_id == compose_id, Asset.kind == "composite")) if compose_id else None
        return templates.TemplateResponse(request, "creation.html", {
            "user": actor, "plan": plan, "recipe": RECIPES[plan.recipe],
            "choices": creation_choices(session, plan, settings) if plan.status == "draft" else [],
            "nodes": plan.snapshot_json.get("nodes", []), "tasks_by_id": {t.id: t for t in tasks},
            "state_label": STATE_LABELS.get(plan.status, plan.status), "task_labels": TASK_LABELS,
            "delivery": delivery, "error": error, "page": "create", "csrf_token": request.session["csrf"],
        }, status_code=status_code)

    @app.get("/create")
    def create_page(request: Request, session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        from .services.writing import writer_is_ready
        profile=session.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
        return templates.TemplateResponse(request, "create.html", {"user": actor, "recipes": RECIPES,"writer_ready":writer_is_ready(profile),
            "page": "create", "csrf_token": request.session["csrf"], "values": {}, "error": ""})

    @app.post("/create")
    def create_form(request: Request, name: str = Form(...), script: str = Form(...), recipe: str = Form(...),
                    target_seconds: int = Form(...), aspect_ratio: str = Form(...), csrf_token: str = Form(...),
                    session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        validate_csrf(request, csrf_token)
        try:
            plan = create_guided_project(session, actor, name=name, script=script, recipe=recipe,
                target_seconds=target_seconds, aspect_ratio=aspect_ratio)
            session.commit()
        except ValueError as exc:
            session.rollback()
            return templates.TemplateResponse(request, "create.html", {"user": actor, "recipes": RECIPES,
                "page": "create", "csrf_token": request.session["csrf"], "error": str(exc),
                "values": {"name": name, "script": script, "recipe": recipe,
                    "target_seconds": target_seconds, "aspect_ratio": aspect_ratio}}, status_code=422)
        return RedirectResponse(f"/creations/{plan.id}", 303)

    @app.get("/creations/{plan_id}")
    def creation_page(plan_id: str, request: Request, session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        return page(request, session, actor, owned(session, actor, plan_id))

    @app.post("/creations/{plan_id}/start")
    def start_form(plan_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        validate_csrf(request, csrf_token)
        plan = owned(session, actor, plan_id)
        try:
            start_creation(session, actor, plan, settings)
            session.commit()
        except (ValueError, InsufficientStorage, OSError) as exc:
            session.rollback()
            return page(request, session, actor, plan, error=str(exc), status_code=409)
        return RedirectResponse(f"/creations/{plan.id}", 303)

    @app.post("/creations/{plan_id}/pause")
    def pause_form(plan_id: str, request: Request, csrf_token: str = Form(...), resume: bool = Form(False),
                   session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        validate_csrf(request, csrf_token)
        plan = owned(session, actor, plan_id)
        try:
            set_creation_paused(session, actor, plan, not resume)
            session.commit()
        except ValueError as exc:
            session.rollback()
            return page(request, session, actor, plan, error=str(exc), status_code=409)
        return RedirectResponse(f"/creations/{plan.id}", 303)

    @app.post("/creations/{plan_id}/shots/{shot_id}/video")
    def upload_form(plan_id: str, shot_id: str, request: Request, csrf_token: str = Form(...),
                    video: UploadFile = File(...), session: Session = Depends(session_dependency)):
        try:
            actor = html_user(request, session)
            if actor is None:
                return RedirectResponse("/login", 303)
            validate_csrf(request, csrf_token)
            plan = owned(session, actor, plan_id)
            lock_creation(session, plan)
            shot = session.get(Shot, shot_id)
            if plan.status != "draft" or shot is None or shot.storyboard_id != plan.storyboard_id:
                raise HTTPException(409, "只能给当前作品的草稿镜头添加素材")
            try:
                asset = store_uploaded_video(session, actor, shot, video.filename or "upload.mp4", video.file, settings)
                snap = copy.deepcopy(plan.snapshot_json)
                snap.setdefault("bindings", {})[shot.id] = asset.id
                plan.snapshot_json = snap
                session.commit()
            except (ValueError, InsufficientStorage, OSError) as exc:
                session.rollback()
                return page(request, session, actor, plan, error=str(exc), status_code=409)
            return RedirectResponse(f"/creations/{plan.id}#shot-{shot.id}", 303)
        finally:
            video.file.close()

    @app.post("/creations/{plan_id}/shots/{shot_id}/edit")
    def edit_form(plan_id: str, shot_id: str, request: Request, csrf_token: str = Form(...),
                  title: str = Form(...), prompt: str = Form(...),
                  session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        validate_csrf(request, csrf_token)
        plan = owned(session, actor, plan_id)
        lock_creation(session, plan)
        shot = session.get(Shot, shot_id)
        if plan.status != "draft" or shot is None or shot.storyboard_id != plan.storyboard_id:
            raise HTTPException(409, "只能编辑当前作品的草稿分镜")
        try:
            if len(title) > 200 or len(prompt) > 20_000:
                raise ValueError("标题请控制在 200 字内，镜头内容请控制在 20000 字内")
            scene = shot.scene_type
            if any(word in prompt for word in ("界面", "按钮", "录屏", "商标", "包装", "价格")):
                scene = "product_ui"
            update_draft_shot(session, actor, shot, title=title, prompt=prompt,
                duration_seconds=shot.duration_seconds, aspect_ratio=shot.aspect_ratio, scene_type=scene)
            session.commit()
        except ValueError as exc:
            session.rollback()
            return page(request, session, actor, plan, error=str(exc), status_code=409)
        return RedirectResponse(f"/creations/{plan.id}#shot-{shot.id}", 303)

    @app.post("/creations/{plan_id}/shots/{shot_id}/bind")
    def bind_form(plan_id: str, shot_id: str, request: Request, csrf_token: str = Form(...),
                  asset_id: str = Form(""), session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        validate_csrf(request, csrf_token)
        plan = owned(session, actor, plan_id)
        lock_creation(session, plan)
        shot = session.get(Shot, shot_id)
        if plan.status != "draft" or shot is None or shot.storyboard_id != plan.storyboard_id:
            raise HTTPException(409, "只能修改当前草稿镜头")
        try:
            if asset_id:
                valid_bound_asset(session, plan, shot, asset_id, settings)
            snap = copy.deepcopy(plan.snapshot_json)
            snap.setdefault("bindings", {})[shot.id] = asset_id or None
            plan.snapshot_json = snap
            session.commit()
        except ValueError as exc:
            session.rollback()
            return page(request, session, actor, plan, error=str(exc), status_code=409)
        return RedirectResponse(f"/creations/{plan.id}#shot-{shot.id}", 303)

    @app.get("/works")
    def works_page(request: Request, session: Session = Depends(session_dependency)):
        actor = html_user(request, session)
        if actor is None:
            return RedirectResponse("/login", 303)
        plans = list(session.scalars(select(CreationPlan).where(CreationPlan.created_by_id == actor.id).order_by(CreationPlan.created_at.desc())))
        documents=list(session.scalars(select(WritingDocument).where(WritingDocument.created_by_id==actor.id).order_by(WritingDocument.created_at.desc())))
        groups={d.project_id:{'project':d.project,'document':d,'plans':[]} for d in documents}
        for plan in plans:
            groups.setdefault(plan.project_id,{'project':plan.project,'document':None,'plans':[]})['plans'].append(plan)
        return templates.TemplateResponse(request, "works.html", {"user": actor, "plans": plans,"groups":list(groups.values()),
            "state_labels": STATE_LABELS, "page": "works", "csrf_token": request.session["csrf"]})
