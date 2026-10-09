from __future__ import annotations

import secrets
import hashlib
import json
import mimetypes
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .db import Database
from .models import Asset, AuditLog, ModelProfile, Project, Review, Shot, Storyboard, Task, User
from .offline import validate_offline_profile
from .queue import QueueService
from .security import PasswordService
from .services.models import (
    ModelNotAdmitted,
    admit_generation,
    matching_profiles_for_shot,
    model_config_fingerprint,
    seed_model_profiles,
    validate_model_for_shot,
    model_readiness,
    video2x_is_admitted,
)
from .services.projects import (
    PermissionDenied,
    SHOT_ASPECT_RATIOS,
    SHOT_DURATION_PRESETS,
    SHOT_SCENE_TYPES,
    approve_storyboard,
    assert_project_access,
    bulk_update_draft_shots,
    create_member,
    create_project,
    bootstrap_admin,
    require_admin,
    submit_storyboard,
    update_draft_shot,
    validate_priority,
    withdraw_storyboard,
)
from .storage import InsufficientStorage, StorageGuard, generated_asset_path
from .services.system_status import collect_system_status
from .services.assets import store_uploaded_video
from .media import InvalidRange, iter_file_range, parse_range_header, resolve_asset_path
from .subtitles import store_subtitle_asset
from .services.postproduction import create_compose_task


PACKAGE_DIR = Path(__file__).parent


def status_context(request: Request) -> dict:
    return {"system_summary": getattr(request.app.state, "system_status_summary", {"level": "unavailable", "label": "状态未检查"})}


templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"), context_processors=[status_context])


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


class ShotUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=20_000)
    negative_prompt: str = Field(default="", max_length=20_000)
    scene_type: str | None = Field(default=None)
    duration_seconds: float = Field(gt=0, le=60)
    aspect_ratio: str = Field(pattern=r"^(16:9|9:16)$")


class MemberCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=200)


class ProjectMemberAdd(BaseModel):
    username: str = Field(min_length=3, max_length=80)


class TaskPriorityUpdate(BaseModel):
    priority: int = Field(ge=0, le=2)


class ModelToggle(BaseModel):
    enabled: bool


class AcceptanceRequest(BaseModel):
    notes: str = Field(default="", max_length=10_000)


class ComposeRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    subtitle_asset_id: str | None = None
    aspect_ratio: str = Field(pattern=r"^(16:9|9:16)$")
    priority: int = Field(default=1, ge=0, le=2)
    upscale: bool = False


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
                "negative_prompt": shot.negative_prompt,
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
    database.migrate()
    with database.session() as session:
        seed_model_profiles(session)

    app = FastAPI(title="AI 视频自动化工作站", version="0.1.0", docs_url="/api/docs")
    app.state.settings = settings
    app.state.database = database
    app.state.system_status_summary = {"level": "unavailable", "label": "状态未检查"}
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
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            validate_csrf(request)
        return user

    def validate_csrf(request: Request, submitted: str | None = None) -> None:
        expected = request.session.get("csrf")
        supplied = submitted or request.headers.get("X-CSRF-Token")
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "跨源请求被拒绝")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            raise HTTPException(403, "CSRF 校验失败")

    def html_user(request: Request, session: Session) -> User | None:
        user_id = request.session.get("user_id")
        user = session.get(User, user_id) if user_id else None
        return user if user and user.is_active else None

    def require_loopback(request: Request) -> None:
        host = request.client.host if request.client else ""
        try:
            is_loopback = ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise HTTPException(403, "仅允许在工作站本机初始化")

    def admin_exists(session: Session) -> bool:
        return session.scalar(select(User.id).where(User.role == "admin")) is not None

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

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, session: Session = Depends(session_dependency)):
        require_loopback(request)
        if admin_exists(session):
            return RedirectResponse("/login", status_code=303)
        request.session["csrf"] = secrets.token_urlsafe(24)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": None, "csrf_token": request.session["csrf"], "username": "admin"},
        )

    @app.post("/setup", response_class=HTMLResponse)
    def setup_admin(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        require_loopback(request)
        if admin_exists(session):
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)

        error = "两次输入的密码不一致" if password != password_confirm else None
        admin = None
        if error is None:
            try:
                admin = bootstrap_admin(session, username, password)
            except ValueError as exc:
                error = str(exc)
        if error is not None:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {"error": error, "csrf_token": csrf_token, "username": username.strip()},
                status_code=400,
            )

        request.session.clear()
        request.session["user_id"] = admin.id
        request.session["csrf"] = secrets.token_urlsafe(24)
        response = RedirectResponse("/", status_code=303)
        response.headers["X-CSRF-Token"] = request.session["csrf"]
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, session: Session = Depends(session_dependency)):
        request.session["csrf"] = secrets.token_urlsafe(24)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None, "csrf_token": request.session["csrf"], "needs_setup": not admin_exists(session)},
        )

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        validate_csrf(request, csrf_token)
        user = session.scalar(select(User).where(User.username == username.strip()))
        if user is None or not user.is_active or not PasswordService().verify(user.password_hash, password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "用户名或密码错误", "csrf_token": csrf_token},
                status_code=401,
            )
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["csrf"] = secrets.token_urlsafe(24)
        response = RedirectResponse("/", status_code=303)
        response.headers["X-CSRF-Token"] = request.session["csrf"]
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str | None = Form(None)):
        validate_csrf(request, csrf_token)
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
            {"user": user, "projects": projects, "tasks": tasks, "counts": counts, "page": "dashboard", "csrf_token": request.session["csrf"]},
        )

    @app.post("/projects")
    def create_project_form(
        request: Request,
        name: str = Form(...),
        source_script: str = Form(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
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
        available_profiles_by_shot = {
            shot.id: matching_profiles_for_shot(shot, profiles)
            for shot in storyboard.shots
        }
        tasks = list(session.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.created_at.desc())))
        tasks_by_id = {task.id: task for task in tasks}
        profiles_by_id = {profile.id: profile for profile in profiles}
        asset_views_by_shot: dict[str, list[dict]] = {}
        video_assets = list(
            session.scalars(
                select(Asset)
                .where(Asset.project_id == project.id, Asset.kind == "video")
                .order_by(Asset.created_at.desc())
            )
        )
        subtitle_assets = list(
            session.scalars(
                select(Asset)
                .where(Asset.project_id == project.id, Asset.kind == "subtitle")
                .order_by(Asset.created_at.desc())
            )
        )
        delivery_assets = list(
            session.scalars(
                select(Asset)
                .where(Asset.project_id == project.id, Asset.kind.in_(("composite", "upscaled")))
                .order_by(Asset.created_at.desc())
            )
        )
        delivery_views: list[dict] = []
        for asset in delivery_assets:
            qc = asset.metadata_json.get("qc", {})
            try:
                resolve_asset_path(asset, settings)
                available = True
            except ValueError:
                available = False
            delivery_views.append({
                "id": asset.id,
                "kind": asset.kind,
                "label": "Video2X 超分版本" if asset.kind == "upscaled" else "FFmpeg 合成成片",
                "available": available,
                "duration_seconds": qc.get("duration_seconds"),
                "width": qc.get("width"),
                "height": qc.get("height"),
                "created_at": asset.created_at,
            })
        for asset in video_assets:
            if asset.shot_id is None:
                continue
            task = tasks_by_id.get(asset.task_id) if asset.task_id else None
            profile = profiles_by_id.get(task.model_profile_id) if task and task.model_profile_id else None
            qc = asset.metadata_json.get("qc", {})
            try:
                resolve_asset_path(asset, settings)
                available = True
            except ValueError:
                available = False
            asset_views_by_shot.setdefault(asset.shot_id, []).append(
                {
                    "id": asset.id,
                    "available": available,
                    "source_label": "真实录屏" if asset.metadata_json.get("source") == "upload" else (profile.display_name if profile else "本地模型"),
                    "duration_seconds": qc.get("duration_seconds"),
                    "width": qc.get("width"),
                    "height": qc.get("height"),
                    "model_version": task.model_version_snapshot if task else "",
                    "quantization": task.quantization_snapshot if task else "",
                    "seed": task.payload_json.get("seed") if task else None,
                    "created_at": asset.created_at,
                    "qc_passed": bool(qc.get("passed")),
                }
            )
        compose_asset_views_by_shot = {
            shot_id: [view for view in views if view["available"] and view["qc_passed"]]
            for shot_id, views in asset_views_by_shot.items()
        }
        approved_shots = [shot for shot in storyboard.shots if shot.status == "approved"]
        compose_missing_shots = [shot for shot in approved_shots if not compose_asset_views_by_shot.get(shot.id)]
        video2x_profile = next((profile for profile in profiles if profile.slug == "video2x"), None)
        video2x_admitted = bool(video2x_profile and video2x_is_admitted(video2x_profile, scale=2))
        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "user": user,
                "project": project,
                "storyboard": storyboard,
                "profiles": profiles,
                "available_profiles_by_shot": available_profiles_by_shot,
                "duration_presets": SHOT_DURATION_PRESETS,
                "aspect_ratios": SHOT_ASPECT_RATIOS,
                "scene_types": SHOT_SCENE_TYPES,
                "tasks": tasks,
                "asset_views_by_shot": asset_views_by_shot,
                "subtitle_assets": subtitle_assets,
                "compose_asset_views_by_shot": compose_asset_views_by_shot,
                "compose_missing_shots": compose_missing_shots,
                "compose_default_aspect_ratio": approved_shots[0].aspect_ratio if approved_shots else "16:9",
                "video2x_admitted": video2x_admitted,
                "delivery_views": delivery_views,
                "page": "projects",
                "csrf_token": request.session["csrf"],
            },
        )

    @app.post("/shots/{shot_id}/settings")
    def update_shot_settings_form(
        shot_id: str,
        request: Request,
        duration_seconds: float = Form(...),
        aspect_ratio: str = Form(...),
        title: str | None = Form(None),
        prompt: str | None = Form(None),
        negative_prompt: str | None = Form(None),
        scene_type: str | None = Form(None),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "镜头不存在")
        try:
            update_draft_shot(
                session,
                user,
                shot,
                title=title if title is not None else shot.title,
                prompt=prompt if prompt is not None else shot.prompt,
                duration_seconds=duration_seconds,
                aspect_ratio=aspect_ratio,
                negative_prompt=negative_prompt,
                scene_type=scene_type,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/projects/{shot.storyboard.project_id}", status_code=303)

    @app.post("/projects/{project_id}/subtitles")
    async def upload_project_subtitle(
        project_id: str,
        request: Request,
        subtitle: UploadFile = File(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        require_admin(user)
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        content = await subtitle.read(2 * 1024 * 1024 + 1)
        try:
            store_subtitle_asset(session, project, user, subtitle.filename or "", content, settings)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @app.post("/storyboards/{storyboard_id}/shot-settings")
    def update_storyboard_shot_settings_form(
        storyboard_id: str,
        request: Request,
        duration_seconds: float = Form(...),
        aspect_ratio: str = Form(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        try:
            bulk_update_draft_shots(
                session,
                user,
                storyboard,
                duration_seconds=duration_seconds,
                aspect_ratio=aspect_ratio,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/projects/{storyboard.project_id}", status_code=303)

    @app.post("/shots/{shot_id}/assets/video")
    def upload_shot_video(
        shot_id: str,
        request: Request,
        video: UploadFile = File(...),
        csrf_token: str = Form(...),
        session: Session = Depends(session_dependency),
    ):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "镜头不存在")
        if shot.storyboard.status != "approved":
            raise HTTPException(409, "分镜审批后才能导入真实素材")
        try:
            store_uploaded_video(session, user, shot, video.filename or "upload.mp4", video.file, settings)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            video.file.close()
        return RedirectResponse(f"/projects/{shot.storyboard.project_id}", status_code=303)

    @app.get("/assets/{asset_id}/content")
    def asset_content(
        asset_id: str,
        request: Request,
        download: bool = False,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(404, "素材不存在")
        project = session.get(Project, asset.project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        assert_project_access(user, project)
        try:
            path = resolve_asset_path(asset, settings)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

        file_size = path.stat().st_size
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            byte_range = parse_range_header(request.headers.get("Range"), file_size)
        except InvalidRange as exc:
            raise HTTPException(
                status_code=416,
                detail=str(exc),
                headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
            ) from exc

        if byte_range is None:
            response = FileResponse(
                path,
                media_type=media_type,
                filename=path.name if download else None,
                content_disposition_type="attachment" if download else "inline",
            )
            response.headers["Accept-Ranges"] = "bytes"
            return response

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {byte_range.start}-{byte_range.end}/{file_size}",
            "Content-Length": str(byte_range.length),
        }
        if download:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name)}"
        return StreamingResponse(
            iter_file_range(path, byte_range),
            status_code=206,
            media_type=media_type,
            headers=headers,
        )

    @app.post("/api/projects/{project_id}/compose", status_code=201)
    def api_compose_project(
        project_id: str,
        payload: ComposeRequest,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        try:
            task = create_compose_task(
                session,
                user,
                project,
                asset_ids=payload.asset_ids,
                subtitle_asset_id=payload.subtitle_asset_id,
                aspect_ratio=payload.aspect_ratio,
                priority=payload.priority,
                settings=settings,
                upscale=payload.upscale,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": task.id, "status": task.status, "task_type": task.task_type}

    @app.post("/storyboards/{storyboard_id}/submit")
    def submit_form(storyboard_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        submit_storyboard(session, user, storyboard)
        return RedirectResponse(f"/projects/{storyboard.project_id}", status_code=303)

    @app.post("/storyboards/{storyboard_id}/approve")
    def approve_form(storyboard_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        approve_storyboard(session, user, storyboard)
        return RedirectResponse(f"/projects/{storyboard.project_id}", status_code=303)

    @app.post("/storyboards/{storyboard_id}/withdraw")
    def withdraw_form(storyboard_id: str, request: Request, csrf_token: str = Form(...), session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        validate_csrf(request, csrf_token)
        storyboard = session.get(Storyboard, storyboard_id)
        if storyboard is None:
            raise HTTPException(404, "分镜不存在")
        try:
            withdraw_storyboard(session, user, storyboard)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
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
        return templates.TemplateResponse(request, "queue.html", {"user": user, "tasks": tasks, "page": "queue", "csrf_token": request.session["csrf"]})

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        require_admin(user)
        profiles = list(session.scalars(select(ModelProfile).order_by(ModelProfile.display_name)))
        readiness_by_slug = {profile.slug: model_readiness(profile) for profile in profiles}
        return templates.TemplateResponse(request, "models.html", {"user": user, "profiles": profiles, "readiness_by_slug": readiness_by_slug, "page": "models", "csrf_token": request.session["csrf"]})

    def refresh_system_status(request: Request, session: Session):
        system_status = collect_system_status(session, settings)
        severities = {alert.severity for alert in system_status.alerts}
        if "critical" in severities:
            summary = {"level": "critical", "label": "需要处理"}
        elif "warning" in severities:
            summary = {"level": "warning", "label": "存在警告"}
        elif system_status.gpu.status == "unavailable":
            summary = {"level": "unavailable", "label": "GPU 状态不可用"}
        else:
            summary = {"level": "ok", "label": "运行正常"}
        request.app.state.system_status_summary = summary
        return system_status

    @app.get("/system", response_class=HTMLResponse)
    def system_page(request: Request, session: Session = Depends(session_dependency)):
        user = html_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        require_admin(user)
        system_status = refresh_system_status(request, session)
        return templates.TemplateResponse(
            request,
            "system.html",
            {"user": user, "status": system_status, "page": "system", "csrf_token": request.session["csrf"]},
        )

    @app.get("/api/system/status")
    def api_system_status(
        request: Request,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        require_admin(user)
        return refresh_system_status(request, session).to_dict()

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
        if (
            float(payload.duration_seconds) != float(shot.duration_seconds)
            or payload.aspect_ratio != shot.aspect_ratio
        ):
            raise HTTPException(409, "入队参数必须与已审批镜头一致")
        priority = validate_priority(user, payload.priority)
        profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == payload.model_slug))
        if profile is None:
            raise HTTPException(404, "模型 Profile 不存在")
        validate_model_for_shot(shot, profile)
        preset = admit_generation(
            profile,
            duration_seconds=shot.duration_seconds,
            aspect_ratio=shot.aspect_ratio,
        )
        estimated_temp_bytes = 1 if profile.adapter_type == "demo" else max(20 * 1024**3, int(shot.duration_seconds * 4 * 1024**3))
        StorageGuard(settings.asset_dir, minimum_free_bytes=settings.minimum_free_bytes).ensure_capacity(
            estimated_temp_bytes=estimated_temp_bytes
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
            model_version_snapshot=profile.model_version,
            quantization_snapshot=profile.quantization,
            model_config_fingerprint=model_config_fingerprint(profile),
            runtime_config_snapshot=profile.runtime_config_json,
            estimated_temp_bytes=estimated_temp_bytes,
        )
        session.add(task)
        session.flush()
        suffix = ".json" if profile.adapter_type == "demo" else ".mp4"
        output_path = generated_asset_path(settings.asset_dir, project.id, task.id, suffix)
        task.payload_json = {
            "prompt": shot.prompt,
            "negative_prompt": shot.negative_prompt,
            "output_path": str(output_path),
            "duration_seconds": shot.duration_seconds,
            "aspect_ratio": shot.aspect_ratio,
            "seed": payload.seed,
            "minimum_free_bytes": settings.minimum_free_bytes,
            "validated_preset": preset,
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

    @app.patch("/api/shots/{shot_id}")
    def api_update_shot(
        shot_id: str,
        payload: ShotUpdate,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "镜头不存在")
        try:
            update_draft_shot(
                session,
                user,
                shot,
                title=payload.title,
                prompt=payload.prompt,
                duration_seconds=payload.duration_seconds,
                aspect_ratio=payload.aspect_ratio,
                negative_prompt=payload.negative_prompt,
                scene_type=payload.scene_type,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": shot.id,
            "title": shot.title,
            "prompt": shot.prompt,
            "negative_prompt": shot.negative_prompt,
            "scene_type": shot.scene_type,
            "duration_seconds": shot.duration_seconds,
            "aspect_ratio": shot.aspect_ratio,
        }

    @app.post("/api/admin/users", status_code=201)
    def api_create_member(
        payload: MemberCreate,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        try:
            member = create_member(session, user, payload.username, payload.password)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": member.id, "username": member.username, "role": member.role}

    @app.post("/api/projects/{project_id}/members")
    def api_add_project_member(
        project_id: str,
        payload: ProjectMemberAdd,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        require_admin(user)
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        member = session.scalar(select(User).where(User.username == payload.username.strip(), User.is_active.is_(True)))
        if member is None:
            raise HTTPException(404, "成员不存在或已停用")
        if member not in project.members:
            project.members.append(member)
            session.add(
                AuditLog(
                    actor_id=user.id,
                    action="project.member_add",
                    entity_type="project",
                    entity_id=project.id,
                    details_json={"user_id": member.id},
                )
            )
        return {"project_id": project.id, "username": member.username}

    def load_task_with_access(task_id: str, user: User, session: Session) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        project = session.get(Project, task.project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        assert_project_access(user, project)
        return task

    @app.post("/api/tasks/{task_id}/cancel")
    def api_cancel_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        task = load_task_with_access(task_id, user, session)
        try:
            QueueService(session).cancel(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.add(AuditLog(actor_id=user.id, action="task.cancel", entity_type="task", entity_id=task.id))
        return {"id": task.id, "status": task.status}

    @app.post("/api/tasks/{task_id}/pause")
    def api_pause_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        task = load_task_with_access(task_id, user, session)
        try:
            QueueService(session).pause(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.add(AuditLog(actor_id=user.id, action="task.pause", entity_type="task", entity_id=task.id))
        return {"id": task.id, "status": task.status}

    @app.post("/api/tasks/{task_id}/resume")
    def api_resume_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        task = load_task_with_access(task_id, user, session)
        try:
            QueueService(session).resume(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        session.add(AuditLog(actor_id=user.id, action="task.resume", entity_type="task", entity_id=task.id))
        return {"id": task.id, "status": task.status}

    @app.post("/api/tasks/{task_id}/retry")
    def api_retry_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(session_dependency)):
        require_admin(user)
        task = load_task_with_access(task_id, user, session)
        if task.status not in {"cancelled", "terminal_failed"}:
            raise HTTPException(409, "只有已取消或终止失败的任务可以人工重试")
        task.status = "queued"
        task.attempts = 0
        task.error_class = None
        task.error_message = None
        task.started_at = None
        task.finished_at = None
        session.add(AuditLog(actor_id=user.id, action="task.retry", entity_type="task", entity_id=task.id))
        return {"id": task.id, "status": task.status}

    @app.patch("/api/tasks/{task_id}/priority")
    def api_update_task_priority(
        task_id: str,
        payload: TaskPriorityUpdate,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        task = load_task_with_access(task_id, user, session)
        task.priority = validate_priority(user, payload.priority)
        session.add(
            AuditLog(
                actor_id=user.id,
                action="task.priority_update",
                entity_type="task",
                entity_id=task.id,
                details_json={"priority": task.priority},
            )
        )
        return {"id": task.id, "priority": task.priority}

    @app.patch("/api/models/{model_slug}")
    def api_toggle_model(
        model_slug: str,
        payload: ModelToggle,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        require_admin(user)
        profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == model_slug))
        if profile is None:
            raise HTTPException(404, "模型 Profile 不存在")
        if payload.enabled:
            validate_offline_profile(profile)
        profile.enabled = payload.enabled
        session.add(
            AuditLog(
                actor_id=user.id,
                action="model.toggle",
                entity_type="model_profile",
                entity_id=profile.id,
                details_json={"enabled": profile.enabled},
            )
        )
        return {"slug": profile.slug, "enabled": profile.enabled}

    @app.post("/api/projects/{project_id}/accept")
    def api_accept_project(
        project_id: str,
        payload: AcceptanceRequest,
        user: User = Depends(current_user),
        session: Session = Depends(session_dependency),
    ):
        require_admin(user)
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        tasks = list(session.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.created_at)))
        if not tasks or any(task.status != "succeeded" for task in tasks):
            raise HTTPException(409, "所有已创建任务成功后才能验收归档")
        profiles = {
            profile.id: profile
            for profile in session.scalars(
                select(ModelProfile).where(ModelProfile.id.in_({task.model_profile_id for task in tasks if task.model_profile_id}))
            )
        }
        manifest = {
            "schema_version": 1,
            "project": {"id": project.id, "name": project.name, "status": "accepted"},
            "storyboards": [
                {
                    "id": storyboard.id,
                    "version": storyboard.version,
                    "status": storyboard.status,
                    "shots": [
                        {
                            "id": shot.id,
                            "sequence_no": shot.sequence_no,
                            "prompt": shot.prompt,
                            "scene_type": shot.scene_type,
                            "duration_seconds": shot.duration_seconds,
                            "aspect_ratio": shot.aspect_ratio,
                        }
                        for shot in sorted(storyboard.shots, key=lambda item: item.sequence_no)
                    ],
                }
                for storyboard in sorted(project.storyboards, key=lambda item: item.version)
            ],
            "tasks": [
                {
                    "id": task.id,
                    "shot_id": task.shot_id,
                    "model": profiles.get(task.model_profile_id).slug if profiles.get(task.model_profile_id) else None,
                    "model_version": task.model_version_snapshot,
                    "quantization": task.quantization_snapshot,
                    "config_fingerprint": task.model_config_fingerprint,
                    "duration_seconds": task.payload_json.get("duration_seconds"),
                    "aspect_ratio": task.payload_json.get("aspect_ratio"),
                    "seed": task.payload_json.get("seed"),
                    "result": task.result_json,
                }
                for task in tasks
            ],
            "review": {"reviewer_id": user.id, "status": "accepted", "notes": payload.notes},
        }
        archive_folder = settings.archive_dir / project.id
        archive_folder.mkdir(parents=True, exist_ok=True)
        manifest_path = archive_folder / "manifest.json"
        temporary_path = archive_folder / ".manifest.json.tmp"
        encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        temporary_path.write_bytes(encoded)
        temporary_path.replace(manifest_path)
        digest = hashlib.sha256(encoded).hexdigest()
        project.status = "accepted"
        review = Review(project_id=project.id, reviewer_id=user.id, status="accepted", notes=payload.notes)
        asset = Asset(
            project_id=project.id,
            kind="archive_manifest",
            path=str(manifest_path),
            sha256=digest,
            metadata_json={"schema_version": 1},
        )
        session.add_all([review, asset])
        session.add(
            AuditLog(
                actor_id=user.id,
                action="project.accept",
                entity_type="project",
                entity_id=project.id,
                details_json={"manifest_sha256": digest},
            )
        )
        return {"project_id": project.id, "status": project.status, "manifest_path": str(manifest_path), "sha256": digest}

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
