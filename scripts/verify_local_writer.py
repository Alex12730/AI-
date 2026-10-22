"""Run real local writing cases; do not mark video presets as admitted."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from video_workstation.adapters.local_writer import LocalWriter
from video_workstation.writing_contracts import WritingRequest
from video_workstation.writer_deployment import validate_writer_deployment


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--seconds',type=int,default=30)
    args=parser.parse_args()
    info=validate_writer_deployment(args.manifest)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    cases=[('story','一个小女孩在森林中寻找萤火虫，最后把它们放回树林，温暖自然的故事。'),
        ('product','为一个本地文件整理软件写教程，先导入真实录屏，展示选择文件夹、按日期整理和查看结果，不虚构软件按钮。'),
        ('knowledge','给初学者讲解为什么下雨后能看到彩虹，语言简单自然，避免夸张和错误知识。')]
    report={'started_at':datetime.now(timezone.utc).isoformat(),'manifest_sha256':info['manifest_sha256'],
        'engine_version':info['manifest']['engine']['version'],'network_isolation':'not_yet_verified',
        'quality_evaluation':'structural checks; human review still required','cases':[]}
    writer=LocalWriter(args.manifest)
    first=None
    for recipe,idea in cases:
        print(json.dumps({'phase':'generating','recipe':recipe,'target_seconds':args.seconds}),flush=True)
        req=WritingRequest(idea=idea,recipe=recipe,target_seconds=args.seconds,aspect_ratio='16:9')
        start=time.monotonic();body=writer.generate(req,work_dir=args.output_dir/(recipe+'-job'))
        elapsed=round(time.monotonic()-start,2)
        output=args.output_dir/(recipe+'.json');output.write_text(body.model_dump_json(indent=2),encoding='utf-8')
        report['cases'].append({'recipe':recipe,'operation':'generate','passed':True,'elapsed_seconds':elapsed,
            'shot_count':len(body.shots),'title':body.title,'output':str(output.resolve())})
        (args.output_dir/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'phase':'passed','recipe':recipe,'elapsed_seconds':elapsed,'title':body.title},ensure_ascii=False),flush=True)
        if recipe=='story':first=(req,body)
    req,body=first
    # A deliberate manual change must remain in its original immutable object.
    manual=body.model_copy(deep=True);manual.script+='\n手工修改：结尾要保持温暖，不把萤火虫带回家。'
    untouched=manual.model_dump_json()
    rewrite=WritingRequest(idea=req.idea,recipe=req.recipe,target_seconds=req.target_seconds,aspect_ratio=req.aspect_ratio,
        operation='custom',base_body=manual,instruction='保留手工修改的温暖结尾，把讲述改得更口语，同时调整各段旁白。')
    print(json.dumps({'phase':'rewriting','recipe':'story'}),flush=True)
    start=time.monotonic();candidate=writer.generate(rewrite,work_dir=args.output_dir/'rewrite-job')
    assert manual.model_dump_json()==untouched
    output=args.output_dir/'rewrite.json';output.write_text(candidate.model_dump_json(indent=2),encoding='utf-8')
    report['cases'].append({'recipe':'story','operation':'custom','passed':True,'manual_input_preserved':True,
        'elapsed_seconds':round(time.monotonic()-start,2),'shot_count':len(candidate.shots),'output':str(output.resolve())})
    report['passed']=True;report['finished_at']=datetime.now(timezone.utc).isoformat()
    (args.output_dir/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'report':str((args.output_dir/'results.json').resolve())}),flush=True)


if __name__=='__main__':main()
