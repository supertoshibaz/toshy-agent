"""Отображаемые названия независимы от ID, путей, веток и журналов запусков."""
import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import replace
from langchain_core.messages import HumanMessage, SystemMessage
from core.context import bind_execution_context
from core.logger import core_logger
from core.model_queue import model_slot
from core.sqlite_storage import SQLiteStore
from core.token_usage import usage_scope

PREFIX = 'conversation_title:'
_jobs = {}
_all_jobs = set()


def generated_title(text):
    """Проверяем целый заголовок; лишние слова никогда не отрезаем."""
    lines = re.sub(r'<think>[\s\S]*?</think>', '', str(text or '')).strip().splitlines()
    if len(lines) != 1:
        return ''
    line = lines[0].strip(' "\'`#')
    line = re.sub(r'^(?:title|headline|название|заголовок)\s*:\s*', '', line, flags=re.I)
    line = re.sub(r'^(?:[-*•]+|\d+[.)])\s+', '', line)
    words = line.split()
    title = ' '.join(words).rstrip(' .,;:!?…—-').strip(' "\'`')
    return title if 2 <= len(title.split()) <= 5 and len(title) <= 160 else ''


async def _generate_title(client, connection, context, prompt):
    instruction = (
        'Generate a TASK DESCRIPTION as a complete, self-contained headline of 2-5 words, '
        'in the language of the user request. Compose it within that length from the start. '
        'Use a short noun phrase, not a sentence or an unfinished phrase. '
        'Name what the user wants to do or learn. Do not answer the request, introduce yourself, '
        'or use a bare entity/model/product name as the title. '
        'For example: "Расскажи о себе" -> "Уточнение информации о Тоши"; '
        '"Что такое Docker?" -> "Знакомство с Docker и контейнерами"; '
        '"Исправь ошибку запуска" -> "Исправление ошибки запуска приложения". '
        'Return ONLY one task headline, without quotes, labels, explanations, tools or reasoning.')
    for attempt in range(3):
        request = prompt
        if attempt:
            request += ('\n\nThe previous attempt did not meet the headline requirements. '
                        'Write a NEW complete task headline using only 2-5 words. '
                        'Rephrase the task more concisely; do not truncate a longer phrase. '
                        'Count the words before returning the single headline.')
        # Повтор тоже уступает место обычному запросу и не идёт параллельно с ним.
        async with model_slot(connection, auxiliary=True):
            with client.bind_connection(connection), bind_execution_context(context), usage_scope('session_title'):
                response = await client.chat_completion([
                    SystemMessage(content=instruction), HumanMessage(content=request)],
                    session_id=context.session_id)
        choices = response.get('choices') or []
        message = (choices[0].get('message') or {}) if choices else response.get('message') or {}
        text = message.get('content') or response.get('content') or ''
        title = generated_title(text)
        if title:
            return title
    core_logger.warn('SESSION_TITLE', 'The model did not produce a complete 2–5 word title after three attempts; the previous title was retained.')
    return ''


def titles(store):
    if not store.exists():return {}
    with store.connection() as db:
        return {key[len(PREFIX):]: json.loads(value) for key,value in
                db.execute('SELECT key,value FROM state WHERE key LIKE ?', (PREFIX+'%',))}


def manual_title(manager, session_id, title, branch='main'):
    title = re.sub(r'\s+', ' ', str(title or '')).strip()[:160]
    if not title:raise ValueError('Название не должно быть пустым.')
    store = manager.storage(session_id)
    with store.connection(write=True, create=False) as db:
        if not db.execute('SELECT 1 FROM branches WHERE name=?',(branch,)).fetchone():
            raise ValueError('Ветка не найдена.')
        SQLiteStore._set_state(db, PREFIX+branch, {'title':title,'manual':True})
    return {'success':True,'new_session_id':session_id,'branch':branch,'title':title}


def _first_user(db, branch):
    row = db.execute('SELECT payload FROM branches WHERE name=?',(branch,)).fetchone()
    if not row:return None
    header=json.loads(row[0])
    start=0 if branch=='main' else int(header.get('fork_message_index') or 0)
    row=db.execute("SELECT payload FROM messages WHERE branch=? AND position>=? "
                   "AND json_extract(payload,'$.type')='human' ORDER BY position LIMIT 1",(branch,start)).fetchone()
    return json.loads(row[0])['data'] if row else None


