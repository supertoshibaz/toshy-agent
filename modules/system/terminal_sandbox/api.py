"""Authenticated by the existing core module-service route."""
from fastapi import HTTPException


async def handle_request(manager, request, path):
    path, method = path.strip('/'), request.method
    try:
        if method == 'GET' and not path:
            try:await manager.prepare_shared_workspace()
            except (RuntimeError,OSError,ValueError) as error:
                manager.container_errors['shared']=str(error) or 'Docker не отвечает.'
            manager.schedule_state_refresh()
            return manager.catalog(request.query_params.get('session_id', ''), request.query_params.get('project_id', ''), request.query_params.get('workspace'))
        if method == 'GET' and path == 'images':
            return {'images': await manager.images()}
        if method == 'GET' and path.startswith('jobs/'):
            return manager.output(path.split('/')[1], int(request.query_params.get('offset', 0)))
        body = await request.json() if method in {'POST', 'PATCH'} else {}
        if (method=='POST' and path in {'terminal-open','jobs','complete'}
                and body.get('profile_id','shared')=='shared'):
            await manager.prepare_shared_workspace()
        if method=='POST' and path=='terminal-feed':
            profile_id=str(body.get('profile_id','shared'))
            if body.get('ensure_default') and not manager.terminal_list(profile_id):
                context=manager.project_context(str(body.get('session_id') or ''),str(body.get('project_id') or ''),body.get('workspace'))
                manager.terminal_open(manager.store.get(profile_id),context,owner='user',project_id=str(body.get('project_id') or ''))
            await manager.wait_feed(body.get('revision'),body.get('wait',20))
            return manager.terminal_feed(profile_id,body.get('terminal_session_id'),body.get('cursors'))
        if method=='POST' and path=='terminal-open':
            context=manager.project_context(str(body.get('session_id') or ''),str(body.get('project_id') or ''),body.get('workspace'))
            return manager.terminal_open(manager.store.get(body.get('profile_id','shared')),context,owner='user',project_id=str(body.get('project_id') or ''))
        if method=='POST' and path=='terminal-complete':
            manager.check_manual_input()
            import uuid,time
            tab=manager.terminal_get(body.get('terminal_session_id'),profile_id=body.get('profile_id'))
            if manager.terminal_public(tab)['state'] not in {'ready','stopped'}:raise ValueError('Вкладка занята программой.')
            context=manager._terminal_context(tab)
            job={'id':uuid.uuid4().hex,'profile_id':tab['profile_id'],'profile_name':'Tab','hidden':True,
                 'source':'manual','command':'Tab','cwd':tab['cwd'],'completion_cwd':tab['cwd'],'bytes':0,
                 'status':'starting','created':time.time(),'session_id':tab['chat_session_id'],'run_id':context.run_id}
            try:return await manager._complete(manager.store.get(tab['profile_id']),str(body.get('line','')),int(body.get('position',0)),context,job)
            finally:
                for suffix in ('.json','.out'):(manager.store.root/'jobs'/(job['id']+suffix)).unlink(missing_ok=True)
        if method=='POST' and path in {'terminal-command','terminal-input','terminal-close'}:
            identity=str(body.get('terminal_session_id') or '')
            tab=manager.terminal_get(identity,profile_id=str(body.get('profile_id') or ''))
            if path=='terminal-close':return await manager.terminal_close(identity,profile_id=tab['profile_id'])
            if path=='terminal-input':return await manager.terminal_input(identity,body.get('text',''),key=body.get('key'),profile_id=tab['profile_id'])
            return await manager.terminal_execute(body.get('command',''),manager._terminal_context(tab),identity=identity,
                owner='user',profile_id=tab['profile_id'],yield_seconds=0)
        if method == 'POST' and path == 'feed':
            identity = str(body.get('profile_id', 'shared'))
            await manager.wait_feed(body.get('revision'), body.get('wait', 20))
            result = manager.feed(identity, body.get('cursors'), body.get('lines', 10000), body.get('states'))
            context = manager.project_context(str(body.get('session_id') or ''), str(body.get('project_id') or ''), body.get('workspace'))
            result['cwd'] = manager.current_directory(manager.store.get(identity), context)
            return result
        if method == 'POST' and path == 'profiles':
            result = await manager.save_profile(body)
            if 'ssh_password' in body:
                from .ssh_support import password
                password(manager.store.owner + ':' + result['id'], str(body['ssh_password']))
            return result
        if method == 'POST' and path.startswith('approval/'):
            manager.policy.answer(path.split('/')[1], body.get('decision'))
            return {'ok': True}
        if method == 'POST' and path == 'credentials':
            from .ssh_support import password
            profile = manager.store.get(body['profile_id'])
            if profile['backend'] != 'ssh':
                raise ValueError('Учётные данные относятся только к SSH.')
            password(manager.store.owner + ':' + profile['id'], str(body.get('password','')))
            return {'ok': True}
        if method == 'POST' and path == 'complete':
            import asyncio
            context = manager.project_context(str(body.get('session_id') or ''), str(body.get('project_id') or ''), body.get('workspace'))
            task = asyncio.create_task(manager.complete(manager.store.get(body.get('profile_id', 'shared')),
                                          str(body.get('line', '')), int(body.get('position', 0)), context))
            try:
                while not task.done():
                    await asyncio.wait([task],timeout=.25)
                    if not task.done() and hasattr(request,'is_disconnected') and await request.is_disconnected():
                        task.cancel();await asyncio.gather(task,return_exceptions=True)
                        return {'start':int(body.get('position',0)),'length':0,'items':[]}
                return await task
            finally:
                if not task.done():
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
        if method == 'POST' and path == 'forget-host':
            import json
            from .store import write_json
            profile = manager.store.get(body['profile_id']); manager.check_idle(profile['id'])
            path = manager.store.root / 'ssh-hosts.json'
            hosts = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            hosts.pop(f"{profile['ssh_host']}:{profile['ssh_port']}", None); write_json(path, hosts)
            return {'ok': True}
        if method == 'POST' and path == 'jobs':
            profile = manager.store.get(body.get('profile_id', 'shared'))
            context = manager.project_context(str(body.get('session_id') or ''), str(body.get('project_id') or ''), body.get('workspace'))
            return manager.submit(profile,str(body.get('command','')),timeout=body.get('timeout'),source='manual',context=context,
                                  background=bool(body.get('is_background')),interactive=bool(body.get('interactive')),
                                  tty=bool(body.get('tty')),columns=body.get('columns',100),rows=body.get('rows',30))
        if method == 'POST' and path.startswith(('input/','resize/')):
            identity=path.split('/')[1]
            job=manager.jobs.get(identity)
            if not job or job['profile_id']!=str(body.get('profile_id','')):
                raise ValueError('Задание не принадлежит выбранному терминалу.')
            if manager.reservations.get(job['profile_id']):
                raise ValueError('Агент использует терминал. Дождитесь завершения его запуска.')
            if path.startswith('resize/'):
                return await manager.resize_process(identity,body.get('columns',100),body.get('rows',30))
            return await manager.write_input(identity,body.get('text',''),key=body.get('key'),
                                             close_stdin=bool(body.get('close_stdin')),source='manual')
        if method == 'POST' and path.startswith('cancel/'):
            return await manager.cancel(path.split('/')[1])
        if method == 'POST' and path.startswith('control/'):
            if body.get('action') in {'delete', 'reset'} and body.get('confirm') is not True:
                raise ValueError('Требуется подтверждение удаления внутреннего состояния контейнера.')
            await manager.control(path.split('/')[1], body.get('action'))
            return {'ok': True}
        if method == 'POST' and path == 'images/import':
            return await manager.import_image(body.get('path', ''))
        if method == 'POST' and path == 'rename-session':
            manager.store.rename_session(str(body['old']), str(body['new']))
            return {'ok': True}
        if method == 'POST' and path == 'app-close':
            await manager.shutdown(app_close=True)
            return {'ok': True}
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, str(error)) from error
    except (RuntimeError, OSError) as error:
        raise HTTPException(503, str(error)) from error
    raise HTTPException(404, 'Неизвестная операция терминала.')
