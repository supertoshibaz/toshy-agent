"""Selected pipeline exports and durable external run records."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from dataclasses import replace

from core.context import ExecutionContext
from core.execution import CoreExecution
from core.paths import APP_PATHS
from .descriptor import MODULE_ID, DISPLAY_NAME, COLOR

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
RESERVED = {'ask_toshy', 'get_run', 'get_run_events', 'cancel_run'}


def write_json(path, data):
    from modules.pipelines.storage import save_layout
    save_layout(path, data)


class CoreToshyMcp:
    module_id, display_name, color, required = MODULE_ID, DISPLAY_NAME, COLOR, True

    def __init__(self, agent, sessions, root=None):
        self.agent, self.sessions = agent, sessions
        self.execution = CoreExecution(agent)
        self.root = Path(root or APP_PATHS.runtime / 'toshy-mcp')
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / 'settings.json'
        self.tasks = {}
        self.records = {}
        self.requests = {}
        self.transport_error = ''
        self.token = self._token()
        try:
            self.settings = self._validate(json.loads(self.settings_path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError, KeyError) as error:
            if not isinstance(error, FileNotFoundError):
                self.transport_error = 'Toshy MCP выключен: настройки не удалось загрузить. ' + str(error)
            self.settings = {'enabled': False, 'agent_enabled': False, 'exports': [],
                'workspace_root': str(APP_PATHS.workspace), 'model_profile_id': '', 'modules': []}
        for path in self.root.glob('runs/*/run.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['status'] not in TERMINAL:
                    record.update(status='interrupted', error='Ядро было остановлено. Действия автоматически не повторялись.')
                    write_json(path, record)
                self.records[record['run_id']] = record
                if record.get('request_key'):
                    self.requests[record['request_key']] = (record['fingerprint'], record['run_id'])
            except (OSError, ValueError, KeyError):
                continue

    def _token(self):
        path = self.root / 'server.token'
        try:
            token = path.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            token = ''
        if not token:
            token = secrets.token_urlsafe(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(token)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token

    def _validate(self, data):
        if not isinstance(data, dict):
            raise ValueError('Настройки должны быть объектом')
        result = copy.deepcopy(data)
        for key in ('enabled', 'agent_enabled'):
            if not isinstance(result.get(key, False), bool):
                raise ValueError(key + ': нужен Boolean')
            result.setdefault(key, False)
        modules = result.get('modules', [])
        if not isinstance(modules, list) or not all(isinstance(x, str) for x in modules):
            raise ValueError('modules: нужен список модулей')
        result['modules'] = sorted(set(modules))
        root = Path(result.get('workspace_root') or APP_PATHS.workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('Project Folder не существует')
        result['workspace_root'] = str(root)
        result['model_profile_id'] = str(result.get('model_profile_id') or '')
        exports = result.get('exports', [])
        if not isinstance(exports, list):
            raise ValueError('exports: нужен список')
        names, refs = set(RESERVED), set()
        for item in exports:
            if not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', item.get('name', '')):
                raise ValueError('Публичное имя: латинские буквы, цифры, подчёркивания, до 64 символов')
            if item['name'] in names or item.get('pipeline') in refs:
                raise ValueError('Публичные имена и ссылки на графы должны быть уникальны')
            names.add(item['name']); refs.add(item.get('pipeline'))
            ref = str(item.get('pipeline') or '')
            if ref.count('/') != 1:
                raise ValueError('Неверная ссылка на пайплайн')
            if not isinstance(item.get('enabled', True), bool):
                raise ValueError('enabled: нужен Boolean')
            item['enabled'] = item.get('enabled', True)
            item['description'] = str(item.get('description') or '')
        result['exports'] = exports
        return result

    def configure(self, data):
        settings = self._validate(data)
        write_json(self.settings_path, settings)
        self.settings = settings
        if self.transport_error.startswith('Toshy MCP выключен: настройки'):
            self.transport_error = ''
        return self.configuration()

    def snapshot(self):
        return {'name': self.module_id, 'display_name': self.display_name, 'color': self.color,
            'category': 'Ядро', 'required': True, 'enabled': True, 'tools': [],
            'service_enabled': self.settings['enabled']}

    def configuration(self):
        from core.model_connections import load_settings
        settings = load_settings()
        profiles = settings.get('connections', [])
        if isinstance(profiles, dict):
            profiles = [dict(value, id=key) for key, value in profiles.items()]
        return {'settings': self.settings, 'url': 'http://127.0.0.1:5000/mcp/', 'token': self.token,
            'error': self.transport_error, 'modules': self.agent.registry.get_modules_status(),
            'profiles': [{'id': p.get('id', ''), 'name': p.get('name', p.get('id', ''))} for p in profiles],
            'pipelines': self.catalog()}

    def catalog(self):
        from modules.pipelines.storage import load_pipeline_script
        from modules.pipelines.contracts import interface, schema_for, validate
        store = self.agent.pipeline_service.store
        allowed = set(self.settings['modules']) & set(self.agent.registry.loaded_modules)
        result = []
        for module in store.list():
            for pid, item in module.get('pipelines', {}).items():
                entry = {'pipeline': module['id'] + '/' + pid, 'name': item.get('name', pid),
                    'module': module['id'], 'module_name': module.get('name', module['id']), 'errors': []}
                try:
                    definition = load_pipeline_script(store.resolve(module['id'], pid) / 'pipeline.py')
                    ins, outs = interface(definition)
                    errors, _, _ = validate(definition, tools=self.agent.registry.tools, enabled=allowed,
                        resolve_call=lambda ref, mid=module['id']: store.internal_definition(mid, ref), stack=(pid,))
                    if module['id'] not in allowed:
                        errors.insert(0, 'Модуль-владелец выключен или не выбран для MCP')
                    entry.update(inputSchema=schema_for(ins), outputSchema=schema_for(outs), errors=errors)
                except Exception as error:
                    entry['errors'] = [str(error)]
                result.append(entry)
        return result

    def tool_specs(self):
        if not self.settings['enabled']:
            return []
        catalog = {x['pipeline']: x for x in self.catalog()}
        tools = []
        envelope = {'type': 'object', 'properties': {'run_id': {'type': 'string'},
            'session_id': {'type': 'string'}, 'status': {'type': 'string'}},
            'required': ['run_id', 'session_id', 'status']}
        for export in self.settings['exports']:
            entry = catalog.get(export['pipeline'])
            if not export['enabled'] or not entry or entry['errors']:
                continue
            tools.append({'name': export['name'], 'description': export['description'] or entry['name'],
                'inputSchema': entry['inputSchema'], 'outputSchema': {**copy.deepcopy(envelope), 'properties': {**copy.deepcopy(envelope['properties']), 'result': entry['outputSchema']}},
                '_meta': {'toshy/outputSchema': entry['outputSchema']}})
        if self.settings['agent_enabled']:
            tools.append({'name': 'ask_toshy', 'description': 'Give a task to Toshy in a new independent session. Returns the run ID.',
                'inputSchema': {'type': 'object', 'properties': {'task': {'type': 'string', 'minLength': 1}},
                    'required': ['task'], 'additionalProperties': False}, 'outputSchema': envelope})
        tools.extend([
            {'name': 'get_run', 'description': 'Get the run status and its final typed result.',
                'inputSchema': {'type': 'object', 'properties': {'run_id': {'type': 'string'}}, 'required': ['run_id'], 'additionalProperties': False}},
            {'name': 'get_run_events', 'description': 'Read new events. Pass next_cursor as cursor to read the next page.',
                'inputSchema': {'type': 'object', 'properties': {'run_id': {'type': 'string'}, 'cursor': {'type': 'integer', 'minimum': 0},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, 'required': ['run_id'], 'additionalProperties': False}},
            {'name': 'cancel_run', 'description': 'Cancel a Toshy run.',
                'inputSchema': {'type': 'object', 'properties': {'run_id': {'type': 'string'}}, 'required': ['run_id'], 'additionalProperties': False}}])
        return tools

    def get_run(self, run_id):
        record = self.records.get(run_id)
        if record is None:
            raise ValueError('Запуск не найден')
        result = {k: copy.deepcopy(v) for k, v in record.items() if k not in {'request_key', 'fingerprint'}}
        mcp = self.agent.registry.loaded_modules.get('mcp_client')
        interactions = getattr(getattr(mcp, 'manager', None), 'interactions', {})
        if result['status'] == 'running' and any(entry['run_id'] == run_id for entry in interactions.values()):
            result['status'] = 'waiting_input'
        return result

    def events(self, run_id, cursor=0, limit=100):
        self.get_run(run_id)
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Недопустимая позиция или размер страницы')
        from itertools import islice
        with (self.root / 'runs' / run_id / 'events.jsonl').open(encoding='utf-8') as handle:
            events = [json.loads(line) for line in islice(handle, cursor, cursor + limit)]
        return {'events': events, 'next_cursor': cursor + len(events), 'status': self.get_run(run_id)['status']}

    async def cancel(self, run_id):
        self.get_run(run_id)
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        record = self.records[run_id]
        if record['status'] not in TERMINAL:
            # A task cancelled before its first coroutine step cannot run finally.
            record.update(status='cancelled', updated_at=time.time())
            write_json(self.root / 'runs' / run_id / 'run.json', record)
            self.tasks.pop(run_id, None)
        return self.get_run(run_id)

    async def call(self, name, arguments, request_key=''):
        arguments = copy.deepcopy(arguments)
        if not self.settings['enabled']:
            raise ValueError('Toshy MCP выключен')
        specs = {x['name']: x for x in self.tool_specs()}
        if name not in specs:
            raise ValueError('Инструмент недоступен; проверьте выбранные пайплайны и модули')
        from jsonschema import validate
        validate(arguments, specs[name]['inputSchema'])
        if name == 'get_run':
            return self.get_run(**arguments)
        if name == 'get_run_events':
            return self.events(**arguments)
        if name == 'cancel_run':
            return await self.cancel(**arguments)
        fingerprint = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if request_key and request_key in self.requests:
            old_fingerprint, run_id = self.requests[request_key]
            if fingerprint != old_fingerprint:
                raise ValueError('Ключ запроса уже использован с другими аргументами')
            return {k: self.records[run_id][k] for k in ('run_id', 'session_id', 'status')}
        settings = copy.deepcopy(self.settings)
        context = ExecutionContext.create(session_id='mcp_' + secrets.token_hex(12),
            workspace_root=settings['workspace_root'], allowed_modules=tuple(settings['modules']),
            model_profile_id=settings['model_profile_id'])
        if not context.model_profile_id:
            from core.model_connections import load_settings
            context = replace(context, model_profile_id=str(load_settings().get('selected') or ''))
        definition, nested, reference, output_schema = None, {}, '', None
        if name != 'ask_toshy':
            export = next(x for x in settings['exports'] if x['name'] == name)
            reference = export['pipeline']
            module, pid = reference.split('/', 1)
            from modules.pipelines.storage import load_pipeline_script
            store = self.agent.pipeline_service.store
            definition = load_pipeline_script(store.resolve(module, pid) / 'pipeline.py')
            def freeze(graph):
                for node in graph.nodes:
                    if node.type_id == 'pipeline.call':
                        ref = node.settings.get('pipeline')
                        if ref not in nested:
                            nested[ref] = store.internal_definition(module, ref)
                            freeze(nested[ref])
            freeze(definition)
            from modules.pipelines.contracts import interface, schema_for
            output_schema = schema_for(interface(definition)[1])
        metadata = {'title': 'MCP: ' + name, 'source': 'toshy-mcp', 'workspace_root': str(context.workspace_root),
            'modules': list(context.allowed_modules), 'model_profile_id': context.model_profile_id}
        from langchain_core.messages import HumanMessage
        initial_messages = [HumanMessage(content=arguments['task'] if name == 'ask_toshy' else json.dumps(arguments, ensure_ascii=False))]
        from core.request_clock import stamp_latest_user
        stamp_latest_user(initial_messages)
        self.sessions.save_session(context.session_id, initial_messages, metadata)
        run_id = context.run_id
        directory = self.root / 'runs' / run_id
        directory.mkdir(parents=True)
        (directory / 'events.jsonl').touch()
        record = {'run_id': run_id, 'session_id': context.session_id, 'status': 'running', 'tool': name,
            'created_at': time.time(), 'updated_at': time.time(), 'result': None, 'error': '', 'event_count': 0,
            'request_key': request_key, 'fingerprint': fingerprint, 'output_schema': output_schema, 'partial_text': ''}
        self.records[run_id] = record
        if request_key:
            self.requests[request_key] = (fingerprint, run_id)
        write_json(directory / 'run.json', record)
        last_saved = 0.0
        async def emit(event):
            nonlocal last_saved
            kind = event.get('type')
            if kind == 'thought':
                return
            if kind == 'content':
                record['partial_text'] += str(event.get('delta') or '')
            elif kind in {'content_reset', 'content_interim', 'answer_restart'}:
                record['partial_text'] = ''
            safe = {k: v for k, v in event.items() if k not in {'state', 'error_object'}}
            if kind in {'human_request', 'pipeline_pause'}:
                record['status'] = 'waiting_input'
            elif record['status'] == 'waiting_input':
                record['status'] = 'running'
            event = {'cursor': record['event_count'], 'time': time.time(), **safe}
            with (directory / 'events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
            record['event_count'] += 1
            record['updated_at'] = time.time()
            record['current_event'] = kind
            if kind == 'pipeline_node_start':
                record['current_node'] = event.get('node_id')
            now = time.monotonic()
            if kind not in {'content', 'content_interim'} or now - last_saved >= .25:
                write_json(directory / 'run.json', record)
                last_saved = now
        async def execute():
            from langchain_core.messages import HumanMessage, AIMessage
            try:
                await emit({'type': 'run_started'})
                if name == 'ask_toshy':
                    messages = await self.execution.task(arguments['task'], context, emit)
                    result = str(messages[-1].content) if messages else ''
                    if messages and (messages[-1].additional_kwargs.get('direct_output') or messages[-1].additional_kwargs.get('subagent_direct')):
                        calls = next((getattr(message,'tool_calls',None) for message in reversed(messages[:-1])
                                      if getattr(message,'tool_calls',None)), [])
                        identities = {call['id'] for call in calls}
                        result = '\n\n'.join(str(message.content) for message in messages
                            if getattr(message,'tool_call_id',None) in identities
                            and (message.additional_kwargs.get('direct_output') or message.additional_kwargs.get('subagent_result')))
                else:
                    result = await self.execution.pipeline(reference, copy.deepcopy(arguments), context, emit, definition, nested)
                    if not output_schema['properties']:
                        result = {}
                    else:
                        from jsonschema import ValidationError
                        try:
                            validate(result, output_schema)
                        except ValidationError:
                            if not isinstance(result, dict) or set(result) != {'result', 'message'}:
                                raise
                            validate(result['result'], output_schema)
                            await emit({'type': 'pipeline_message', 'message': str(result['message'])})
                            result = result['result']
                    messages = [initial_messages[0],
                        AIMessage(content=json.dumps(result, ensure_ascii=False, default=str))]
                self.sessions.save_session(context.session_id, messages, metadata)
                record.update(status='completed', result=result, partial_text='')
            except asyncio.CancelledError:
                record.update(status='cancelled')
                for pending in self.agent.pipeline_service.broker.pending(context.session_id):
                    self.agent.pipeline_service.broker.abandon(pending['request_id'])
            except Exception as error:
                record.update(status='failed', error=str(error))
            finally:
                if record['status'] != 'completed' and record['partial_text']:
                    self.sessions.save_session(context.session_id, [*initial_messages, AIMessage(content=record['partial_text'])], metadata)
                await emit({'type': 'run_finished', 'status': record['status']})
                self.tasks.pop(run_id, None)
        self.tasks[run_id] = asyncio.create_task(execute())
        return {k: record[k] for k in ('run_id', 'session_id', 'status')}

    async def shutdown(self):
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for run_id in list(self.tasks):
            await self.cancel(run_id)
