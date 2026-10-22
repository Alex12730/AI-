import hashlib
import json
import zipfile

import pytest

from video_workstation.writer_install import activate_writer_package,download_file
from video_workstation.writer_deployment import LocalWriterError,validate_writer_deployment


def package(tmp_path,escape=False):
    model=tmp_path/'cached.gguf';model.write_bytes(b'GGUF-test-package')
    archive=tmp_path/'engine.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('llama-server.exe',b'MZ-test-server')
        z.writestr('../outside.dll' if escape else 'ggml-base.dll',b'local-dll')
    model_info={'repo':'Qwen/Qwen3-8B-GGUF','revision':'a'*40,'sha256':hashlib.sha256(model.read_bytes()).hexdigest(),'bytes':model.stat().st_size}
    engine_info={'version':'test-build','sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'bytes':archive.stat().st_size}
    return model,archive,model_info,engine_info


def test_package_activation_validates_before_executing_or_enabling(tmp_path):
    model,archive,mi,ei=package(tmp_path)
    root=tmp_path/'install'
    manifest=activate_writer_package(root,model,archive,mi,ei,model_license=b'local model license',engine_license=b'local engine license')
    info=validate_writer_deployment(manifest)
    assert info['model_path'].is_file()
    assert info['manifest']['verification'] is None
    assert not model.exists()  # Same-volume move reuses the downloaded weight.


def test_archive_escape_or_wrong_hash_never_activates_package(tmp_path):
    model,archive,mi,ei=package(tmp_path,escape=True)
    root=tmp_path/'install'
    with pytest.raises(LocalWriterError):
        activate_writer_package(root,model,archive,mi,ei,model_license=b'x',engine_license=b'x')
    assert not (root/'local-writer-deployment.json').exists()
    assert not (tmp_path/'outside.dll').exists()
    mi['sha256']='0'*64
    with pytest.raises(LocalWriterError):
        activate_writer_package(root,model,archive,mi,ei,model_license=b'x',engine_license=b'x')
    assert model.exists()


def test_missing_dependency_and_file_urls_rejected(tmp_path):
    model,archive,mi,ei=package(tmp_path)
    manifest=activate_writer_package(tmp_path/'install',model,archive,mi,ei,model_license=b'x',engine_license=b'x')
    info=validate_writer_deployment(manifest)
    (info['exe_path'].parent/'ggml-base.dll').unlink()
    with pytest.raises(LocalWriterError):validate_writer_deployment(manifest)
    with pytest.raises(LocalWriterError):download_file('file:///C:/Windows/win.ini',tmp_path/'bad',1,'0'*64)


def test_interrupted_download_keeps_partial_and_never_activates(tmp_path,monkeypatch):
    import urllib.request
    class InterruptedResponse:
        status=200
        headers={}
        reads=0
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):
            self.reads+=1
            if self.reads==1:return b'GGUF'
            raise OSError('connection interrupted')
    class Opener:
        def open(self,*args,**kwargs):return InterruptedResponse()
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args:Opener())
    target=tmp_path/'model.gguf'
    with pytest.raises(LocalWriterError,match='中断'):
        download_file('https://huggingface.co/Qwen/test',target,20,'0'*64)
    assert target.with_name(target.name+'.part').read_bytes()==b'GGUF'
    assert not target.exists()
    assert not (tmp_path/'local-writer-deployment.json').exists()


def test_registration_requires_matching_real_report_and_never_admits_video(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import select
    from video_workstation.models import ModelProfile
    from video_workstation.services.writing import writer_is_ready
    from video_workstation.writer_deployment import file_sha256
    from test_writing_contracts import body_data
    spec=importlib.util.spec_from_file_location('register_writer',Path(__file__).parents[1]/'scripts/register_local_writer.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    model,archive,mi,ei=package(tmp_path)
    manifest=activate_writer_package(tmp_path/'install',model,archive,mi,ei,model_license=b'x',engine_license=b'x')
    output=tmp_path/'body.json';output.write_text(json.dumps(body_data()),encoding='utf-8')
    report={'passed':True,'finished_at':'2026-10-22T00:00:00+00:00','engine_version':ei['version'],
        'manifest_sha256':'0'*64,'cases':[
            {'recipe':r,'operation':op,'passed':True,'manual_input_preserved':True,'shot_count':6,'output':str(output)}
            for r,op in [('story','generate'),('product','generate'),('knowledge','generate'),('story','custom')]]}
    path=tmp_path/'results.json';path.write_text(json.dumps(report),encoding='utf-8')
    with pytest.raises(ValueError,match='does not match'):module.register(manifest,path,tmp_path/'db')
    assert json.loads(manifest.read_text())['verification'] is None
    report['manifest_sha256']=file_sha256(manifest);path.write_text(json.dumps(report),encoding='utf-8')
    db=module.register(manifest,path,tmp_path/'db')
    module.register(manifest,path,tmp_path/'db')  # Idempotent same attestation.
    with db.session() as session:
        profile=session.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
        assert writer_is_ready(profile)
        assert profile.validated_presets_json==[]
    changed=json.loads(manifest.read_text(encoding='utf-8'))
    weight=manifest.parent/changed['model']['filename']
    weight.write_bytes(b'GGUF-different-model')
    changed['model']['bytes']=weight.stat().st_size
    changed['model']['sha256']=file_sha256(weight)
    manifest.write_text(json.dumps(changed,indent=2),encoding='utf-8')
    with pytest.raises(ValueError,match='does not match'):
        module.register(manifest,path,tmp_path/'db')
