"""Validate a closed, local-only writer package before executing any binary."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


class LocalWriterError(ValueError):
    pass


def file_sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def local_package_path(root: Path, relative: str) -> Path:
    if not isinstance(relative,str) or not relative or ':' in relative or '\\' in relative:
        raise LocalWriterError('部署路径必须是包内相对路径')
    part=Path(relative)
    if part.is_absolute() or '..' in part.parts:
        raise LocalWriterError('部署路径越界')
    result=(root/part).resolve()
    if root not in result.parents:
        raise LocalWriterError('部署路径越界')
    return result


def _check_file(root, path, size, digest):
    resolved=local_package_path(root,path)
    if (not resolved.is_file() or type(size) is not int or size<=0 or resolved.stat().st_size!=size
        or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest)
        or file_sha256(resolved)!=digest):
        raise LocalWriterError('本地写作文件校验失败，请重新准备该能力包')
    return resolved


def validate_writer_deployment(manifest_path: Path) -> dict:
    manifest_path=Path(manifest_path).resolve()
    try:
        if manifest_path.stat().st_size>1024*1024:
            raise LocalWriterError('写作部署清单过大')
        manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        root=manifest_path.parent
        if (manifest['schema_version']!=1 or manifest['context_size']!=16384
            or manifest['gpu_layers']!=0 or manifest['template_version']!='local-writing-v1'):
            raise LocalWriterError('本地写作部署版本不匹配')
        engine=manifest['engine'];model=manifest['model']
        exe=local_package_path(root,engine['exe_path'])
        if not engine['version'] or not engine['files'] or exe.suffix.lower()!='.exe':
            raise LocalWriterError('缺少本地写作引擎')
        paths=[]
        for row in engine['files']:
            paths.append(_check_file(root,row['path'],row['bytes'],row['sha256']))
        if exe not in paths or len(paths)!=len(set(paths)):
            raise LocalWriterError('写作引擎校验清单不完整')
        # Every sibling DLL must be declared; no untracked dependency may replace it.
        if any(p.resolve() not in paths for p in exe.parent.rglob('*.dll')):
            raise LocalWriterError('写作引擎存在未登记的依赖文件')
        model_path=_check_file(root,model['filename'],model['bytes'],model['sha256'])
        if model['repo']!='Qwen/Qwen3-8B-GGUF' or not re.fullmatch('[a-f0-9]{40}',model['revision']):
            raise LocalWriterError('写作模型来源或固定版本不匹配')
        with model_path.open('rb') as stream:
            if stream.read(4)!=b'GGUF':raise LocalWriterError('写作模型格式无效')
        if not local_package_path(root,model['license_path']).is_file():
            raise LocalWriterError('缺少写作模型许可证')
        return {'manifest':manifest,'manifest_sha256':file_sha256(manifest_path),'root':root,
            'exe_path':exe,'model_path':model_path}
    except LocalWriterError:
        raise
    except (OSError,KeyError,TypeError,ValueError) as exc:
        raise LocalWriterError('本地写作能力包未准备好，请检查文件与部署清单') from exc
