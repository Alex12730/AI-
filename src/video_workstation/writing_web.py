"""Editing and explicit confirmation of local writing candidates."""
from __future__ import annotations

import secrets
from fastapi import Depends,HTTPException,Request
from fastapi.responses import RedirectResponse,JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ModelProfile,Task,WritingDocument,WritingRevision
from .services.creation import RECIPES
from .services.writing import (assert_writing_owner,create_writing_document,enqueue_writing,
    get_writing_revision,publish_writing_revision,save_writing_revision,select_writing_revision,writer_is_ready)
from .writing_contracts import WritingBody,WritingRequest


def register_writing_routes(app,*,templates,session_dependency,html_user,validate_csrf):
    def owned(session,actor,document_id):
        document=session.get(WritingDocument,document_id)
        if document is None:raise HTTPException(404,'文案不存在')
        assert_writing_owner(actor,document)
        return document

    def render(request,session,actor,document,*,error='',status_code=200,values=None):
        versions=list(session.scalars(select(WritingRevision).where(WritingRevision.document_id==document.id).order_by(WritingRevision.version.desc())))
        selected=next((v for v in versions if v.version==document.selected_version),None)
        tasks=list(session.scalars(select(Task).where(Task.project_id==document.project_id,Task.task_type=='write_script').order_by(Task.created_at.desc()).limit(5)))
        profile=session.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
        return templates.TemplateResponse(request,'writing.html',{'user':actor,'document':document,
            'versions':versions,'selected':selected,'body':selected.body_json if selected else None,
            'candidates':[v for v in versions if v.kind!='manual' and v.version!=document.selected_version][:3],
            'tasks':tasks,'writer_ready':writer_is_ready(profile),'recipe':RECIPES[document.recipe],
            'csrf_token':request.session['csrf'],'request_key':secrets.token_hex(16),'page':'create',
            'error':error,'values':values or {}},status_code=status_code)

    def message(exc):
        if isinstance(exc,ValidationError):
            return '请检查文案字段：'+ '、'.join('.'.join(map(str,e['loc'])) for e in exc.errors()[:5])
        return str(exc)

    @app.post('/writing')
    async def create(request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:return RedirectResponse('/login',303)
        form=await request.form();validate_csrf(request,form.get('csrf_token'))
        try:
            req=WritingRequest(idea=form.get('idea',''),recipe=form.get('recipe','story'),
                target_seconds=int(form.get('target_seconds',30)),aspect_ratio=form.get('aspect_ratio','16:9'))
            document=create_writing_document(session,actor,req)
            if form.get('intent')!='save':
                enqueue_writing(session,actor,document,req,expected_latest=0,request_key=secrets.token_hex(16))
            session.commit()
            return RedirectResponse(f'/writing/{document.id}',303)
        except ValueError as exc:
            session.rollback()
            profile=session.scalar(select(ModelProfile).where(ModelProfile.slug=='qwen-local'))
            return templates.TemplateResponse(request,'create.html',{'user':actor,'recipes':RECIPES,
                'writer_ready':writer_is_ready(profile),'page':'create','csrf_token':request.session['csrf'],
                'error':message(exc),'values':dict(form)},status_code=422)

    @app.get('/writing/{document_id}')
    def page(document_id:str,request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:return RedirectResponse('/login',303)
        return render(request,session,actor,owned(session,actor,document_id))

    @app.get('/writing/{document_id}/status')
    def status(document_id:str,request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:raise HTTPException(401,'请先登录')
        document=owned(session,actor,document_id)
        tasks=list(session.scalars(select(Task).where(Task.project_id==document.project_id,Task.task_type=='write_script').order_by(Task.created_at.desc()).limit(5)))
        return JSONResponse({'latest_version':document.latest_version,'selected_version':document.selected_version,
            'tasks':[{'status':t.status,'message':t.error_message or ''} for t in tasks]},headers={'Cache-Control':'no-store'})

    @app.post('/writing/{document_id}/generate')
    async def generate(document_id:str,request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:return RedirectResponse('/login',303)
        form=await request.form();validate_csrf(request,form.get('csrf_token'))
        document=owned(session,actor,document_id)
        try:
            base_version=int(form.get('expected_selected','-1'))
            base=get_writing_revision(session,document,base_version).body_json if base_version>0 else None
            req=WritingRequest(idea=document.idea,recipe=document.recipe,target_seconds=document.target_seconds,
                aspect_ratio=document.aspect_ratio,operation=form.get('operation','generate'),
                base_body=base,instruction=form.get('instruction',''))
            enqueue_writing(session,actor,document,req,expected_latest=int(form.get('expected_latest','-1')),
                request_key=form.get('request_key',''),expected_selected=base_version)
            session.commit()
            return RedirectResponse(f'/writing/{document.id}',303)
        except ValueError as exc:
            session.rollback()
            return render(request,session,actor,document,error=message(exc),status_code=409)

    @app.post('/writing/{document_id}/save')
    async def save(document_id:str,request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:return RedirectResponse('/login',303)
        form=await request.form();validate_csrf(request,form.get('csrf_token'))
        document=owned(session,actor,document_id)
        try:
            source=int(form.get('source_version','-1'));expected=int(form.get('expected_latest','-1'))
            original=get_writing_revision(session,document,source)
            body=WritingBody.model_validate(original.body_json).model_dump()
            for key in ('title','summary','script'):body[key]=form.get(key,'')
            for i,shot in enumerate(body['shots']):
                for key in ('title','visual','narration'):
                    field=f'shot_{i}_{key}'
                    if field in form:shot[key]=form[field]
            select_writing_revision(session,actor,document,source,expected_latest=expected)
            save_writing_revision(session,actor,document,WritingBody.model_validate(body),expected_latest=expected)
            session.commit()
            return RedirectResponse(f'/writing/{document.id}',303)
        except ValueError as exc:
            session.rollback()
            return render(request,session,actor,document,error=message(exc),status_code=409,values=dict(form))

    @app.post('/writing/{document_id}/select')
    async def select_version(document_id:str,request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:return RedirectResponse('/login',303)
        form=await request.form();validate_csrf(request,form.get('csrf_token'))
        document=owned(session,actor,document_id)
        try:
            select_writing_revision(session,actor,document,int(form.get('version','-1')),expected_latest=int(form.get('expected_latest','-1')))
            session.commit();return RedirectResponse(f'/writing/{document.id}',303)
        except ValueError as exc:
            session.rollback();return render(request,session,actor,document,error=message(exc),status_code=409)

    @app.post('/writing/{document_id}/publish')
    async def publish(document_id:str,request:Request,session:Session=Depends(session_dependency)):
        actor=html_user(request,session)
        if actor is None:return RedirectResponse('/login',303)
        form=await request.form();validate_csrf(request,form.get('csrf_token'))
        document=owned(session,actor,document_id)
        try:
            plan=publish_writing_revision(session,actor,document,int(form.get('version','-1')))
            session.commit();return RedirectResponse(f'/creations/{plan.id}',303)
        except ValueError as exc:
            session.rollback();return render(request,session,actor,document,error=message(exc),status_code=409)
