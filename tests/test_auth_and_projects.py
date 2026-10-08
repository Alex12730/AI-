from __future__ import annotations

import pytest

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import AuditLog, Shot, User
from video_workstation.security import PasswordService
from video_workstation.services.projects import (
    PermissionDenied,
    approve_storyboard,
    assert_project_access,
    bootstrap_admin,
    create_project,
    submit_storyboard,
    validate_priority,
)


@pytest.fixture()
def database(tmp_path):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}"))
    db.create_schema()
    return db


def test_passwords_use_argon2_and_wrong_password_is_rejected():
    service = PasswordService()
    encoded = service.hash("a-long-local-password")

    assert encoded.startswith("$argon2")
    assert service.verify(encoded, "a-long-local-password") is True
    assert service.verify(encoded, "wrong-password") is False


def test_first_admin_requires_explicit_non_default_password(database):
    with database.session() as session:
        with pytest.raises(ValueError, match="至少 12"):
            bootstrap_admin(session, "admin", "short")
        admin = bootstrap_admin(session, "admin", "a-unique-local-password")
        assert admin.role == "admin"

    with database.session() as session:
        with pytest.raises(ValueError, match="已经存在"):
            bootstrap_admin(session, "other", "another-local-password")


def test_project_access_is_owner_member_or_admin_only(database):
    with database.session() as session:
        owner = User(username="owner", password_hash="hash", role="member")
        outsider = User(username="outsider", password_hash="hash", role="member")
        admin = User(username="admin", password_hash="hash", role="admin")
        session.add_all([owner, outsider, admin])
        session.flush()
        project = create_project(session, owner, "演示片", "真实产品界面")
        assert_project_access(owner, project)
        assert_project_access(admin, project)
        with pytest.raises(PermissionDenied):
            assert_project_access(outsider, project)


def test_storyboard_requires_admin_approval_and_writes_audit(database):
    with database.session() as session:
        member = User(username="member", password_hash="hash", role="member")
        admin = User(username="admin", password_hash="hash", role="admin")
        session.add_all([member, admin])
        session.flush()
        project = create_project(session, member, "新品营销片", "真实录屏。环境空镜。人物使用产品。")
        storyboard = project.storyboards[0]
        assert len(storyboard.shots) == 3
        assert session.query(Shot).filter_by(storyboard_id=storyboard.id).count() == 3

        submit_storyboard(session, member, storyboard)
        assert storyboard.status == "pending_approval"
        with pytest.raises(PermissionDenied):
            approve_storyboard(session, member, storyboard)
        approve_storyboard(session, admin, storyboard)

        assert storyboard.status == "approved"
        assert project.status == "approved"
        assert storyboard.approved_by_id == admin.id
        assert session.query(AuditLog).filter_by(action="storyboard.approve").count() == 1


def test_p0_priority_is_admin_only():
    member = User(username="member", password_hash="hash", role="member")
    admin = User(username="admin", password_hash="hash", role="admin")

    assert validate_priority(admin, 0) == 0
    assert validate_priority(member, 1) == 1
    with pytest.raises(PermissionDenied):
        validate_priority(member, 0)
    with pytest.raises(ValueError):
        validate_priority(admin, 3)
