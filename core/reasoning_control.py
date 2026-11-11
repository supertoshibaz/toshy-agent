"""Run-scoped final answers, tool gate, and optional native reasoning control."""
import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager, contextmanager, aclosing
from contextvars import ContextVar

import httpx

from core.context import get_execution_context

_active = {}
_runs = {}
_current_run_control = ContextVar('final_answer_control',default=None)


class FinalizationRequested(RuntimeError):
    """Stop a model/wait boundary, never an already running file operation."""


BLOCKED_TOOL = 'Tool not executed: the user requested a final answer without further actions.'


def contains_native_tools(value):
    if isinstance(value,dict):
        return (value.get('type') in ('tool_use','function_call')
            or any(value.get(key) for key in ('tool_calls','function_call','functionCall'))
            or any(contains_native_tools(item) for item in value.values()))
    return isinstance(value,list) and any(contains_native_tools(item) for item in value)


class RunReasoningControl:
    def __init__(self, context=None, on_finalize=None):
        self.stopped = False
        self.pending = None
        self.finalizing = False
        self.final_answer_started = False
        self.control_id = uuid.uuid4().hex
        self.parent_run_id = getattr(context, 'parent_run_id', '')
        self.run_id=getattr(context,'run_id','')
        current=_current_run_control.get()
        self.parent=current if current is not None and current.run_id==self.parent_run_id else None
        self.interrupt = asyncio.Event()
        self.alive = True
        self.on_finalize = on_finalize

    def status(self):
        return {'available': self.alive and not self.finalizing,
                'message': ('Формируется итоговый ответ. Новые инструменты запрещены.' if self.finalizing else
                            'Завершить работу: дождаться начатой операции и выдать один итоговый ответ без новых инструментов.'),
                'control_id': self.control_id, 'stopped_for_run': self.stopped,
                'finalizing': self.finalizing}


def finish_owner(context=None):
    context=context or get_execution_context()
    identity=context.run_id;parent=getattr(context,'parent_run_id','');seen=set()
    while identity and identity not in seen:
        seen.add(identity);state=_runs.get(identity)
        if state is not None and state.finalizing:return state
        identity=state.parent_run_id if state is not None else parent
        parent=''
    # A task spawned during the run retains this reference after the graph
    # exits. Finishing the graph must not reopen the tool gate for late tasks.
    state=_current_run_control.get()
    if state is not None and state.run_id in {context.run_id,getattr(context,'parent_run_id','')}:
        while state is not None:
            if state.finalizing:return state
            state=state.parent
    return None


def finish_requested(context=None):
    return finish_owner(context) is not None


def ensure_work_allowed(context=None):
    if finish_requested(context):raise FinalizationRequested(BLOCKED_TOOL)


@contextmanager
def reasoning_scope(context, on_finalize=None):
    state = _runs.get(context.run_id)
    owns = state is None
    if owns:state = _runs[context.run_id] = RunReasoningControl(context,on_finalize)
    token=_current_run_control.set(state)
    try:
        yield state
    finally:
        _current_run_control.reset(token)
        if owns and _runs.get(context.run_id) is state:
            state.alive=False
            _runs.pop(context.run_id, None)


async def _wait_finish(context):
    # The state's identity stays stable through tool passes. Descendants also
    # observe their ancestor's request, without affecting independent sessions.
    context=context or get_execution_context()
    states=[];identity=context.run_id;parent=getattr(context,'parent_run_id','');seen=set()
    while identity and identity not in seen:
        seen.add(identity);state=_runs.get(identity)
        if state is not None:states.append(state)
        identity=state.parent_run_id if state is not None else parent;parent=''
    if not states:await asyncio.Future()
    tasks=[asyncio.create_task(state.interrupt.wait()) for state in states]
    try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def interruptible(awaitable, context=None):
    """Only for model calls and human waits; never wrap tool/file execution."""
    task=asyncio.ensure_future(awaitable);stop=asyncio.create_task(_wait_finish(context))
    try:
        if finish_requested(context):raise FinalizationRequested(BLOCKED_TOOL)
        done,_=await asyncio.wait((task,stop),return_when=asyncio.FIRST_COMPLETED)
        if stop in done:raise FinalizationRequested(BLOCKED_TOOL)
        return await task
    finally:
        for pending in (task,stop):
            if not pending.done():pending.cancel()
        await asyncio.gather(task,stop,return_exceptions=True)