def _fingerprint(data):
    return hashlib.sha256(json.dumps([data.get('content'), data.get('additional_kwargs',{}).get('attachments')],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _reserve(manager, context, input_messages):
    store=manager.storage(context.session_id)
    with store.connection(write=True, create=False) as db:
        existing=SQLiteStore._state(db,PREFIX+context.branch,{})
        # Старые сессии с явно заданным ID тоже не переименовываем автоматически.
        if existing.get('manual') or (context.branch=='main' and not existing and
                                     not re.fullmatch(r'session_\d+',context.session_id)):
            return None
        first=_first_user(db,context.branch)
        last=next((m for m in reversed(input_messages) if isinstance(m,HumanMessage)),None)
        if not first or last is None:return None
        identity=first.get('additional_kwargs',{}).get('chat_id')
        same_request = (identity and identity == last.additional_kwargs.get('chat_id')) or _fingerprint(first) == _fingerprint(last.model_dump())
        # A failed first attempt must not leave a conversation unnamed forever.
        # Retry on a later ordinary answer while the factory title remains.
        needs_title = not existing.get('title') or existing.get('default')
        if not same_request and not needs_title:return None
        if last.additional_kwargs.get('task_status_check'):return None
        if (existing.get('title') and not existing.get('default')
                and existing.get('source_fingerprint') == _fingerprint(first)):
            return None
        if first.get('additional_kwargs',{}).get('task_status_check'):return None
        if existing.get('source_run')==context.run_id and existing.get('title'):return None
        token=uuid.uuid4().hex
        SQLiteStore._set_state(db,PREFIX+context.branch,{**existing,'pending':token})
        return token,_fingerprint(first),first


def _apply(manager, context, token, fingerprint, title):
    store=manager.storage(context.session_id)
    with store.connection(write=True,create=False) as db:
        current=SQLiteStore._state(db,PREFIX+context.branch,{})
        first=_first_user(db,context.branch)
        if current.get('manual') or current.get('pending')!=token or not first or _fingerprint(first)!=fingerprint:
            return False
        SQLiteStore._set_state(db,PREFIX+context.branch,{'title':title,'manual':False,
            'source_run':context.run_id,'source_fingerprint':fingerprint})
        return True


def schedule_title(client, manager, context, input_messages):
    if not context.session_id or context.task_status_check:return
    key=(context.session_id,context.branch)
    previous = _jobs.get(key)
    if previous is not None and not previous.done():
        return

    async def generate():
        try:
            reservation=await asyncio.to_thread(_reserve,manager,context,input_messages)
            if reservation is None:return
            token,fingerprint,first=reservation
            connection=await asyncio.to_thread(client.model_connection,context.model_profile_id or None)
            source=str(first.get('content') or '')
            # В заголовок не отправляется большой вставленный код или весь документ.
            source=re.sub(r'```[\s\S]*?```',' [attached text/code] ',source)
            source=source[:2000]+('\n…\n'+source[-2000:] if len(source)>4000 else source[2000:])
            names=[a.get('filename','') for a in first.get('additional_kwargs',{}).get('attachments',[])]
            prompt='First user request:\n'+source+'\nAttached files: '+', '.join(names)
            # Сохраняем лимиты профиля: маленький max_tokens обрывает reasoning-модели
            # до самого заголовка и может конфликтовать с их thinking budget.
            options=dict(connection.options)
            if connection.mode=='local' and connection.protocol=='chat_completions':
                extra=dict(options.get('extra_body') or {})
                extra['chat_template_kwargs']={**(extra.get('chat_template_kwargs') or {}),'enable_thinking':False}
                options['extra_body']=extra
            connection=replace(connection,options=options)
            title=await _generate_title(client,connection,context,prompt)
            if title:await asyncio.to_thread(_apply,manager,context,token,fingerprint,title)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            core_logger.warn('SESSION_TITLE',str(error))
        finally:
            if _jobs.get(key) is asyncio.current_task():_jobs.pop(key,None)
    job=asyncio.create_task(generate())
    _jobs[key] = job
    _all_jobs.add(job)
    job.add_done_callback(_all_jobs.discard)


async def shutdown():
    jobs=list(_all_jobs)
    for job in jobs:job.cancel()
    await asyncio.gather(*jobs,return_exceptions=True)
    _jobs.clear()
