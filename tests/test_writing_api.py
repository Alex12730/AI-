from sqlalchemy import select

from test_api import make_client,login
from test_creation_api import token
from test_writing_contracts import body_data,request_data
from video_workstation.models import User,WritingDocument,Task,WritingRevision,ModelProfile
from video_workstation.services.writing import save_writing_revision,store_writing_candidate
from video_workstation.services.models import model_config_fingerprint
from video_workstation.writing_contracts import WritingBody


def new_document(client):
    page=client.get('/create')
    return client.post('/writing',data={'csrf_token':token(page),'idea':'森林里的萤火虫',
        'recipe':'story','target_seconds':30,'aspect_ratio':'16:9','intent':'save'},follow_redirects=False)


def seed_revision(app,client):
    url=new_document(client).headers['location']
    with app.state.database.session() as s:
        doc=s.scalar(select(WritingDocument));actor=s.get(User,doc.created_by_id)
        save_writing_revision(s,actor,doc,WritingBody(**body_data()),expected_latest=0)
    return url


def test_new_writer_entry_and_selected_recipe_help(tmp_path):
    client,app=make_client(tmp_path);login(client,'member')
    page=client.get('/create')
    assert '让工作台帮我写' in page.text
    assert '我已有完整文案' in page.text
    assert '选哪种流程？' not in page.text
    response=new_document(client)
    assert response.status_code==303
    assert client.get(response.headers['location']).status_code==200


def test_edit_save_conflict_preserves_text_and_publication_is_idempotent(tmp_path):
    client,app=make_client(tmp_path);login(client,'member')
    url=seed_revision(app,client);page=client.get(url)
    edited={'csrf_token':token(page),'expected_latest':1,'source_version':1,
        'title':'我的标题','summary':'我的概要','script':'这是我亲手修改的文案。'}
    saved=client.post(url+'/save',data=edited,follow_redirects=False)
    assert saved.status_code==303
    response=client.post(url+'/save',data={**edited,'script':'第二个标签页尚未保存的内容'})
    assert response.status_code==409
    assert '第二个标签页尚未保存的内容' in response.text
    current=client.get(url)
    pub=client.post(url+'/publish',data={'csrf_token':token(current),'version':2},follow_redirects=False)
    assert pub.status_code==303
    pub2=client.post(url+'/publish',data={'csrf_token':token(current),'version':2},follow_redirects=False)
    assert pub.headers['location']==pub2.headers['location']
    assert client.get(pub.headers['location']).status_code==200
    with app.state.database.session() as s:
        assert s.scalar(select(Task.id)) is None


def test_writer_requires_owner_csrf_and_escapes_user_content(tmp_path):
    client,app=make_client(tmp_path);login(client,'member')
    url=seed_revision(app,client)
    page=client.get(url)
    assert client.post(url+'/select',data={'csrf_token':'wrong','version':1,'expected_latest':1}).status_code==403
    modified=client.post(url+'/save',data={'csrf_token':token(page),'source_version':1,'expected_latest':1,
        'title':'X','summary':'说明','script':'<script>bad()</script>'},follow_redirects=False)
    assert modified.status_code==303
    assert '&lt;script&gt;bad()&lt;/script&gt;' in client.get(url).text
    client.post('/logout');login(client,'outsider')
    assert client.get(url).status_code==403
    assert client.get(url+'/status').status_code==403
    assert client.post(url+'/publish',data={'csrf_token':token(client.get('/create')),'version':1}).status_code==403


def test_missing_model_keeps_writing_and_does_not_queue_fake_output(tmp_path):
    client,app=make_client(tmp_path);login(client,'member')
    url=seed_revision(app,client);page=client.get(url)
    response=client.post(url+'/generate',data={'csrf_token':token(page),'expected_latest':1,
        'expected_selected':1,'operation':'generate','request_key':'c'*32})
    assert response.status_code==409
    assert '尚未准备好' in response.text
    assert '小女孩走入森林' in response.text
    with app.state.database.session() as s:assert s.scalar(select(Task.id)) is None


