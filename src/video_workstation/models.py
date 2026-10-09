from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


project_members = Table(
    "project_members",
    Base.metadata,
    Column("project_id", String(32), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owned_projects: Mapped[list["Project"]] = relationship(back_populates="creator", foreign_keys="Project.created_by_id")
    projects: Mapped[list["Project"]] = relationship(secondary=project_members, back_populates="members")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    source_script: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    creator: Mapped[User] = relationship(back_populates="owned_projects", foreign_keys=[created_by_id])
    members: Mapped[list[User]] = relationship(secondary=project_members, back_populates="projects")
    storyboards: Mapped[list["Storyboard"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Storyboard(Base):
    __tablename__ = "storyboards"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_storyboard_project_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="storyboards")
    shots: Mapped[list["Shot"]] = relationship(back_populates="storyboard", cascade="all, delete-orphan")


class Shot(Base):
    __tablename__ = "shots"
    __table_args__ = (UniqueConstraint("storyboard_id", "sequence_no", name="uq_shot_storyboard_sequence"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    storyboard_id: Mapped[str] = mapped_column(ForeignKey("storyboards.id", ondelete="CASCADE"), index=True)
    sequence_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    scene_type: Mapped[str] = mapped_column(String(40), default="broll", index=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=5)
    aspect_ratio: Mapped[str] = mapped_column(String(10), default="16:9")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    storyboard: Mapped[Storyboard] = relationship(back_populates="shots")


class ModelProfile(Base):
    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(20), default="local")
    adapter_type: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_heavy: Mapped[bool] = mapped_column(Boolean, default=True)
    model_version: Mapped[str] = mapped_column(String(120), default="unconfigured")
    quantization: Mapped[str] = mapped_column(String(80), default="unconfigured")
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validated_presets_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    runtime_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    license_name: Mapped[str] = mapped_column(String(160), default="")
    license_version: Mapped[str] = mapped_column(String(80), default="")
    license_url: Mapped[str] = mapped_column(String(500), default="")
    notice: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, index=True)
    model_profile_id: Mapped[str | None] = mapped_column(ForeignKey("model_profiles.id"), nullable=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    task_type: Mapped[str] = mapped_column(String(40), default="generate")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=2, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    leased_by: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), default=new_id, unique=True)
    model_version_snapshot: Mapped[str] = mapped_column(String(120), default="")
    quantization_snapshot: Mapped[str] = mapped_column(String(80), default="")
    model_config_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    runtime_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_temp_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerStatus(Base):
    __tablename__ = "worker_statuses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(20), default="starting", index=True)
    current_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id", ondelete="SET NULL"), nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id", ondelete="SET NULL"), nullable=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str] = mapped_column(String(80), index=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
