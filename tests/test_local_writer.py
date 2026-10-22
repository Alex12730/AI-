import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from video_workstation.adapters.local_writer import LocalWriter, LocalWriterError
from video_workstation.writer_deployment import validate_writer_deployment
from video_workstation.writing_contracts import WritingRequest
from test_writing_contracts import body_data, request_data


def deployment(tmp_path):
    engine=tmp_path/'engine';engine.mkdir(exist_ok=True)
    exe=engine/'llama-server.exe';exe.write_bytes(b'MZ-test-engine')
    model=tmp_path/'model.gguf';model.write_bytes(b'GGUF-test-model')
    license=tmp_path/'LICENSE';license.write_text('test license')
    row=lambda p:{'path':str(p.relative_to(tmp_path)).replace('\\','/'),'bytes':p.stat().st_size,
        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    manifest={'schema_version':1,'engine':{'version':'test','exe_path':'engine/llama-server.exe','files':[row(exe)]},
        'model':{'repo':'Qwen/Qwen3-8B-GGUF','revision':'a'*40,'filename':'model.gguf',
            'bytes':model.stat().st_size,'sha256':hashlib.sha256(model.read_bytes()).hexdigest(),'license_path':'LICENSE'},
        'context_size':16384,'gpu_layers':0,'template_version':'local-writing-v1','verification':None}
    path=tmp_path/'deployment.json';path.write_text(json.dumps(manifest),encoding='utf-8')
    return path


def test_deployment_validates_all_files_and_rejects_escape_and_changed_weights(tmp_path):
    path=deployment(tmp_path)
    result=validate_writer_deployment(path)
    assert result['model_path']==tmp_path/'model.gguf'
    manifest=json.loads(path.read_text())
    manifest['model']['filename']='../model.gguf';path.write_text(json.dumps(manifest))
    with pytest.raises(LocalWriterError):validate_writer_deployment(path)
    path=deployment(tmp_path)
    (tmp_path/'model.gguf').write_bytes(b'GGUF-corrupted')
    with pytest.raises(LocalWriterError,match='校验'):validate_writer_deployment(path)


class FakeProcess:
    pid=1234567
    stopped=False
    def poll(self):return 0 if self.stopped else None
    def terminate(self):self.stopped=True
    def kill(self):self.stopped=True
    def wait(self,timeout=None):return 0


@pytest.fixture
def writer_server(tmp_path,monkeypatch):
    import video_workstation.adapters.local_writer as mod
    replies={'finish_reason':'stop','content':json.dumps(body_data(),ensure_ascii=False),'status':200}
    observed=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            payload=json.dumps({'choices':[{'finish_reason':replies['finish_reason'],
                'message':{'content':replies['content']}}]},ensure_ascii=False).encode('utf-8')
            self.send_response(replies['status'])
            if replies['status']==302:self.send_header('Location','https://example.com/')
            self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(mod,'WRITER_PORT',server.server_port)
    monkeypatch.setattr(mod,'_port_is_free',lambda p:True)
    monkeypatch.setattr(mod,'_listener_pid',lambda p:1234567)
    monkeypatch.setattr(mod,'_process_identity',lambda pid:{'pid':pid,'created':'test-time','exe':str(tmp_path/'engine/llama-server.exe')})
    process=FakeProcess()
    commands=[]
    def spawn(argv,**kwargs):
        commands.append((argv,kwargs));return process
    monkeypatch.setattr(mod.subprocess,'Popen',spawn)
    yield deployment(tmp_path),replies,observed,commands,process
    server.shutdown();server.server_close();thread.join(timeout=3)


def test_writer_uses_local_schema_and_cleans_own_process(writer_server,tmp_path):
    path,replies,observed,commands,process=writer_server
    body=LocalWriter(path).generate(WritingRequest(**request_data(idea='中文 "引号" 与换行\n$(not-a-command)')),work_dir=tmp_path/'job')
    assert body.title=='森林里的萤火虫'
    assert observed[0]['chat_template_kwargs']=={'enable_thinking':False}
    assert observed[0]['response_format']['type']=='json_schema'
    argv,kwargs=commands[0]
    assert '--offline' in argv and '-hf' not in argv
    assert kwargs['shell'] is False
    assert process.stopped
    assert not (tmp_path/'job'/'engine-owner.json').exists()


@pytest.mark.parametrize('mutation',[
    {'content':''},{'content':'{"title":"missing fields"}'},{'finish_reason':'length'},
    {'content':'x'*1_048_577},{'status':302}])
def test_invalid_or_redirected_result_fails_and_releases_engine(writer_server,tmp_path,mutation):
    path,replies,observed,commands,process=writer_server
    replies.update(mutation)
    with pytest.raises(LocalWriterError):
        LocalWriter(path).generate(WritingRequest(**request_data()),work_dir=tmp_path/'job')
    assert process.stopped


def test_unknown_process_wins_port_race_is_never_contacted(writer_server,tmp_path,monkeypatch):
    import video_workstation.adapters.local_writer as mod
    path,replies,observed,commands,process=writer_server
    monkeypatch.setattr(mod,'_listener_pid',lambda p:999999)
    with pytest.raises(LocalWriterError,match='端口'):
        LocalWriter(path).generate(WritingRequest(**request_data()),work_dir=tmp_path/'job')
    assert observed==[]
    assert process.stopped
