"""Explicit online preparation, separate from offline task execution."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from video_workstation.writer_install import activate_writer_package,download_file,download_license
from video_workstation.writer_deployment import file_sha256


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--cache',type=Path,default=Path(__file__).resolve().parents[1]/'data/writer-cache')
    parser.add_argument('--proxy')
    args=parser.parse_args()
    args.root.mkdir(parents=True,exist_ok=True);args.cache.mkdir(parents=True,exist_ok=True)
    source=json.loads((Path(__file__).resolve().parents[1]/'config/writer-source-lock.json').read_text())
    if shutil.disk_usage(args.root).free<max(100*1024**3,source['model']['bytes']*2):
        raise SystemExit('磁盘剩余空间不足，未开始安装')
    model=source['model'];engine=source['engine']
    existing=args.root/'models/local-writer/Qwen3-8B-Q4_K_M.gguf'
    if existing.is_file() and existing.stat().st_size==model['bytes'] and file_sha256(existing)==model['sha256']:
        model_cache=existing
    else:model_cache=download_file(model['url'],args.cache/'Qwen3-8B-Q4_K_M.gguf',model['bytes'],model['sha256'],proxy=args.proxy)
    archive=download_file(engine['url'],args.cache/f"llama-{engine['version']}-bin-win-cpu-x64.zip",engine['bytes'],engine['sha256'],proxy=args.proxy)
    manifest=activate_writer_package(args.root,model_cache,archive,
        {k:model[k] for k in ('repo','revision','bytes','sha256')},
        {k:engine[k] for k in ('version','bytes','sha256')},
        model_license=download_license(model['license_url'],proxy=args.proxy),
        engine_license=download_license(engine['license_url'],proxy=args.proxy))
    print(json.dumps({'prepared':True,'manifest':str(manifest),'text_ready':False},ensure_ascii=False))


if __name__=='__main__':main()
