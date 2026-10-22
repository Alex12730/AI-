from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from video_workstation.models import User, ModelProfile, Task, WritingRevision, Shot
from video_workstation.db import Database
from video_workstation.config import Settings
from video_workstation.services.models import seed_model_profiles, model_config_fingerprint
from video_workstation.services.projects import PermissionDenied
from video_workstation.services.writing import (create_writing_document, save_writing_revision,
    enqueue_writing, store_writing_candidate, select_writing_revision, publish_writing_revision, WritingConflict)
from video_workstation.writing_contracts import WritingRequest, WritingBody
from test_writing_contracts import body_data, request_data


@pytest.fixture
def writing_db(tmp_path):
    db=Database(Settings(data_dir=tmp_path));db.create_schema()
    with db.session() as s:
        owner=User(username="owner",password_hash="hash",role="member")
        outsider=User(username="other",password_hash="hash",role="admin")
        s.add_all([owner,outsider]);seed_model_profiles(s);s.flush()
        p=s.scalar(select(ModelProfile).where(ModelProfile.slug=="qwen-local"))
        p.enabled=True;p.model_version="writer-test";p.quantization="Q4_K_M"
        p.runtime_config_json={"writer_manifest":"test.json","manifest_sha256":"a"*64}
        p.capabilities_json={"text_ready":True,"text_ready_fingerprint":model_config_fingerprint(p)}
    return db,owner.id,outsider.id


def make_doc(s,owner):
    return create_writing_document(s,s.get(User,owner),WritingRequest(**request_data()))


def running(task):
    task.status="running";task.lease_token="lease"
    task.lease_expires_at=datetime.now(timezone.utc)+timedelta(seconds=60)
    from sqlalchemy.orm import object_session
    object_session(task).flush()


def test_candidate_never_overwrites_manual_edit_and_is_idempotent(writing_db):
    db,owner,_=writing_db
    with db.session() as s:
        actor=s.get(User,owner);doc=make_doc(s,owner)
        v1=save_writing_revision(s,actor,doc,WritingBody(**body_data()),expected_latest=0)
        req=WritingRequest(**request_data(operation="conversational",base_body=v1.body_json))
        task=enqueue_writing(s,actor,doc,req,expected_latest=1,request_key="a"*32)
        again=enqueue_writing(s,actor,doc,req,expected_latest=1,request_key="a"*32)
        assert again.id==task.id
        v2=save_writing_revision(s,actor,doc,WritingBody(**{**body_data(),"script":"这是我自己的修改。"}),expected_latest=1)
        running(task)
        v3=store_writing_candidate(s,task,WritingBody(**{**body_data(),"script":"这是生成的另一种写法。"}),lease_token="lease")
        assert (v1.version,v2.version,v3.version)==(1,2,3)
        assert doc.selected_version==2
        assert v2.body_json["script"]=="这是我自己的修改。"
        assert store_writing_candidate(s,task,WritingBody(**body_data()),lease_token="lease").id==v3.id
        assert v3.parent_version==1


def test_conflicts_and_other_users_cannot_change_document(writing_db):
    db,owner,outsider=writing_db
    with db.session() as s:
        doc=make_doc(s,owner);actor=s.get(User,owner)
        save_writing_revision(s,actor,doc,WritingBody(**body_data()),expected_latest=0)
        with pytest.raises(WritingConflict):
            save_writing_revision(s,actor,doc,WritingBody(**body_data()),expected_latest=0)
        with pytest.raises(PermissionDenied):
            save_writing_revision(s,s.get(User,outsider),doc,WritingBody(**body_data()),expected_latest=1)
        with pytest.raises(PermissionDenied):
            publish_writing_revision(s,s.get(User,outsider),doc,1)


def test_publication_freezes_separate_boards_and_does_not_enqueue_video(writing_db):
    db,owner,_=writing_db
    with db.session() as s:
        actor=s.get(User,owner);doc=make_doc(s,owner)
        save_writing_revision(s,actor,doc,WritingBody(**body_data()),expected_latest=0)
        first=publish_writing_revision(s,actor,doc,1)
        assert publish_writing_revision(s,actor,doc,1).id==first.id
        old_ids=[shot.id for shot in first.storyboard.shots]
        first.status="running";first.snapshot_json={"immutable":"first run"}
        save_writing_revision(s,actor,doc,WritingBody(**{**body_data(),"script":"新文案"}),expected_latest=1)
        second=publish_writing_revision(s,actor,doc,2)
        assert second.id!=first.id and second.project_id==first.project_id
        assert second.storyboard.version==first.storyboard.version+1
        assert first.snapshot_json=={"immutable":"first run"}
        assert [shot.id for shot in first.storyboard.shots]==old_ids
        assert s.scalar(select(Task.id)) is None
        assert second.storyboard.status=="draft"


def test_expired_lease_cannot_store_generated_candidate(writing_db):
    db,owner,_=writing_db
    with db.session() as s:
        doc=make_doc(s,owner);actor=s.get(User,owner)
        task=enqueue_writing(s,actor,doc,WritingRequest(**request_data()),expected_latest=0,request_key="b"*32)
        running(task);task.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        s.flush()
        with pytest.raises(WritingConflict):
            store_writing_candidate(s,task,WritingBody(**body_data()),lease_token="lease")
        assert s.scalar(select(WritingRevision.id)) is None


def test_real_interface_constraints_survive_generated_scene_type(writing_db):
    db,owner,_=writing_db
    with db.session() as s:
        doc=make_doc(s,owner);actor=s.get(User,owner)
        data=body_data();data["shots"][0]["visual"]="展示真实软件按钮和价格"
        save_writing_revision(s,actor,doc,WritingBody(**data),expected_latest=0)
        plan=publish_writing_revision(s,actor,doc,1)
        assert sorted(plan.storyboard.shots,key=lambda v:v.sequence_no)[0].scene_type=="product_ui"
