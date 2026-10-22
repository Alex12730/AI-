import copy

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text

from video_workstation.writing_contracts import WritingBody, WritingRequest
from video_workstation.config import Settings
from video_workstation.db import Database


def body_data(count=6):
    return {"schema_version": 1, "title": "森林里的萤火虫", "summary": "寻找与发现的故事",
        "script": "小女孩走入森林，发现了萤火虫。", "characters": [{"name": "小雨", "description": "穿黄色雨衣的小女孩"}],
        "shots": [{"title": f"镜头 {i+1}", "visual": "森林，树叶间透出微光", "narration": "森林亮了起来。",
                   "scene_type": "broll", "duration_seconds": 5} for i in range(count)]}


def request_data(**updates):
    return {"idea": "小女孩在森林寻找萤火虫", "recipe": "story", "target_seconds": 30,
        "aspect_ratio": "16:9", "operation": "generate", **updates}


def test_writing_accepts_chinese_and_enforces_exact_shot_budget():
    body = WritingBody.model_validate(body_data())
    body.validate_for_target(30)
    assert len(body.shots) == 6
    assert body.script == "小女孩走入森林，发现了萤火虫。"
    with pytest.raises(ValueError, match="镜头"):
        body.validate_for_target(10)


@pytest.mark.parametrize("changes", [{"target_seconds": 31}, {"idea": " "}, {"recipe": "unknown"},
    {"operation": "execute"}, {"operation": "custom", "instruction": ""},
    {"operation": "custom", "instruction": "字" * 2001}, {"idea": "字" * 4001}])
def test_invalid_writing_request_rejected_before_queue(changes):
    with pytest.raises(ValidationError):
        WritingRequest.model_validate(request_data(**changes))


def test_writing_rejects_empty_script_excess_shots_and_extra_fields():
    for change in ({"script": " "}, {"execute": "anything"}, {"shots": body_data(37)["shots"]}):
        with pytest.raises(ValidationError):
            WritingBody.model_validate({**body_data(), **change})
    with pytest.raises(ValidationError):
        WritingRequest.model_validate(request_data(base_body={**body_data(), "script": "字"*6000}, idea="字"*4000,
            instruction="字"*2000, operation="custom"))


def test_migration_upgrades_existing_0005_preserving_personal_plan(tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    db = Database(Settings(data_dir=tmp_path))
    db.migrate()
    from video_workstation.models import User, Project, Storyboard, CreationPlan, Task
    with db.session() as session:
        user=User(username="existing", password_hash="hash")
        session.add(user);session.flush()
        project=Project(name="原作品",created_by_id=user.id)
        session.add(project);session.flush()
        board=Storyboard(project_id=project.id,version=1,created_by_id=user.id)
        session.add(board);session.flush()
        plan=CreationPlan(project_id=project.id,storyboard_id=board.id,created_by_id=user.id,
            recipe="story",target_seconds=5,aspect_ratio="16:9")
        task=Task(project_id=project.id,created_by_id=user.id,task_type="generate")
        session.add_all([plan,task]);session.flush()
        ids=(user.id,project.id,board.id,plan.id,task.id)
    # Recreate the old uniqueness to simulate the schema from 0005.
    with db.engine.begin() as c:
        c.execute(text("CREATE UNIQUE INDEX old_creation_project_unique ON creation_plans(project_id)"))
        c.execute(text("UPDATE alembic_version SET version_num='20261022_0005'"))
        for table in ("writing_publications", "writing_revisions", "writing_documents"):
            c.execute(text(f"DROP TABLE {table}"))
    db.migrate()
    with db.session() as session:
        assert session.get(User,ids[0]).username == "existing"
        assert session.get(Task,ids[4]).project_id == ids[1]
        assert session.get(CreationPlan,ids[3]).storyboard_id == ids[2]
        new_board=Storyboard(project_id=ids[1],version=2,created_by_id=ids[0])
        session.add(new_board);session.flush()
        session.add(CreationPlan(project_id=ids[1],storyboard_id=new_board.id,created_by_id=ids[0],
            recipe="story",target_seconds=5,aspect_ratio="16:9"))
    assert {"writing_documents","writing_revisions","writing_publications"} <= set(inspect(db.engine).get_table_names())
