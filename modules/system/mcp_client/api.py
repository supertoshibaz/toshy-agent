"""HTTP surface owned by the MCP module; the core only dispatches requests."""
from fastapi import HTTPException
from fastapi.responses import HTMLResponse


def scope(manager, request, sessions):
    session_id = request.query_params.get('session_id')
    branch = request.query_params.get('branch', 'main')
    project_id = request.query_params.get('project_id')
    allowed = None
    if session_id:
        if not sessions.has_branch(session_id, branch):
            raise HTTPException(status_code=404, detail='Сессия или ветка не найдена.')
        meta, _ = sessions.load_session_metadata(session_id, branch=branch)
        allowed = sessions.resolve_session_project_settings(session_id, meta, branch=branch).get('modules')
    elif project_id:
        for folder in sessions._folder_chain(project_id):
            if folder['settings'].get('modules') is not None:
                allowed = folder['settings']['modules']
    return allowed, session_id, branch


async def handle_request(manager, request, path, sessions):
    if path == 'oauth/callback' and request.method == 'GET':
        state = request.query_params.get('state', '')
        future = manager.oauth_waiters.pop(state, None)
        if future is None or future.done():
            raise HTTPException(status_code=400, detail='OAuth state expired or unknown')
        code, error = request.query_params.get('code'), request.query_params.get('error')
        if error or not code:
            future.set_exception(ValueError('Авторизация отклонена'))
            return HTMLResponse('<p>Авторизация отклонена. Вернитесь в Toshy.</p>', status_code=400)
        future.set_result((code, state, request.query_params.get('iss')))
        return HTMLResponse('<p>Авторизация завершена. Можно вернуться в Toshy.</p>')
    allowed, session_id, branch = scope(manager, request, sessions)
    if not path and request.method == 'GET':
        return manager.catalog(allowed, session_id=session_id, branch=branch)
    # Server lifecycle/configuration belongs to its module, not to the active
    # chat. Apps and user responses remain bound to their execution context.
    parts = path.split('/')
    management = (len(parts) == 1 and request.method == 'POST'
        or len(parts) == 2 and request.method == 'PATCH'
        or len(parts) == 3 and request.method == 'POST' and parts[2] in {'start', 'stop', 'delete', 'refresh'})
    reason = manager.access_reason(None if management else allowed)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    if parts[0] == 'apps' and len(parts) in {2, 3}:
        try:
            entry = manager.apps.get(parts[1], session_id, branch, allowed)
            if len(parts) == 2 and request.method == 'GET':
                return await manager.apps.document(entry)
            if len(parts) == 3 and parts[2] == 'message' and request.method == 'POST':
                return await manager.apps.message(entry, await request.json())
            if len(parts) == 3 and parts[2] == 'close' and request.method == 'POST':
                await manager.apps.close(entry)
                return {'success': True}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        raise HTTPException(status_code=404, detail='Неизвестная операция приложения')
    owner = parts[0]
    identity = parts[1] if len(parts) > 1 else None
    try:
        manager.store.root(owner)  # Reject global owners for every mutation.
        if len(parts) == 1 and request.method == 'POST':
            body = await request.json()
            return await manager.add(owner, reference=body.get('reference', ''), name=body.get('name', ''),
                transport=body.get('transport', 'auto'), command=body.get('command', ''), args=body.get('args', []))
        if len(parts) == 2 and request.method == 'PATCH':
            return await manager.update(owner, identity, await request.json())
        if len(parts) == 4 and parts[2] == 'respond' and request.method == 'POST':
            return manager.respond(owner, identity, parts[3], await request.json(), session_id, branch)
        if len(parts) == 3 and request.method == 'POST':
            manager.store.read(owner, identity)
            action = parts[2]
            if action == 'start':
                reason = manager.access_reason(owner=owner)
                if reason:
                    raise ValueError(reason)
                await manager.start(owner, identity)
            elif action == 'stop':
                await manager.stop(owner, identity)
            elif action == 'delete':
                await manager.remove(owner, identity)
            elif action == 'refresh':
                actor = manager.actors.get((owner, identity))
                if not actor or actor.status != 'connected':
                    raise ValueError('Сначала подключите сервер.')
                if not actor.refresh_pending:
                    actor.refresh_pending = True
                    actor.queue.put_nowait('refresh')
            else:
                raise ValueError('Неизвестное действие MCP.')
            manager.registry.refresh_package_tools()
            return {'success': True}
        raise HTTPException(status_code=404, detail='Неизвестная операция модуля.')
    except HTTPException:
        raise
    except Exception as error:
        message = manager.redact(str(error), owner, identity) if identity else str(error)
        raise HTTPException(status_code=400, detail=message) from error
