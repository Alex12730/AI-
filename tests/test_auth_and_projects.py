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
from video_workstation.services import projects as project_service


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


def test_draft_shot_presets_are_validated_and_audited(database):
    with database.session() as session:
        member = User(username="member", password_hash="hash", role="member")
        session.add(member)
        session.flush()
        project = create_project(session, member, "竖屏宣传片", "产品环境空镜。")
        shot = project.storyboards[0].shots[0]

        for duration, aspect_ratio in ((5, "16:9"), (10, "9:16"), (15, "16:9"), (20, "9:16")):
            project_service.update_draft_shot(
                session,
                member,
                shot,
                title=shot.title,
                prompt=shot.prompt,
                duration_seconds=duration,
                aspect_ratio=aspect_ratio,
            )
            assert shot.duration_seconds == duration
            assert shot.aspect_ratio == aspect_ratio

        with pytest.raises(ValueError, match="5、10、15、20"):
            project_service.update_draft_shot(
                session,
                member,
                shot,
                title=shot.title,
                prompt=shot.prompt,
                duration_seconds=6,
                aspect_ratio="9:16",
            )
        with pytest.raises(ValueError, match="16:9、9:16"):
            project_service.update_draft_shot(
                session,
                member,
                shot,
                title=shot.title,
                prompt=shot.prompt,
                duration_seconds=20,
                aspect_ratio="1:1",
            )

        assert shot.duration_seconds == 20
        assert shot.aspect_ratio == "9:16"
        assert session.query(AuditLog).filter_by(action="shot.update", entity_id=shot.id).count() == 4


def test_pending_storyboard_withdrawal_obeys_owner_admin_and_state_rules(database):
    with database.session() as session:
        owner = User(username="owner", password_hash="hash", role="member")
        collaborator = User(username="collaborator", password_hash="hash", role="member")
        admin = User(username="admin", password_hash="hash", role="admin")
        session.add_all([owner, collaborator, admin])
        session.flush()
        project = create_project(session, owner, "审批撤回", "环境空镜。")
        project.members.append(collaborator)
        storyboard = project.storyboards[0]

        submit_storyboard(session, owner, storyboard)
        with pytest.raises(PermissionDenied, match="创建者或管理员"):
            project_service.withdraw_storyboard(session, collaborator, storyboard)

        project_service.withdraw_storyboard(session, owner, storyboard)
        assert storyboard.status == "draft"
        assert project.status == "draft"
        assert storyboard.submitted_at is None
        assert all(shot.status == "draft" for shot in storyboard.shots)

        submit_storyboard(session, owner, storyboard)
        project_service.withdraw_storyboard(session, admin, storyboard)
        assert storyboard.status == "draft"

        submit_storyboard(session, owner, storyboard)
        approve_storyboard(session, admin, storyboard)
        with pytest.raises(ValueError, match="只有待审批"):
            project_service.withdraw_storyboard(session, admin, storyboard)

        assert session.query(AuditLog).filter_by(action="storyboard.withdraw", entity_id=storyboard.id).count() == 2


def test_bulk_update_draft_shots_updates_all_and_writes_one_audit(database):
    with database.session() as session:
        member = User(username="member", password_hash="hash", role="member")
        session.add(member)
        session.flush()
        project = create_project(session, member, "统一镜头", "环境空镜。人物表演。产品界面。")
        storyboard = project.storyboards[0]

        updated_count = project_service.bulk_update_draft_shots(
            session,
            member,
            storyboard,
            duration_seconds=15,
            aspect_ratio="9:16",
        )

        assert updated_count == 3
        assert [(shot.duration_seconds, shot.aspect_ratio) for shot in storyboard.shots] == [
            (15, "9:16"),
            (15, "9:16"),
            (15, "9:16"),
        ]
        audit = session.query(AuditLog).filter_by(
            action="storyboard.shots.bulk_update",
            entity_id=storyboard.id,
        ).one()
        assert audit.entity_type == "storyboard"
        assert audit.details_json == {
            "duration_seconds": 15,
            "aspect_ratio": "9:16",
            "shot_count": 3,
        }


def test_bulk_update_draft_shots_rejects_invalid_or_locked_without_partial_changes(database):
    with database.session() as session:
        member = User(username="member", password_hash="hash", role="member")
        session.add(member)
        session.flush()
        project = create_project(session, member, "拒绝错误批量设置", "环境空镜。人物表演。")
        storyboard = project.storyboards[0]
        original = [(shot.duration_seconds, shot.aspect_ratio) for shot in storyboard.shots]

        with pytest.raises(ValueError, match="5、10、15、20"):
            project_service.bulk_update_draft_shots(
                session,
                member,
                storyboard,
                duration_seconds=6,
                aspect_ratio="9:16",
            )
        assert [(shot.duration_seconds, shot.aspect_ratio) for shot in storyboard.shots] == original

        with pytest.raises(ValueError, match="16:9、9:16"):
            project_service.bulk_update_draft_shots(
                session,
                member,
                storyboard,
                duration_seconds=15,
                aspect_ratio="1:1",
            )
        assert [(shot.duration_seconds, shot.aspect_ratio) for shot in storyboard.shots] == original

        submit_storyboard(session, member, storyboard)
        with pytest.raises(ValueError, match="只有草稿分镜可以编辑"):
            project_service.bulk_update_draft_shots(
                session,
                member,
                storyboard,
                duration_seconds=15,
                aspect_ratio="9:16",
            )
        assert [(shot.duration_seconds, shot.aspect_ratio) for shot in storyboard.shots] == original
        assert session.query(AuditLog).filter_by(action="storyboard.shots.bulk_update").count() == 0


def test_bulk_update_draft_shots_enforces_access_and_accepts_empty_storyboard(database):
    with database.session() as session:
        owner = User(username="owner", password_hash="hash", role="member")
        outsider = User(username="outsider", password_hash="hash", role="member")
        session.add_all([owner, outsider])
        session.flush()
        project = create_project(session, owner, "空分镜批量设置", "环境空镜。")
        storyboard = project.storyboards[0]

        with pytest.raises(PermissionDenied, match="无权访问"):
            project_service.bulk_update_draft_shots(
                session,
                outsider,
                storyboard,
                duration_seconds=10,
                aspect_ratio="9:16",
            )
        assert session.query(AuditLog).filter_by(action="storyboard.shots.bulk_update").count() == 0

        storyboard.shots.clear()
        updated_count = project_service.bulk_update_draft_shots(
            session,
            owner,
            storyboard,
            duration_seconds=10,
            aspect_ratio="9:16",
        )

        assert updated_count == 0
        assert storyboard.shots == []
        audit = session.query(AuditLog).filter_by(
            action="storyboard.shots.bulk_update",
            entity_id=storyboard.id,
        ).one()
        assert audit.details_json == {
            "duration_seconds": 10,
            "aspect_ratio": "9:16",
            "shot_count": 0,
        }


def test_p0_priority_is_admin_only():
    member = User(username="member", password_hash="hash", role="member")
    admin = User(username="admin", password_hash="hash", role="admin")

    assert validate_priority(admin, 0) == 0
    assert validate_priority(member, 1) == 1
    with pytest.raises(PermissionDenied):
        validate_priority(member, 0)
    with pytest.raises(ValueError):
        validate_priority(admin, 3)
