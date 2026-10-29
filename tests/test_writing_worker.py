import time
from sqlalchemy import select

from video_workstation.models import ModelProfile,User,Task,WritingRevision
from video_workstation.worker import Worker
from video_workstation.writing_contracts import WritingRequest,WritingBody
from video_workstation.writer_deployment import file_sha256,LocalWriterError
from video_workstation.services.models import model_config_fingerprint
from video_workstation.services.writing import create_writing_document,enqueue_writing
from test_writing import writing_db
from test_local_writer import deployment
from test_writing_contracts import request_data,body_data


def queued(db,owner,tmp_path):
    path=deployment(tmp_path)
    with db.session() as s:
        p=s.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
        p.runtime_config_json={'writer_manifest':str(path),'manifest_sha256':file_sha256(path)}
        p.capabilities_json={'text_ready':True,'text_ready_fingerprint':model_config_fingerprint(p)}
        actor=s.get(User,owner);req=WritingRequest(**request_data())
        doc=create_writing_document(s,actor,req)
        task=enqueue_writing(s,actor,doc,req,expected_latest=0,request_key='d'*32)
    return task.id,doc.id


def test_writing_worker_stores_candidate_with_heartbeat(writing_db,tmp_path,monkeypatch):
    from video_workstation.adapters.local_writer import LocalWriter
    db,owner,_=writing_db
    task_id,doc_id=queued(db,owner,tmp_path)
    def generate(*args,**kwargs):
        time.sleep(.08)
        return WritingBody(**body_data())
    monkeypatch.setattr(LocalWriter,'generate',generate)
    with db.session() as s:
        assert Worker('writer-test',settings=db.settings,heartbeat_seconds=.01).run_once(s)
        task=s.get(Task,task_id)
        assert task.status=='succeeded'
        revision=s.scalar(select(WritingRevision).where(WritingRevision.task_id==task_id))
        assert revision.body_json['title']=='森林里的萤火虫'
        assert task.result_json['revision_id']==revision.id
        assert task.heartbeat_at is not None


def test_writer_failure_does_not_fabricate_candidate_or_crash_worker(writing_db,tmp_path,monkeypatch):
    from video_workstation.adapters.local_writer import LocalWriter
    db,owner,_=writing_db
    task_id,_=queued(db,owner,tmp_path)
    def fail(*a,**kw):raise LocalWriterError('本地模型缺失')
    monkeypatch.setattr(LocalWriter,'generate',fail)
    with db.session() as s:
        Worker('writer-test',settings=db.settings).run_once(s)
        assert s.get(Task,task_id).status=='terminal_failed'
        assert '模型缺失' in s.get(Task,task_id).error_message
        assert s.scalar(select(WritingRevision.id)) is None


def test_changed_manifest_does_not_execute_model(writing_db,tmp_path,monkeypatch):
    from video_workstation.adapters.local_writer import LocalWriter
    db,owner,_=writing_db
    task_id,_=queued(db,owner,tmp_path)
    (tmp_path/'deployment.json').write_text('{}')
    called=[]
    monkeypatch.setattr(LocalWriter,'generate',lambda *a,**k:called.append(True))
    with db.session() as s:
        Worker('writer-test',settings=db.settings).run_once(s)
        assert s.get(Task,task_id).status=='terminal_failed'
        assert '清单' in s.get(Task,task_id).error_message
        assert called==[]


def test_exhausted_task_orphan_is_reaped_before_next_writing_job(writing_db,tmp_path,monkeypatch):
    import json
    from datetime import datetime,timedelta,timezone
    import video_workstation.adapters.local_writer as mod
    db,owner,_=writing_db
    old_id,_=queued(db,owner,tmp_path)
    with db.session() as s:
        old=s.get(Task,old_id);old.status='running';old.attempts=old.max_attempts
        old.lease_token='expired';old.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        old.heartbeat_at=old.lease_expires_at
        actor=s.get(User,owner);req=WritingRequest(**request_data())
        doc=create_writing_document(s,actor,req)
        fresh=enqueue_writing(s,actor,doc,req,expected_latest=0,request_key='e'*32)
        fresh_id=fresh.id
    job=db.settings.data_dir/'writing-jobs'/old_id;job.mkdir(parents=True)
    identity={'pid':1234567,'created':'old-time','exe':str(tmp_path/'engine/llama-server.exe')}
    record=job/'engine-owner.json';record.write_text(json.dumps({'task_id':old_id,'identity':identity}))
    killed=[]
    monkeypatch.setattr(mod,'_process_identity',lambda pid:identity)
    monkeypatch.setattr(mod,'_inspect_windows_process',lambda pid,terminate_expected=None:killed.append(pid))
    def generate(*a,**k):
        if record.exists():raise LocalWriterError('端口被遗留引擎占用')
        return WritingBody(**body_data())
    monkeypatch.setattr(mod.LocalWriter,'generate',generate)
    with db.session() as s:
        Worker('recovery-test',settings=db.settings).run_once(s)
        assert s.get(Task,old_id).status=='terminal_failed'
        assert s.get(Task,fresh_id).status=='succeeded'
        assert killed==[1234567]
        assert not record.exists()
