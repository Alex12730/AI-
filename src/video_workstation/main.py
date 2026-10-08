from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .db import Database
from .models import AuditLog, ModelProfile, Project, Shot, Storyboard, Task, User
from .security import PasswordService
from .services.models import ModelNotAdmitted, admit_generation, seed_model_profiles
from .services.projects import (
    PermissionDenied,
    approve_storyboard,
    assert_project_access,
    create_project,
    require_admin,
    submit_storyboard,
    validate_priority,
)
from .storage import InsufficientStorage, StorageGuard, generated_asset_path


PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    source_script: str = Field(min_length=1, max_length=100_000)


class EnqueueRequest(BaseModel):
    model_slug: str
    duration_seconds: float = Field(gt=0, le=60)
    aspect_ratio: str = Field(pattern=r"^(16:9|9:16)$")
    priority: int = Field(ge=0, le=2)
    estimated_temp_bytes: int = Field(gt=0)
    seed: int = Field(ge=0, le=2**32 - 1)


def serialize_project(project: Project) -> dict:
    storyboard = max(project.storyboards, key=lambda item: item.version)
    return {
        "id": project.id,
        "name": project.name,
        "status": project.status,
        "source_script": project.source_script,
        "storyboard_id": storyboard.id,
        "storyboard_status": storyboard.status,
        "shots": [
            {
                "id": shot.id,
                "sequence_no": shot.sequence_no,
                "title": shot.title,
                "prompt": shot.prompt,
                "scene_type": shot.scene_type,
                "duration_seconds": shot.duration_seconds,
                "aspect_ratio": shot.aspect_ratio,
                "status": shot.status,
            }
            for shot in sorted(storyboard.shots, key=lambda item: item.sequence_no)
        ],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if len(settings.session_secret) < 24:
        raise RuntimeError("VIDEO_WORKSTATION_SESSION_SECRET 至少需要 24 个字符")
    database = Database(settings)
    database.create_schema()
    with database.session() as session:
        seed_model_profiles(session)

    app = FastAPI(title="AI 视频自动化工作站", version="0.1.0", docs_url="/api/docs")
    app.state.settings = settings
    app.state.database = database
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=settings.secure_cookies,
        session_cookie="video_workstation_session",
        max_age=8 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    def session_dependency(request: Request):
        with request.app.state.database.session() as session:
            yield session

    def current_user(request: Request, session: Session = Depends(session_dependency)) -> User:
        user_id = request.session.get("user_id")
        user = session.get(User, user_id) if user_id else None
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    def html_user(request: Request, session: Session) -> User | None:
        user_id = request.session.get("user_id")
        user = session.get(User, user_id) if user_id else None
        return user if user and user.is_active else None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(PermissionDenied)
    async def permission_error(_request: Request, exc: PermissionDenied):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ModelNotAdmitted)
    async def admission_error(_request: Request, exc: ModelNotAdmitted):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InsufficientStorage)
    async def storage_error(_request: Request, exc: InsufficientStorage):
        return JSONResponse(status_code=507, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": "local-only"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = session.scalar(select(User).where(User.username == username.strip()))
        if user is None or not user.is_active or not PasswordService().verify(user.password_hash, password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "用户名或密码错误"},
                status_code=401,
            )
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["csrf"] = secrets.token_urlsafe(24)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        projects = list(session.scalars(select(Project).order_by(Project.updated_at.desc())))
        if user.role != "admin":
            projects = [project for project in projects if user.id == project.created_by_id or any(m.id == user.id for m in project.members)]
        tasks = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(20)))
        if user.role != "admin":
            project_ids = {project.id for project in projects}
            tasks = [task for task in tasks if task.project_id in project_ids]
        counts = {state: sum(task.status == state for task in tasks) for state in ("queued", "running", "succeeded", "terminal_failed")}
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"user": user, "projects": projects, "tasks": tasks, "counts": counts, "page": "dashboard"},
        )

    @app.post("/projects")
    def create_project_form(
        request: Request,
        name: str = Form(...),
        source_script: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        project = create_project(session, user, name, source_script)
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(project_id: str, request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        assert_project_access(user, project)
        storyboard = max(project.storyboards, key=lambda item: item.version)
        profiles = list(session.scalars(select(ModelProfile).order_by(ModelProfile.display_name)))
        tasks = list(session.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())))
        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "user": user,
                "project": project,
                "storyboard": storyboard,
                "profiles": profiles,
                "tasks": tasks,
                "page": "projects",
            },
        )

    @app.post("/storyboards/{storyboard_id}/submit")
    def submit_form(storyboard_id: str, request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        submit_storyboard(session, user, storyboard)
        return RedirectResponse(f"/projects/{storyboard.project_id}", status_code=303)

    @app.post("/storyboards/{storyboard_id}/approve")
    def approve_form(storyboard_id: str, request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        approve_storyboard(session, user, storyboard)
        return RedirectResponse(f"/projects/{storyboard.project_id}", status_code=303)

    @app.get("/queue", response_class=HTMLResponse)
    def queue_page(request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        tasks = list(session.scalars(select(Task).order_by(Task.created_at.desc()).limit(100)))
        if user.role != "admin":
            allowed = {project.id for project in session.scalars(select(Project)) if user.id == project.created_by_id or any(m.id == user.id for m in project.members)}
            tasks = [task for task in tasks if task.project_id in allowed]
        return templates.TemplateResponse(request, "queue.html", {"user": user, "tasks": tasks, "page": "queue"})

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        require_admin(user)
        profiles = list(session.scalars(select(ModelProfile).order_by(ModelProfile.display_name)))
        return templates.TemplateResponse(request, "models.html", {"user": user, "profiles": profiles, "page": "models"})

    @app.get("/api/projects")
    def api_projects(user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        projects = list(session.scalars(select(Project).order_by(Project.updated_at.desc())))
        if user.role != "admin":
            projects = [project for project in projects if user.id == project.created_by_id or any(m.id == user.id for m in project.members)]
        return [serialize_project(project) for project in projects]

    @app.post("/api/projects", status_code=201)
    def api_create_project(payload: ProjectCreate, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        return serialize_project(create_project(session, user, payload.name, payload.source_script))

    @app.get("/api/projects/{project_id}")
    def api_project(project_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        assert_project_access(user, project)
        return serialize_project(project)

    @app.post("/api/storyboards/{storyboard_id}/submit")
    def api_submit(storyboard_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        submit_storyboard(session, user, storyboard)
        return {"id": storyboard.id, "status": storyboard.status}

    @app.post("/api/storyboards/{storyboard_id}/approve")
    def api_approve(storyboard_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        approve_storyboard(session, user, storyboard)
        return {"id": storyboard.id, "status": storyboard.status}

    @app.post("/api/shots/{shot_id}/enqueue", status_code=201)
    def api_enqueue(
        shot_id: str,
        payload: EnqueueRequest,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "镜头不存在")
        project = shot.storyboard.project
        assert_project_access(user, project)
        if shot.storyboard.status != "approved" or shot.status != "approved":
            raise HTTPException(409, "分镜尚未审批")
        priority = validate_priority(user, payload.priority)
        profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == payload.model_slug))
        if profile is None:
            raise HTTPException(404, "模型 Profile 不存在")
        admit_generation(profile, duration_seconds=payload.duration_seconds, aspect_ratio=payload.aspect_ratio)
        StorageGuard(settings.asset_dir, minimum_free_bytes=settings.minimum_free_bytes).ensure_capacity(
            estimated_temp_bytes=payload.estimated_temp_bytes
        )
        task = Task(
            project_id=project.id,
            shot_id=shot.id,
            model_profile_id=profile.id,
            created_by_id=user.id,
            task_type="generate",
            status="queued",
            priority=priority,
            payload_json={},
        )
        session.add(task)
        session.flush()
        suffix = ".json" if profile.adapter_type == "demo" else ".mp4"
        output_path = generated_asset_path(settings.asset_dir, project.id, task.id, suffix)
        task.payload_json = {
            "prompt": shot.prompt,
            "negative_prompt": shot.negative_prompt,
            "output_path": str(output_path),
            "duration_seconds": payload.duration_seconds,
            "aspect_ratio": payload.aspect_ratio,
            "seed": payload.seed,
        }
        session.add(
            AuditLog(
                actor_id=user.id,
                action="task.enqueue",
                entity_type="task",
                entity_id=task.id,
                details_json={"model_slug": profile.slug, "shot_id": shot.id},
            )
        )
        return {"id": task.id, "status": task.status, "priority": task.priority, "model": profile.slug}

    @app.get("/api/models")
    def api_models(user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        require_admin(user)
        return [
            {
                "slug": profile.slug,
                "display_name": profile.display_name,
                "enabled": profile.enabled,
                "validated_presets": profile.validated_presets_json,
                "license_name": profile.license_name,
            }
            for profile in session.scalars(select(ModelProfile).order_by(ModelProfile.display_name))
        ]

    return app


def application() -> FastAPI:
    return create_app()
