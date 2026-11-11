"""Authenticated project history endpoints; roots come from the project catalog."""
import asyncio
import base64

from fastapi import HTTPException, Request
from core.file_history import get_history, HistoryError


def install(app, sessions, default_workspace, registry=None, active_runs=None):
    @app.post('/v1/file-origin')
    async def file_origin(request: Request):
        body = await request.json()
        try:
            from core.file_origins import origin
            path, oid = str(body.get('path') or ''), str(body.get('oid') or '')
            model = await asyncio.to_thread(origin, path, oid)
            if model is not None:
                return {'model': model}
            return await asyncio.to_thread(get_history().file_origin, path, oid)
        except (HistoryError, OSError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/v1/backups/{action}')
    async def backup_action(action: str, request: Request):
        from core import backups
        body=await request.json()
        if not isinstance(body,dict):raise HTTPException(400,'Ожидается объект JSON.')
        if action=='status':
            return await asyncio.to_thread(lambda:dict(settings=backups.settings(),items=backups.list_backups()))
        if action=='settings':
            return await asyncio.to_thread(backups.configure,body)
        if action!='create':raise HTTPException(404,'Операция не найдена.')
        if (getattr(registry,'_active_executions',0) or active_runs and active_runs()
                or getattr(app.state,'backup_mutations',0)):
            raise HTTPException(409,'Дождитесь завершения всех запусков перед резервным копированием.')
        # Block new mutating API calls until the data archive is published.
        app.state.data_backup=True
        worker=asyncio.create_task(asyncio.to_thread(backups.create))
        try:
            try:return await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
        except (HistoryError,OSError,ValueError) as error:
            raise HTTPException(409,str(error)) from error
        finally:app.state.data_backup=False

    def resolve(project):
        folders={f['id']:f for f in sessions.get_project_folders()}
        seen=set();folder=folders.get(project)
        while folder and folder.get('parent_id') and folder['id'] not in seen:
            seen.add(folder['id']);folder=folders.get(folder['parent_id'])
        if not folder:
            raise HistoryError('Проект не найден.')
        root=(folder.get('settings') or {}).get('workspace_path') or default_workspace
        store=get_history()
        return store,store.bind(folder['id'],root)

    def operation(project,action,body):
        store,workspace=resolve(project)
        if action=='status':
            return store.status(workspace)
        if action=='settings':
            return store.configure(workspace,body)
        if action=='list':
            return dict(items=store.list(workspace,str(body.get('session') or ''),str(body.get('path') or '')))
        if action=='snapshot':
            return dict(id=store.snapshot(workspace,str(body.get('label') or 'Контрольная точка')))
        if action=='file':
            value=store.file(workspace,str(body.get('snapshot') or ''),str(body.get('path') or ''),bool(body.get('current')))
            for key in ('before','after'):
                raw=value[key]
                value[key+'_size']=len(raw)
                value[key+'_truncated']=len(raw)>2*1024*1024
                value[key]=base64.b64encode(raw[:2*1024*1024]).decode('ascii')
            return value
        if action=='plan':
            return store.plan(workspace,str(body.get('snapshot') or ''),str(body.get('mode') or 'undo'),str(body.get('path') or ''))
        if action=='restore':
            return store.restore(workspace,str(body.get('token') or ''),bool(body.get('skip_conflicts',True)))
        if action=='gc':
            store.gc(workspace);return dict(success=True)
        raise HistoryError('Неизвестная операция истории.')

    @app.post('/v1/file-history/{project}/{action}')
    async def history_action(project: str, action: str, request: Request):
        body=await request.json()
        if not isinstance(body,dict):
            raise HTTPException(400,'Ожидается объект JSON.')
        try:
            return await asyncio.to_thread(operation,project,action,body)
        except (HistoryError,OSError,ValueError) as error:
            raise HTTPException(409,str(error)) from error
