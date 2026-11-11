"""Manual compaction jobs share the ordinary session execution lock."""

import asyncio

from core.context import ExecutionContext, bind_execution_context
from core.context_compression import ContextCompressor
from core.context_archive import get_status
from core.paths import APP_PATHS

JOBS = {}


def compressed_context_preview(manager, session_id=None, branch="main"):
    """Read the applicable handoff without model calls, credentials or writes."""
    import datetime
    from core.context_archive import checkpoint
    from core.token_usage import session_connection

    branch = manager.clean_branch_name(branch or "main")
    result = dict(session_id=session_id or None, branch=branch, has_summary=False)
    heading = "# Сжатый контекст\n\n"
    if not session_id:
        result["markdown"] = heading + "Выберите сохранённую сессию.\n"
        return result
    if not manager.has_branch(session_id, branch):
        raise FileNotFoundError("Сессия или ветка не найдена.")
    messages, metadata, active = manager.load_session(session_id, branch=branch)
    connection = session_connection(manager.storage(session_id), active)
    if connection is None:
        result["markdown"] = (
            heading
            + "Подключение модели этой сессии недоступно. Выберите модель, чтобы определить применимое резюме.\n"
        )
        return result
    fingerprint = (
        connection.fingerprint
        + ":"
        + str(metadata.get("include_thoughts_in_context", True))
    )
    entry = checkpoint(manager.storage(session_id), active, "", fingerprint, messages)
    heading += f"Сессия: {session_id}\n\nВетка: {active}\n\n"
    if entry is None:
        result["markdown"] = heading + (
            "Для текущей истории и модели нет применимого сохранённого резюме. "
            "Сжатие ещё не выполнялось либо прежнее резюме перестало соответствовать истории после изменений.\n"
        )
        return result
    timestamp = (
        datetime.datetime.fromtimestamp(entry["created"])
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %z")
    )
    result.update(has_summary=True, compaction_id=entry["id"])
    result["markdown"] = (
        heading + f"Создано: {timestamp}\n\n---\n\n" + entry["summary"] + "\n"
    )
    return result


def cancel(session_id, branch):
    item = JOBS.get((session_id, branch))
    if item and not item.done():
        item.cancel()
        return True
    return False


async def start(core, manager, session_id, branch, lock):
    from core.context_archive import set_status
    key = (session_id, branch)
    if key in JOBS and not JOBS[key].done():
        raise ValueError('Сжатие уже запущено или ожидает завершения генерации.')
    store = manager.storage(session_id)
    queued = lock.locked()
    async def run():
        compressor = None
        try:
            await asyncio.to_thread(set_status, store, branch, '', status='queued', manual=True,
                message='Сжатие начнётся после завершения генерации.' if queued else 'Подготовка сжатия…')
            async with lock:
                asyncio.current_task().waiting_for_generation = False
                # Перечитываем историю после ожидания: завершившийся ответ тоже учитывается.
                if not manager.has_branch(session_id, branch):
                    return
                messages, metadata, active = await asyncio.to_thread(manager.load_session, session_id, branch=branch)
                effective = await asyncio.to_thread(manager.resolve_session_project_settings, session_id, metadata, branch=active)
                context = ExecutionContext.create(session_id=session_id, branch=active,
                    workspace_root=effective.get('workspace_path') or str(APP_PATHS.workspace),
                    include_thoughts_in_context=metadata.get('include_thoughts_in_context', True),
                    model_profile_id=metadata.get('model_profile_id', ''),
                    project_system_prompt=effective.get('system_prompt', ''),
                    allowed_modules=tuple(effective['modules']) if effective.get('modules') is not None else None)
                connection = await asyncio.to_thread(core.client.model_connection, context.model_profile_id or None)
                with bind_execution_context(context), core.client.bind_connection(connection):
                    compressor = ContextCompressor(core.client, context)
                    await compressor.apply(messages, force=True)
        except asyncio.CancelledError:
            if store.exists():
                await asyncio.to_thread(set_status, store, branch, '', status='cancelled', message='Сжатие отменено; история сохранена.')
        except Exception as error:
            if store.exists():
                await asyncio.to_thread(set_status, store, branch, '', status='error',
                    message='Сжатие не выполнено; история сохранена. ' + str(error))
        finally:
            if JOBS.get(key) is asyncio.current_task():
                JOBS.pop(key, None)

    task = asyncio.create_task(run())
    task.waiting_for_generation = queued
    JOBS[key] = task
    return dict(success=True, queued=queued)


async def snapshot(manager, session_id, branch):
    result = await asyncio.to_thread(get_status, manager.storage(session_id), branch)
    task = JOBS.get((session_id, branch))
    if task and not task.done():
        if getattr(task, 'waiting_for_generation', False):
            return dict(result, status='queued', manual=True, message='Сжатие начнётся после завершения генерации.')
        message = (
            result.get("message")
            if result.get("status") in {"queued", "running"}
            else "Сжатие контекста…"
        )
        result = dict(result, status=result.get("status") if result.get("status") in {"queued", "running"} else "running", manual=True, message=message)
    return result