def test_second_tab_selection_cannot_silently_change_rewrite_source(tmp_path):
    client,app=make_client(tmp_path);login(client,'member')
    url=seed_revision(app,client)
    with app.state.database.session() as s:
        doc=s.scalar(select(WritingDocument));actor=s.get(User,doc.created_by_id)
        save_writing_revision(s,actor,doc,WritingBody(**{**body_data(),'script':'第二版内容'}),expected_latest=1)
        profile=s.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
        from test_local_writer import deployment
        from video_workstation.writer_deployment import file_sha256
        manifest=deployment(tmp_path/'package')
        profile.enabled=True;profile.runtime_config_json={'writer_manifest':str(manifest),'manifest_sha256':file_sha256(manifest)}
        profile.capabilities_json={'text_ready':True,'text_ready_fingerprint':model_config_fingerprint(profile)}
    page=client.get(url)  # Tab A sees v2.
    assert client.post(url+'/select',data={'csrf_token':token(page),'version':1,'expected_latest':2},follow_redirects=False).status_code==303
    stale=client.post(url+'/generate',data={'csrf_token':token(page),'expected_latest':2,
        'expected_selected':2,'operation':'conversational','request_key':'d'*32})
    assert stale.status_code==409
    with app.state.database.session() as s:assert s.scalar(select(Task.id)) is None


def test_live_activity_and_duplicate_generation_guard(tmp_path):
    from datetime import datetime,timedelta,timezone
    from test_local_writer import deployment
    from video_workstation.writer_deployment import file_sha256
    client,app=make_client(tmp_path);login(client,'member')
    url=seed_revision(app,client)
    with app.state.database.session() as s:
        profile=s.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
        manifest=deployment(tmp_path/'package')
        profile.enabled=True;profile.runtime_config_json={'writer_manifest':str(manifest),'manifest_sha256':file_sha256(manifest)}
        profile.capabilities_json={'text_ready':True,'text_ready_fingerprint':model_config_fingerprint(profile)}
    page=client.get(url)
    payload={'csrf_token':token(page),'expected_latest':1,'expected_selected':1,'operation':'generate','request_key':'1'*32}
    assert client.post(url+'/generate',data=payload,follow_redirects=False).status_code==303
    duplicate=client.post(url+'/generate',data={**payload,'request_key':'2'*32},follow_redirects=False)
    assert duplicate.status_code==303
    with app.state.database.session() as s:
        tasks=list(s.scalars(select(Task)));assert len(tasks)==1
        task=tasks[0];task.status='running';task.started_at=datetime.now(timezone.utc)-timedelta(seconds=40)
        task.heartbeat_at=datetime.now(timezone.utc)-timedelta(seconds=3)
    state=client.get(url+'/status').json()['activity']
    assert state['state']=='running' and state['elapsed_seconds']>=40
    assert state['heartbeat_age_seconds']<10 and state['active_count']==1
    page=client.get(url)
    assert 'data-writing-status-label' in page.text and '正在本机生成' in page.text
    assert 'data-back-button' in page.text
    assert 'data-writing-busy="yes"' in page.text
    with app.state.database.session() as s:
        task=s.scalar(select(Task));task.heartbeat_at=datetime.now(timezone.utc)-timedelta(minutes=3)
    assert client.get(url+'/status').json()['activity']['state']=='recovering'
    with app.state.database.session() as s:
        task=s.scalar(select(Task));task.status='terminal_failed';task.error_message='端口被占用'
    final=client.get(url+'/status').json()['activity']
    assert final['state']=='failed' and final['active_count']==0
    assert '端口被占用' in final['detail']


def test_models_page_has_back_and_start_controls(tmp_path):
    client,app=make_client(tmp_path);login(client,'admin')
    page=client.get('/models')
    assert 'data-back-button' in page.text
    assert '返回上一页' in page.text and '返回开始制作' in page.text
