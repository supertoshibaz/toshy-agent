"""One task owns each transport, protocol session and its recovery loop."""
from __future__ import annotations
import asyncio
import inspect
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field as data_field
import uuid
from datetime import timedelta

from core.logger import core_logger
from .callbacks import Callbacks, field


def dump(value):
    return value.model_dump(by_alias=True, mode='json') if hasattr(value, 'model_dump') else value


def error_message(error):
    children = getattr(error, 'exceptions', None)
    return '\n'.join(dict.fromkeys(error_message(child) for child in children)) if children else str(error) or type(error).__name__


def timeout_value(method, name, seconds):
    annotation = inspect.signature(method).parameters[name].annotation
    return seconds if 'float' in str(annotation) else timedelta(seconds=seconds)


class ConnectionLost(RuntimeError):
    pass


@dataclass
class Job:
    name: str
    arguments: dict
    future: asyncio.Future
    context: object
    connection: object = None
    operation: str = 'call_tool'
    interactive: int = 0
    pause_seconds: float = 0
    call_id: str = data_field(default_factory=lambda: uuid.uuid4().hex)
    rpc_request_id: object = None


class ServerActor:
    def __init__(self, manager, owner, record):
        self.manager, self.owner, self.record = manager, owner, record
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.status, self.error = 'connecting', ''
        self.leases, self.calls = 0, 0
        self.stop_requested = False
        self.authorization_url = ''
        self.log_path = manager.store.path(owner, record['id']) / 'runtime' / 'stderr.log'
        self.active_job = None
        self.reserved = 0
        self.channel_key = None
        self.callbacks = Callbacks(self)
        self.retry_count = 0
        self.refresh_pending = False
        self.session = None
        self.connection_error = None
        self.task = asyncio.create_task(self.run())

    def fail_queue(self, message):
        while not self.queue.empty():
            job = self.queue.get_nowait()
            if isinstance(job, Job) and not job.future.done():
                job.future.set_exception(ConnectionLost(message))

    async def received(self, message):
        if isinstance(message, Exception):
            self.connection_error = ConnectionLost(error_message(message))
            self.queue.put_nowait(ConnectionLost(error_message(message)))
            return
        root = getattr(message, 'root', message)
        method = field(root, 'method', default='')
        if method in {'notifications/tools/list_changed', 'notifications/resources/list_changed', 'notifications/prompts/list_changed', 'notifications/skills/list_changed'} and not self.refresh_pending:
            self.refresh_pending = True
            # Queue a refresh: never issue RPCs inline from the SDK reader.
            self.queue.put_nowait('refresh')

    async def discover(self, session):
        tools, cursor = [], None
        capabilities = self.record.get('capabilities', {})
        while capabilities.get('tools') is not None:
            signature = inspect.signature(session.list_tools).parameters
            if cursor and 'params' in signature:
                from mcp.types import PaginatedRequestParams
                response = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            elif cursor:
                response = await session.list_tools(cursor=cursor)
            else:
                response = await session.list_tools()
            tools.extend(dump(x) for x in response.tools)
            cursor = field(response, 'next_cursor', 'nextCursor')
            if not cursor:
                break
        record = self.manager.store.read(self.owner, self.record['id'])
        record.update(tools=tools, error='')
        for capability, method, key in [('resources', 'list_resources', 'resources'),
                ('resources', 'list_resource_templates', 'resourceTemplates'), ('prompts', 'list_prompts', 'prompts')]:
            record[key] = []
            if capabilities.get(capability) is not None:
                try:
                    record[key] = (await self.list_all(session, method))[key]
                except Exception as error:
                    if method != 'list_resource_templates' or field(getattr(error, 'error', None), 'code') != -32601:
                        raise
        from .skills import discover
        try:
            candidates = await discover(session, record)
            from .skills import validate_entry
            record['skills'], errors = [], []
            for entry in candidates:
                try:
                    validate_entry(entry)
                    record['skills'].append(entry)
                except (ValueError, KeyError, TypeError) as error:
                    errors.append(str(error))
            record['skills_error'] = '\n'.join(errors)
        except Exception as error:
            record['skills'], record['skills_error'] = [], str(error)
        current = self.manager.store.read(self.owner, self.record['id'])
        current.update({key: record[key] for key in ('tools', 'resources', 'resourceTemplates', 'prompts', 'error', 'skills', 'skills_error')})
        self.manager.store.write(self.owner, current)
        self.record = current
        self.refresh_pending = False
        self.manager.publish()

    async def execute(self, session, job):
        self.active_job = job
        self.calls += 1
        task = None
        try:
            self.manager.check_tool(self.owner, self.record['id'], job.name, job.operation, job.context)
            if self.record.get('roots', True) and hasattr(session, 'send_roots_list_changed'):
                await session.send_roots_list_changed()
            if job.operation == 'call_tool':
                kwargs = {'arguments': job.arguments}
                if 'read_timeout_seconds' in inspect.signature(session.call_tool).parameters:
                    kwargs['read_timeout_seconds'] = timeout_value(session.call_tool, 'read_timeout_seconds', 1200)
                operation = session.call_tool(job.name, **kwargs)
            elif job.operation == 'skill_resource':
                operation = session.read_resource(job.arguments['uri'])
            elif job.operation in {'skill_get', 'skill_directory'}:
                from .skills import rpc
                operation = rpc(session, 'skills/get' if job.operation == 'skill_get' else 'resources/directory/read', job.arguments)
            elif job.operation == 'read_resource':
                operation = session.read_resource(job.arguments['uri'])
            elif job.operation == 'get_prompt':
                operation = session.get_prompt(job.arguments['name'], arguments=job.arguments.get('arguments'))
            else:
                operation = self.list_all(session, job.operation)
            task = asyncio.create_task(operation)
            started = time.monotonic()
            timeout = float(self.record.get('timeout', 60))
            while not task.done():
                await asyncio.wait({task}, timeout=0.2)
                if job.future.cancelled():
                    raise ConnectionLost('Вызов отменён; результат действия может быть неизвестен.')
                if self.connection_error:
                    raise self.connection_error
                if not job.interactive and time.monotonic() - started - job.pause_seconds > timeout:
                    raise ConnectionLost('Таймаут MCP. Результат действия может быть неизвестен; повтор автоматически не выполнялся.')
                if self.stop_requested and self.manager.access_reason(owner=self.owner):
                    raise ConnectionLost('Модуль отключён; выполнение MCP остановлено.')
            result = dump(task.result())
            if not job.future.done():
                job.future.set_result(result)
        except asyncio.CancelledError:
            if not job.future.done():
                job.future.set_exception(ConnectionLost('MCP-сервер остановлен; результат текущего действия может быть неизвестен.'))
            raise
        except Exception as error:
            message = self.manager.redact(error_message(error), self.owner, self.record['id'])
            if not job.future.done():
                job.future.set_exception(RuntimeError(message))
            # Protocol/tool failures leave a healthy session usable. Transport
            # loss and ambiguous timeouts restart it, never replaying the action.
            code = field(getattr(error, 'error', None), 'code')
            if isinstance(error, (ConnectionLost, EOFError, OSError)) or code in {-32000, -32001} and any(
                    word in message.casefold() for word in ('closed', 'connection', 'timeout', 'timed out')):
                raise ConnectionLost(message) from error
        finally:
            if task is not None and not task.done():
                if job.rpc_request_id is not None:
                    from mcp import types
                    try:
                        await asyncio.wait_for(session.send_notification(types.ClientNotification(types.CancelledNotification(
                            params=types.CancelledNotificationParams(requestId=job.rpc_request_id, reason='Toshy call cancelled')))), timeout=1)
                    except Exception:
                        pass
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for identity, entry in list(self.manager.interactions.items()):
                if entry['owner'] == self.owner and entry['server_id'] == self.record['id'] and entry.get('call_id') == job.call_id:
                    if not entry['future'].done():
                        entry['future'].cancel()
                    self.manager.interactions.pop(identity, None)
            self.active_job = None
            self.calls -= 1

    async def list_all(self, session, operation):
        method = getattr(session, operation)
        key = {'list_resources': 'resources', 'list_resource_templates': 'resourceTemplates',
               'list_prompts': 'prompts'}[operation]
        items, cursor = [], None
        while True:
            parameters = inspect.signature(method).parameters
            if cursor and 'params' in parameters:
                from mcp.types import PaginatedRequestParams
                result = await method(params=PaginatedRequestParams(cursor=cursor))
            elif cursor:
                result = await method(cursor=cursor)
            else:
                result = await method()
            data = dump(result)
            items.extend(data.get(key, []))
            cursor = data.get('nextCursor')
            if not cursor:
                return {key: items}

    async def connection(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from . import http_factory
        config = self.manager.launch(self.owner, self.record)
        timeout = float(self.record.get('timeout', 60))
        async with AsyncExitStack() as stack:
            self.connection_error = None
            if config['transport'] == 'stdio':
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                error_log = stack.enter_context(self.log_path.open('a', encoding='utf-8'))
                streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=config['command'], args=config['args'], env=config['env'], cwd=config['cwd']), errlog=error_log))
            else:
                from .oauth import provider
                auth = provider(self, config['url']) if self.record.get('oauth') else None
                if config['transport'] == 'sse':
                    transport = sse_client(config['url'], headers=config['headers'], auth=auth, httpx_client_factory=http_factory(config['url']))
                else:
                    try:
                        from mcp.client.streamable_http import streamable_http_client as http_transport
                    except ImportError:
                        from mcp.client.streamable_http import streamablehttp_client as http_transport
                    if 'headers' in inspect.signature(http_transport).parameters:
                        transport = http_transport(config['url'], headers=config['headers'], auth=auth, httpx_client_factory=http_factory(config['url']))
                    else:
                        http = await stack.enter_async_context(http_factory(config['url'])(headers=config['headers'], auth=auth))
                        transport = http_transport(config['url'], http_client=http)
                streams = await stack.enter_async_context(transport)
            kwargs = {'read_timeout_seconds': timeout_value(ClientSession, 'read_timeout_seconds', 1200),
                'message_handler': self.received}
            for enabled, name, callback in [('sampling', 'sampling_callback', self.callbacks.sampling),
                    ('elicitation', 'elicitation_callback', self.callbacks.elicitation), ('roots', 'list_roots_callback', self.callbacks.roots)]:
                if self.record.get(enabled, True):
                    kwargs[name] = callback
            actor = self
            class TrackedSession(ClientSession):
                async def send_request(self, request, result_type, *args, **kwargs):
                    root = getattr(request, 'root', request)
                    if actor.active_job is not None and getattr(root, 'method', '') in {
                            'tools/call', 'resources/read', 'resources/list', 'resources/templates/list', 'prompts/get', 'prompts/list'}:
                        actor.active_job.rpc_request_id = getattr(self, '_request_id', None)
                    return await super().send_request(request, result_type, *args, **kwargs)
            from .apps import session_class
            Session = session_class(TrackedSession, self.record.get('apps', True))
            session = await stack.enter_async_context(Session(streams[0], streams[1], **kwargs))
            self.session = session
            initialized = None
            if hasattr(session, 'discover'):
                try:
                    initialized = await asyncio.wait_for(session.discover(), timeout=timeout)
                except Exception as error:
                    if field(getattr(error, 'error', None), 'code') not in {-32601, -32022}:
                        raise
            if initialized is None:
                initialized = await asyncio.wait_for(session.initialize(), timeout=360 if self.record.get('oauth') else timeout)
            record = self.manager.store.read(self.owner, self.record['id'])
            record.update(instructions=field(initialized, 'instructions', default='') or '',
                capabilities=dump(field(initialized, 'capabilities')) or {})
            self.manager.store.write(self.owner, record)
            self.record = record
            await asyncio.wait_for(self.discover(session), timeout=timeout)
            self.status, self.error = 'connected', ''
            self.manager.publish()
            if not self.ready.done():
                self.ready.set_result(True)
            while True:
                try:
                    job = await asyncio.wait_for(self.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    await asyncio.wait_for(session.send_ping(), timeout=min(timeout, 10))
                    continue
                if job is None:
                    return
                if isinstance(job, ConnectionLost):
                    raise job
                if job == 'refresh':
                    await asyncio.wait_for(self.discover(session), timeout=timeout)
                elif isinstance(job, Job) and not job.future.cancelled():
                    await self.execute(session, job)

    async def run(self):
        try:
            while not self.stop_requested:
                started = time.monotonic()
                try:
                    self.record = self.manager.store.read(self.owner, self.record['id'])
                    self.status = 'connecting' if not self.retry_count else 'reconnecting'
                    await self.connection()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.session = None
                    self.refresh_pending = False
                    self.error = self.manager.redact(error_message(error), self.owner, self.record['id'])
                    # Unsent jobs stay queued across reconnect. execute() has
                    # already completed the sent job with an ambiguous failure.
                    core_logger.event('modules', 'MCP_CONNECT_ERROR', self.error, level='ERROR')
                    if time.monotonic() - started > 60:
                        self.retry_count = 0
                    if self.stop_requested or isinstance(error, ValueError) or self.manager.access_reason(owner=self.owner) or not self.record.get('reconnect', True) or self.retry_count >= int(self.record.get('reconnect_attempts', 5)):
                        self.status = 'error'
                        if not self.ready.done():
                            self.ready.set_result(False)
                        break
                    self.retry_count += 1
                    self.status = 'reconnecting'
                    self.manager.publish()
                    if not self.ready.done():
                        self.ready.set_result(True)
                    await asyncio.sleep(min(2 ** (self.retry_count - 1), 30))
        finally:
            self.session = None
            for state, future in list(self.manager.oauth_waiters.items()):
                if getattr(future, '_mcp_actor', None) is self:
                    self.manager.oauth_waiters.pop(state, None)
                    future.cancel()
            if not self.ready.done():
                self.ready.set_result(False)
            if self.status != 'error':
                self.status = 'stopped'
            self.fail_queue('Соединение MCP закрыто; ожидавший вызов не выполнен.')
            self.manager.publish()

    async def stop(self, force=False):
        self.stop_requested = True
        if not force and (self.leases or self.calls):
            return
        if not self.task.done():
            if force or self.status in {'connecting', 'authorizing', 'reconnecting'}:
                self.task.cancel()
            else:
                self.queue.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
