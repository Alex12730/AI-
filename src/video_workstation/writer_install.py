"""Explicit installation helpers. Never imported by the runtime adapter."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import stat
import urllib.parse
import urllib.request
import uuid
import zipfile

from .writer_deployment import LocalWriterError,file_sha256,local_package_path,validate_writer_deployment


def _source_url(url):
    p=urllib.parse.urlparse(url)
    if p.scheme!='https' or p.hostname not in {'huggingface.co','github.com','raw.githubusercontent.com'} or p.username or p.password:
        raise LocalWriterError('安装文件只能来自已确认的 HTTPS 官方来源')


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urllib.parse.urlparse(newurl).scheme!='https':raise LocalWriterError('拒绝不安全的安装下载重定向')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download_file(url:str,destination:Path,expected_bytes:int,sha256:str,*,proxy:str|None=None)->Path:
    _source_url(url)
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_file() and destination.stat().st_size==expected_bytes and file_sha256(destination)==sha256:
        return destination
    partial=destination.with_name(destination.name+'.part')
    if partial.is_file() and partial.stat().st_size==expected_bytes and file_sha256(partial)==sha256:
        partial.replace(destination);return destination
    handlers=[_HttpsRedirect()]
    if proxy:handlers.append(urllib.request.ProxyHandler({'https':proxy,'http':proxy}))
    opener=urllib.request.build_opener(*handlers)
    headers={'User-Agent':'local-video-workstation-installer'}
    offset=partial.stat().st_size if partial.exists() else 0
    if offset:headers['Range']=f'bytes={offset}-'
    try:
        with opener.open(urllib.request.Request(url,headers=headers),timeout=60) as response:
            append=response.status==206 and response.headers.get('Content-Range','').startswith(f'bytes {offset}-')
            count=offset if append else 0
            with partial.open('ab' if append else 'wb') as stream:
                while chunk:=response.read(1024*1024):
                    count+=len(chunk)
                    if count>expected_bytes:raise LocalWriterError('安装文件超出公布大小')
                    stream.write(chunk)
        if partial.stat().st_size!=expected_bytes or file_sha256(partial)!=sha256:
            raise LocalWriterError('安装文件下载不完整或校验失败，未激活')
        partial.replace(destination);return destination
    except OSError as exc:
        raise LocalWriterError('安装下载中断，临时文件已保留；尚未激活模型') from exc


def download_license(url:str,*,proxy:str|None=None)->bytes:
    _source_url(url)
    handlers=[_HttpsRedirect()]
    if proxy:handlers.append(urllib.request.ProxyHandler({'https':proxy,'http':proxy}))
    with urllib.request.build_opener(*handlers).open(url,timeout=30) as response:
        data=response.read(1024*1024+1)
    if not data or len(data)>1024*1024:raise LocalWriterError('许可证文件无效')
    data.decode('utf-8')
    return data


def activate_writer_package(root:Path,model_cache:Path,engine_archive:Path,model_info:dict,engine_info:dict,
                            *,model_license:bytes,engine_license:bytes)->Path:
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    for path,info in ((model_cache,model_info),(engine_archive,engine_info)):
        if Path(path).stat().st_size!=info['bytes'] or file_sha256(Path(path))!=info['sha256']:
            raise LocalWriterError('能力包校验失败，未激活')
    version=engine_info['version']
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,80}',version):raise LocalWriterError('引擎版本无效')
    engine_root=local_package_path(root,'runtime/local-writer')
    target=engine_root/version
    stage=engine_root/('.'+version+'-'+uuid.uuid4().hex+'.part')
    stage.mkdir(parents=True)
    with zipfile.ZipFile(engine_archive) as archive:
        entries=archive.infolist()
        if len(entries)>2000 or sum(e.file_size for e in entries)>512*1024*1024:
            raise LocalWriterError('引擎压缩包大小或文件数异常')
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr>>16):raise LocalWriterError('引擎压缩包含符号链接')
            path=local_package_path(stage,entry.filename.rstrip('/'))
            if entry.is_dir():path.mkdir(parents=True,exist_ok=True);continue
            path.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(entry) as source,path.open('wb') as dest:shutil.copyfileobj(source,dest)
    if not (stage/'llama-server.exe').is_file():raise LocalWriterError('压缩包缺少 llama-server.exe')
    (stage/'LICENSE').write_bytes(engine_license)
    if target.exists():
        for item in stage.rglob('*'):
            if item.is_file():
                existing=target/item.relative_to(stage)
                if not existing.is_file() or file_sha256(existing)!=file_sha256(item):
                    raise LocalWriterError('现有引擎版本目录内容不同，拒绝覆盖；请检查安装目录')
    else:stage.replace(target)
    model_dir=local_package_path(root,'models/local-writer');model_dir.mkdir(parents=True,exist_ok=True)
    model=model_dir/'Qwen3-8B-Q4_K_M.gguf'
    if model.exists():
        if model.stat().st_size!=model_info['bytes'] or file_sha256(model)!=model_info['sha256']:
            raise LocalWriterError('现有写作模型校验不同，拒绝覆盖')
    else:Path(model_cache).replace(model)
    license=model_dir/'LICENSE';license.write_bytes(model_license)
    relative=lambda path:path.relative_to(root).as_posix()
    files=[{'path':relative(p),'bytes':p.stat().st_size,'sha256':file_sha256(p)} for p in sorted(target.rglob('*')) if p.is_file()]
    manifest={'schema_version':1,'engine':{'version':version,'exe_path':relative(target/'llama-server.exe'),
            'files':files,'source_sha256':engine_info['sha256']},
        'model':{**model_info,'filename':relative(model),'license_path':relative(license)},
        'context_size':16384,'gpu_layers':0,'template_version':'local-writing-v1','verification':None}
    path=root/'local-writer-deployment.json';temporary=root/'local-writer-deployment.json.part'
    temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    validate_writer_deployment(temporary)
    temporary.replace(path)
    return path