async def finalizable_stream(source,context=None):
    # One producer task owns the entire async generator. Advancing it in a new
    # task per token would invalidate ContextVar tokens in provider adapters.
    if finish_requested(context):
        await source.aclose()
        raise FinalizationRequested(BLOCKED_TOOL)
    queue=asyncio.Queue(maxsize=1)
    async def produce():
        try:
            async with aclosing(source):
                async for delta in source:await queue.put(('delta',delta))
        except asyncio.CancelledError:raise
        except BaseException as error:await queue.put(('error',error))
        else:await queue.put(('end',None))
    task=asyncio.create_task(produce());stop=asyncio.create_task(_wait_finish(context));read=None
    try:
        while True:
            read=asyncio.create_task(queue.get())
            done,_=await asyncio.wait((read,stop),return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                if read.done() and not read.cancelled():
                    kind,value=read.result()
                    if kind=='delta':yield value
                raise FinalizationRequested(BLOCKED_TOOL)
            kind,value=read.result();read=None
            if kind=='end':break
            if kind=='error':raise value
            yield value
    finally:
        for pending in (read,stop,task):
            if pending is not None and not pending.done():pending.cancel()
        await asyncio.gather(*(p for p in (read,stop,task) if p is not None),return_exceptions=True)


class CompletionControl:
    def __init__(self, connection, armed=False, run=None):
        self.connection = connection
        self.control_id = run.control_id if run is not None else uuid.uuid4().hex
        self.armed = armed
        self.completion_id = ''
        self.thinking = False
        self.requested = False
        self.active = True
        self.run = run
        self._last_status = None
        self._lock = asyncio.Lock()
        self._inline = None
        self.unavailable_reason = 'Сервер не поддерживает управление размышлениями во время генерации.'

    def status(self):
        if self.run is not None:return self.run.status()
        stopped = bool(self.run and self.run.stopped)
        available = bool(self.active and self.armed and self.completion_id and self.thinking and not self.requested and not stopped)
        if not self.active:
            reason = 'Нет активной генерации модели.'
        elif not self.armed:
            reason = self.unavailable_reason
        elif stopped:
            reason = 'Размышления отключены до конца текущего запуска, включая шаги после инструментов.'
        elif self.requested:
            reason = 'Команда перехода к ответу отправлена.'
        elif not self.completion_id:
            reason = 'Ожидание начала генерации модели.'
        elif not self.thinking:
            reason = 'Модель сейчас не генерирует размышления.'
        else:
            reason = 'Завершить текущие размышления и перейти к ответу.'
        return {'available': available, 'message': reason, 'control_id': self.control_id,
                'stopped_for_run': stopped}

    def change(self):
        status = self.status()
        if status == self._last_status:
            return None
        self._last_status = status
        return {'_reasoning_control': status}

    def observe(self, event):
        identity = event.get('id')
        if isinstance(identity, str) and identity:
            self.completion_id = identity
        for choice in event.get('choices') or []:
            delta = choice.get('delta') or {}
            if delta.get('reasoning_content') or delta.get('reasoning'):
                self.thinking = True
            if delta.get('content'):
                if self._inline is None:
                    from core.graph import _ThinkStreamFilter
                    self._inline = _ThinkStreamFilter()
                was_inline_thought = self._inline.in_thought
                events = self._inline.feed(delta['content'])
                for item in events:
                    if item['delta'].strip():
                        self.thinking = item['type'] == 'thought'
                if self._inline.in_thought:
                    self.thinking = True
                elif was_inline_thought:
                    self.thinking = False
            if delta.get('tool_calls') or choice.get('finish_reason') is not None:
                self.thinking = False
        return self.change()

    async def stop(self):
        if self.run is None or not self.run.alive:
            return {'success':False,'message':'Нет активного запуска.'}
        return await _finish_run(self.run,self)


async def _finish_run(run,control=None):
    if run.finalizing:return {'success':True,'message':'Запуск уже завершается.'}
    run.finalizing=True;run.stopped=True
    pending=asyncio.get_running_loop().create_future();run.pending=pending
    native=False
    try:
        if run.on_finalize is not None:
            result=run.on_finalize()
            if inspect.isawaitable(result):await result
        if control is not None:
            control.requested=True
            if control.armed and control.active and control.completion_id and control.thinking:
                connection=control.connection
                try:
                    async with httpx.AsyncClient(timeout=5,headers=connection.headers(),
                            trust_env=connection.options.get('trust_env',True)) as http:
                        response=await http.post(connection.base_url.rstrip('/')+'/chat/completions/control',
                            json={'id':control.completion_id,'action':'reasoning_end','model':connection.model})
                    native=response.is_success and response.json().get('success') is True
                except (httpx.HTTPError,ValueError,AttributeError):pass
    finally:
        run.interrupt.set()
        if not pending.done():pending.set_result(None)
        if run.pending is pending:run.pending=None
    return {'success':True,'native_control':native,
            'message':'Новые инструменты запрещены. После завершения начатой операции будет выдан один итоговый ответ.'}


@asynccontextmanager
async def completion_control(client, connection, payload, *, enabled=False):
    context = get_execution_context()
    cached = getattr(client, '_compatibility_cache', {}).get((connection.fingerprint, connection.key))
    detected = bool(cached and cached[1].get('_llama_cpp'))
    detected |= getattr(client, '_continuation_server_info', {}).get(
        (connection.base_url, connection.model), {}).get('backend') == 'llama.cpp'
    armed = bool(enabled and connection.protocol == 'chat_completions'
                 and detected and context.run_id != 'default')
    run = _runs.get(context.run_id)
    if enabled and run is not None and run.pending is not None:
        await asyncio.shield(run.pending)
    control = CompletionControl(connection, armed, run)
    if armed:
        payload['reasoning_control'] = True
        if run is not None and run.stopped:
            # These are llama.cpp request controls, not display filters. Keep
            # reasoning_format intact: every token actually received is shown.
            payload['reasoning_effort'] = 'none'
            payload['chat_template_kwargs'] = {
                **(payload.get('chat_template_kwargs') or {}), 'enable_thinking': False}
    owner=finish_owner(context)
    if owner is not None and owner.final_answer_started:
        for key in ('tools','functions','function_call','parallel_tool_calls','toolConfig'):
            payload.pop(key,None)
        if connection.protocol in {'chat_completions','responses'} and connection.options.get('compat_tools')!='off':payload['tool_choice']='none'
        else:payload.pop('tool_choice',None)
        if detected:
            payload['reasoning_effort']='none'
            payload['chat_template_kwargs']={**(payload.get('chat_template_kwargs') or {}),'enable_thinking':False}
    # Internal compression/model helpers must not replace the visible run's
    # completion. Child agents use their own run ID and cannot affect the parent.
    registered = enabled and context.run_id != 'default' and context.run_id not in _active
    if registered:
        _active[context.run_id] = control
    try:
        yield control
    finally:
        control.active = False
        if registered and _active.get(context.run_id) is control:
            _active.pop(context.run_id, None)


async def stop_thinking(run_id, control_id=None):
    run=_runs.get(run_id)
    if run is None or not run.alive:
        return {'success':False,'message':'Сейчас нет активного запуска.'}
    if control_id is not None and control_id!=run.control_id:
        return {'success':False,'message':'Этот запуск уже завершён.'}
    result=await _finish_run(run,_active.get(run_id))
    return {**result,'control':run.status()}
