"""Авторство отслеживается до завершения команды, включая фоновое выполнение."""
import asyncio
import time
from core.file_history import get_history, HistoryBusy, _join_history_worker, _finish_thread, _report


async def begin_command(manager, invocation, context, owner='agent', workspace=None):
    store = get_history()
    label = 'Терминал: ' + invocation.get('command','')[:200]
    if owner=='agent' and context is not None:
        worker = asyncio.create_task(asyncio.to_thread(store.tool_begin,context,label))
    else:
        path = workspace or (context.workspace_root if context is not None else manager.store.workspace)
        worker = asyncio.create_task(asyncio.to_thread(store.user_begin,path))
    try:
        ticket,cancelled = await _join_history_worker(worker)
        if ticket:
            entries = getattr(manager,'_history_commands',None)
            if entries is None:manager._history_commands=entries={}
            entries[invocation['id']] = (store,ticket,context,owner,label)
        if cancelled:
            finish_command(manager,invocation)
            await wait_command(manager,invocation)
            raise asyncio.CancelledError
    except asyncio.CancelledError:raise
    except HistoryBusy:raise
    except Exception as error:_report(error)


def finish_command(manager, invocation):
    entry = getattr(manager,'_history_commands',{}).pop(invocation['id'],None)
    if entry is None:return
    store,ticket,context,owner,label = entry
    async def finish():
        try:
            if owner=='agent' and context is not None:
                await _finish_thread(store.tool_end,ticket,context,label)
            else:
                await _finish_thread(store.user_end,ticket,label)
        except Exception as error:_report(error)
    tasks = getattr(manager,'_history_tasks',None)
    if tasks is None:manager._history_tasks=tasks={}
    task = tasks[invocation['id']] = asyncio.create_task(finish())
    task.add_done_callback(lambda done:tasks.pop(invocation['id'],None))


async def wait_command(manager, invocation):
    task = getattr(manager,'_history_tasks',{}).get(invocation['id'])
    if task is not None:
        _,cancelled = await _join_history_worker(task)
        if cancelled:raise asyncio.CancelledError


async def run_job(manager, job, profile, context, runner):
    if job.get('terminal_shell'):
        await runner
        return
    try:
        await begin_command(manager,job,context,'agent' if job.get('source')=='agent' else 'user',profile.get('_workspace'))
        await runner
    except BaseException as error:
        if job.get('status')=='queued':
            job.update(status='cancelled' if isinstance(error,asyncio.CancelledError) else 'failed',
                       finished=time.time(),error=str(error))
            manager._save_job(job)
        raise
    finally:
        if hasattr(runner,'close'):runner.close()
        finish_command(manager,job)
        await wait_command(manager,job)


async def flush(manager):
    for identity in list(getattr(manager,'_history_commands',{})):
        finish_command(manager,{'id':identity})
    for identity in list(getattr(manager,'_history_tasks',{})):
        await wait_command(manager,{'id':identity})
