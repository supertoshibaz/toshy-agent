"""Owner-aware MCP client. Each connection is entered and closed by one actor task."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from core.context import get_execution_context
from core.logger import core_logger
from core.registry import BaseModule, BaseTool
from .store import GLOBAL_OWNER, McpStore, get_secret, set_secret, tool_name
from .install import install, environment
from .runtime import ServerActor, Job


def dump(value):
    return value.model_dump(by_alias=True, mode="json") if hasattr(value, "model_dump") else value


def error_message(error):
    children = getattr(error, 'exceptions', None)
    if children:
        return '\n'.join(dict.fromkeys(error_message(child) for child in children))
    return str(error) or type(error).__name__


def http_factory(url):
    # MCP SDK 1 uses httpx, SDK 2 uses httpx2. Use its own HTTP types.
    from mcp.shared._httpx_utils import create_mcp_http_client
    module = inspect.getmodule(create_mcp_http_client)
    library = getattr(module, 'httpx2', None) or getattr(module, 'httpx')
    from ipaddress import ip_address
    host = urlsplit(url).hostname or ''
    try:
        local = ip_address(host).is_loopback
    except ValueError:
        local = host.casefold() == 'localhost'
    def factory(headers=None, timeout=None, auth=None):
        return library.AsyncClient(headers=headers, timeout=timeout or 60, auth=auth, trust_env=not local)
    return factory


class McpToolBridge(BaseTool):
    def __init__(self, manager, owner, record, tool):
        self.manager, self.mcp_owner, self.server_id = manager, owner, record['id']
        self.original_name = tool['name']
        self.mcp_visibility = record.get('visibility', 'model')
        self.model_visible = self.mcp_visibility != 'pipeline' and 'model' in ((tool.get('_meta') or {}).get('ui') or {}).get('visibility', ['model', 'app'])
        self.operation = tool.get('_operation', 'call_tool')
        self.bound = tool.get('_bound')
        super().__init__(tool_name(owner, record['id'], tool['name']),
            f"[{owner} / {record['name']}] " + (tool.get('description') or tool['name']),
            copy.deepcopy(tool.get('inputSchema') or {'type': 'object', 'properties': {}}))

    async def execute(self, **kwargs):
        context = get_execution_context()
        reason = self.manager.access_reason(context.allowed_modules, self.mcp_owner)
        if reason:
            return 'System Error: ' + reason
        if self.mcp_visibility == 'pipeline':
            from modules.pipelines.service import _frame
            frame = _frame.get()
            if not frame or frame.get('depth', 0) < 1:
                return 'System Error: MCP-сервер доступен только пайплайнам.'
        try:
            from jsonschema import validate
            validate(kwargs, self.args_schema)
            arguments = kwargs
            if self.bound and self.operation == 'get_prompt':
                arguments = {'name': self.bound, 'arguments': kwargs}
            elif self.bound and self.operation == 'read_resource':
                from urllib.parse import quote
                arguments = {'uri': re.sub(r'\{([^{}]+)\}', lambda match: quote(str(kwargs[match[1]]), safe=''), self.bound)}
            result = await self.manager.call(self.mcp_owner, self.server_id, self.original_name, arguments, operation=self.operation)
            if self.operation == 'call_tool':
                try:
                    client = getattr(self.manager.registry, 'client', None)
                    self.manager.apps.capture(self.mcp_owner, self.server_id, self.original_name, kwargs, result, context,
                        client.model_connection() if client else None)
                except Exception as error:
                    core_logger.event('modules', 'MCP_APPS_ERROR', self.manager.redact(str(error), self.mcp_owner, self.server_id), level='WARN')
            blocks = result.get('content', [])
            if self.operation != 'call_tool':
                return json.dumps(result, ensure_ascii=False)
            parts = [str(x.get('text', '')) for x in blocks if x.get('type') == 'text']
            # Preserve structured and media output rather than treating base64 as prose.
            other = [x for x in blocks if x.get('type') != 'text']
            if result.get('structuredContent') is not None:
                structured = result['structuredContent']
                if structured != {'result': '\n'.join(parts)}:
                    parts.append(json.dumps(structured, ensure_ascii=False))
            if other:
                from core.paths import APP_PATHS
                import base64, uuid
                directory = self.manager.store.path(self.mcp_owner, self.server_id) / 'runtime' / 'results' / context.run_id
                directory.mkdir(parents=True, exist_ok=True)
                for block in other:
                    if block.get('type') in {'image', 'audio'} and block.get('data'):
                        suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp',
                                  'audio/wav': '.wav', 'audio/mpeg': '.mp3'}.get(block.get('mimeType'), '.bin')
                        path = directory / (uuid.uuid4().hex + suffix)
                        path.write_bytes(base64.b64decode(block['data'], validate=True))
                        parts.append('MCP media result saved: ' + str(path))
                    else:
                        parts.append(json.dumps(block, ensure_ascii=False))
            return ('System Error: MCP returned an error\n' if result.get('isError') else '') + ('\n'.join(parts) or 'MCP: выполнено.')
        except Exception as error:
            return 'System Error: ' + self.manager.redact(str(error), self.mcp_owner, self.server_id)


class McpManager:
    def __init__(self):
        self.store = McpStore()
        self.registry = None
        self.actors = {}
        self.lock = asyncio.Lock()
        self._published = set()
        self.oauth_waiters = {}
        self.manual_stops = set()
        self.interactions = {}
        self.workers = set()
        from .apps import Apps
        self.apps = Apps(self)

    @staticmethod
    def all_tools(record):
        tools = [{**tool, '_operation': 'call_tool'} for tool in record.get('tools', [])]
        capabilities = record.get('capabilities', {})
        operations = []
        if capabilities.get('resources') is not None:
            operations.extend([
                ('list_resources', 'Получить список ресурсов MCP-сервера.', {}),
                ('list_resource_templates', 'Получить шаблоны адресов ресурсов MCP-сервера.', {}),
                ('read_resource', 'Прочитать ресурс MCP-сервера по URI.', {'uri': {'type': 'string'}})])
        if capabilities.get('prompts') is not None:
            operations.extend([
                ('list_prompts', 'Получить список шаблонов сообщений MCP-сервера.', {}),
                ('get_prompt', 'Получить MCP-шаблон сообщений с подставленными аргументами.',
                    {'name': {'type': 'string'}, 'arguments': {'type': 'object', 'additionalProperties': {'type': 'string'}}})])
        existing = {tool['name'] for tool in tools}
        for operation, description, properties in operations:
            name = '__toshy_mcp_' + operation
            if name in existing:
                continue
            tools.append({'name': name, 'description': description, '_operation': operation,
                'inputSchema': {'type': 'object', 'properties': properties,
                    'required': [key for key in properties if key != 'arguments'], 'additionalProperties': False}})
        for prompt in record.get('prompts', []):
            name = '__toshy_prompt_' + prompt['name']
            if name not in existing:
                tools.append({'name': name, 'title': 'Prompt: ' + prompt['name'], 'description': prompt.get('description', prompt['name']),
                    '_operation': 'get_prompt', '_bound': prompt['name'],
                    'inputSchema': {'type': 'object', 'properties': {a['name']: {'type': 'string',
                        'description': a.get('description', '')} for a in prompt.get('arguments', [])},
                        'required': [a['name'] for a in prompt.get('arguments', []) if a.get('required')],
                        'additionalProperties': False}})
        for resource in [*record.get('resources', []), *record.get('resourceTemplates', [])]:
            uri = resource.get('uri') or resource.get('uriTemplate')
            if not uri:
                continue
            import hashlib
            name = '__toshy_resource_' + hashlib.sha256(uri.encode()).hexdigest()[:16]
            variables = re.findall(r'\{([A-Za-z0-9_]+)\}', uri)
            # Advanced RFC 6570 templates remain available through read_resource.
            if re.search(r'\{[^{}]*[^A-Za-z0-9_{}][^{}]*\}', uri):
                continue
            tools.append({'name': name, 'title': 'Resource: ' + (resource.get('name') or uri), 'description': resource.get('description') or resource.get('name') or uri,
                '_operation': 'read_resource', '_bound': uri,
                'inputSchema': {'type': 'object', 'properties': {v: {'type': 'string'} for v in variables},
                    'required': variables, 'additionalProperties': False}})
        return tools

    def tool_enabled(self, owner, identity, name):
        try:
            record = self.store.read(owner, identity)
            return name not in record.get('disabled_tools', []) and any(x['name'] == name for x in self.all_tools(record))
        except (OSError, ValueError):
            return False

    def check_tool(self, owner, identity, name, operation, context=None):
        reason = self.access_reason((context or get_execution_context()).allowed_modules, owner)
        if reason:
            raise ValueError(reason)
        record = self.store.read(owner, identity)
        if operation in {'skill_get', 'skill_resource', 'skill_directory'}:
            from .skills import supported, EXTENSION
            if not supported(record):
                raise ValueError('Сервер не объявил поддержку MCP Skills.')
            if operation == 'skill_directory' and not record['capabilities']['extensions'][EXTENSION].get('directoryRead'):
                raise ValueError('Сервер не поддерживает чтение каталогов.')
            return
        if name in record.get('disabled_tools', []):
            raise ValueError('MCP-инструмент отключён пользователем: ' + name)
        tool = next((x for x in self.all_tools(record) if x['name'] == name), None)
        if tool is None or tool['_operation'] != operation:
            raise ValueError('MCP-инструмент больше недоступен: ' + name)

    def respond(self, owner, identity, request_id, response, session_id, branch):
        from jsonschema import validate
        entry = self.interactions.get(request_id)
        if not entry or entry['future'].done() or (entry['owner'], entry['server_id']) != (owner, identity):
            raise ValueError('Запрос ввода завершён или не существует.')
        if entry['session_id'] != session_id or entry['branch'] != branch:
            raise ValueError('Запрос ввода относится к другой сессии или ветке.')
        action = response.get('action')
        if action not in {'accept', 'decline', 'cancel'}:
            raise ValueError('Неизвестный ответ elicitation.')
        result = {'action': action}
        if action == 'accept' and entry['mode'] == 'form':
            content = response.get('content', {})
            validate(content, entry['schema'])
            result['content'] = content
        entry['future'].set_result(result)
        return {'success': True}

    def access_reason(self, allowed=None, owner=None):
        registry = self.registry
        if not (self.store.modules / 'system' / 'mcp_client' / '__init__.py').is_file():
            return 'Системный модуль MCP отсутствует.'
        if registry is None or 'mcp_client' not in registry.loaded_modules:
            return 'Системный модуль MCP выключен.'
        if allowed is not None and 'mcp_client' not in allowed:
            return 'Системный модуль MCP запрещён для текущей сессии.'
        if owner and owner != GLOBAL_OWNER:
            if owner not in registry.loaded_modules:
                return 'Модуль-владелец выключен. Сервер недоступен агенту.'
            if allowed is not None and owner not in allowed:
                return 'Модуль-владелец запрещён для текущей сессии.'
        return ''

    def redact(self, text, owner, identity):
        try:
            record = self.store.read(owner, identity)
            for field in [*record.get('secret_fields', []), 'oauth:tokens', 'oauth:client']:
                value = get_secret(owner, identity, field)
                if value:
                    text = text.replace(value, '[secret]')
                    if field.startswith('oauth:'):
                        try:
                            for key, token in json.loads(value).items():
                                if isinstance(token, str) and ('token' in key or 'secret' in key):
                                    text = text.replace(token, '[secret]')
                        except ValueError:
                            pass
        except Exception:
            pass
        return text

    def launch(self, owner, record):
        base = self.store.path(owner, record['id'])
        cwd = (base / record.get('working_directory', '.')).resolve()
        if not cwd.is_relative_to(base.resolve()) or not cwd.is_dir():
            raise ValueError('Рабочий каталог сервера должен находиться внутри его установки')
        values = copy.deepcopy(record.get('values', {}))
        for key, field in record.get('user_config', {}).items():
            if field.get('sensitive'):
                values[key] = get_secret(owner, record['id'], 'config:' + key)
            if field.get('required') and values.get(key) in (None, '', []):
                raise ValueError('Заполните настройку: ' + str(field.get('title') or key))
        def expand(value):
            if isinstance(value, dict) and value.get('secret'):
                return get_secret(owner, record['id'], value['secret'])
            text = str(value)
            def replace(match):
                key = match.group(1)
                if key == '__dirname':
                    return str(cwd)
                if key == 'HOME':
                    return str(Path.home())
                if key.startswith('user_config.'):
                    val = values.get(key[len('user_config.'):], '')
                    return json.dumps(val) if isinstance(val, (dict, list)) else str(val).lower() if isinstance(val, bool) else str(val)
                raise ValueError('Неизвестная переменная запуска: ' + key)
            return re.sub(r'\$\{([^}]+)\}', replace, text)
        args = []
        for argument in record.get('args', []):
            match = re.fullmatch(r'\$\{user_config\.([^}]+)\}', str(argument))
            if match and isinstance(values.get(match.group(1)), list):
                args.extend(str(x) for x in values[match.group(1)])
            else:
                args.append(expand(argument))
        local_venv = cwd / 'venv'
        local_bin = local_venv / ('Scripts' if os.name == 'nt' else 'bin')
        env = environment(base / 'runtime', isolated_python=local_bin.is_dir())
        # Preserve local runtimes and isolate npm/uv caches per owner instance.
        search_paths = [str(Path(sys.executable).parent), env.get('PATH', '')]
        if local_bin.is_dir():
            search_paths.insert(0, str(local_bin))
            env['VIRTUAL_ENV'] = str(local_venv)
        env['PATH'] = os.pathsep.join(search_paths)
        env.update({key: expand(value) for key, value in record.get('env', {}).items()})
        command = expand(record.get('command', ''))
        if command in {'python', 'python3'}:
            local_python = local_bin / ('python.exe' if os.name == 'nt' else 'python')
            command = str(local_python) if local_python.is_file() else sys.executable
        if record.get('transport', 'stdio') == 'stdio' and not command:
            raise ValueError('Укажите команду запуска сервера')
        return {'command': command, 'args': args, 'env': env, 'cwd': str(cwd),
                'transport': record.get('transport', 'stdio'), 'url': expand(record.get('url', '')),
                'headers': {key: expand(value) for key, value in record.get('headers', {}).items()}}

    def publish(self):
        registry = self.registry
        if registry is None:
            return
        removed = {name for name in self._published if getattr(registry.tools.get(name), 'manager', None) is self}
        for name in removed:
            registry.tools.pop(name, None)
        for owner in registry.module_tools_map:
            registry.module_tools_map[owner] = [n for n in registry.module_tools_map[owner] if n not in removed]
        self._published.clear()
        if self.access_reason():
            return
        for (owner, identity), actor in self.actors.items():
            if actor.status != 'connected' or actor.stop_requested or self.access_reason(owner=owner):
                continue
            module = 'mcp_client' if owner == GLOBAL_OWNER else owner
            for raw in self.all_tools(actor.record):
                if raw['name'] in actor.record.get('disabled_tools', []):
                    continue
                tool = McpToolBridge(self, owner, actor.record, raw)
                registry.tools[tool.name] = tool
                registry.module_tools_map.setdefault(module, []).append(tool.name)
                self._published.add(tool.name)

    async def initialize(self):
        await self.reconcile()

    async def reconcile(self):
        async with self.lock:
            for worker in list(self.workers):
                if self.access_reason(owner=worker.owner):
                    await worker.stop(force=True)
            for (owner, identity), actor in list(self.actors.items()):
                if self.access_reason(owner=owner):
                    await actor.stop(force=True)
            if not self.access_reason():
                for owner, record in self.store.records():
                    if record.get('autostart') and record.get('installation') == 'ready' and not self.access_reason(owner=owner):
                        key = owner, record['id']
                        if key not in self.manual_stops and (key not in self.actors or self.actors[key].status == 'stopped'):
                            try:
                                await self._start(owner, record)
                            except Exception as error:
                                core_logger.event('modules', 'MCP_AUTOSTART_ERROR', self.redact(str(error), owner, record['id']), level='ERROR')
            self.publish()

    async def _start(self, owner, record):
        key = owner, record['id']
        previous = self.actors.get(key)
        if previous and not previous.task.done():
            return previous
        if record.get('installation') != 'ready':
            raise ValueError('Сначала завершите установку и настройку сервера')
        actor = ServerActor(self, owner, record)
        self.actors[key] = actor
        await asyncio.wait_for(asyncio.shield(actor.ready), timeout=max(75, float(record.get('timeout', 60)) + 5))
        if actor.status not in {'connected', 'authorizing', 'reconnecting'}:
            raise RuntimeError(actor.error or 'Не удалось подключиться к MCP')
        return actor

    async def start(self, owner, identity):
        async with self.lock:
            reason = self.access_reason(owner=owner)
            if reason:
                raise ValueError(reason)
            self.manual_stops.discard((owner, identity))
            await self._start(owner, self.store.read(owner, identity))
            self.publish()

    async def stop(self, owner, identity):
        async with self.lock:
            self.manual_stops.add((owner, identity))
            actor = self.actors.get((owner, identity))
            if actor:
                await actor.stop()
            for worker in list(self.workers):
                if worker.owner == owner and worker.record['id'] == identity:
                    await worker.stop()
            self.publish()

    async def call(self, owner, identity, name, arguments, *, operation='call_tool'):
        self.check_tool(owner, identity, name, operation)
        actor = self.actors.get((owner, identity))
        if not actor or actor.status not in {'connected', 'reconnecting', 'connecting'} or actor.task.done() or actor.stop_requested:
            raise ValueError('MCP-сервер не подключён: дождитесь восстановления или включите его тумблером.')
        context = get_execution_context()
        client = getattr(self.registry, 'client', None)
        connection = client.model_connection() if client is not None else None
        future = asyncio.get_running_loop().create_future()
        # Legacy callbacks have no originating request id. Keep one persistent
        # connection per chat/branch so both callbacks and server-side state stay
        # with their owner. Calls within that chat retain their Exec order.
        channel = (context.session_id or context.run_id, context.branch)
        candidates = [actor, *[w for w in self.workers if w.owner == owner and w.record['id'] == identity]]
        lane = next((w for w in candidates if w.channel_key == channel and not w.task.done() and not w.stop_requested), None)
        if lane is not None:
            actor = lane
        elif actor.channel_key is None:
            actor.channel_key = channel
        else:
            actor = ServerActor(self, owner, self.store.read(owner, identity))
            actor.channel_key = channel
            self.workers.add(actor)
        actor.reserved += 1
        try:
            if not actor.ready.done() or actor.status not in {'connected', 'reconnecting'}:
                await asyncio.wait_for(asyncio.shield(actor.ready), timeout=360)
                if actor.status not in {'connected', 'reconnecting', 'connecting'}:
                    raise ConnectionError(actor.error or 'Не удалось открыть параллельное соединение MCP')
            await actor.queue.put(Job(name, arguments, future, context, connection, operation))
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise
        finally:
            actor.reserved -= 1
            if actor.stop_requested and not actor.reserved:
                await actor.stop()

    async def close_session(self, session_id):
        for worker in list(self.workers):
            if worker.channel_key and worker.channel_key[0] == session_id:
                await worker.stop(force=True)
                self.workers.discard(worker)
        for key, actor in list(self.actors.items()):
            if actor.channel_key and actor.channel_key[0] == session_id:
                restart = not actor.stop_requested and not self.access_reason(owner=actor.owner)
                await actor.stop(force=True)
                if restart:
                    await self.start(*key)
        for key, entry in list(self.apps.entries.items()):
            if entry['context'].session_id == session_id:
                await self.apps.close(entry)
                self.apps.entries.pop(key, None)

    def acquire(self, tools):
        actors = {self.actors.get((t.mcp_owner, t.server_id)) for t in tools.values() if isinstance(t, McpToolBridge)} - {None}
        for actor in actors:
            actor.leases += 1
        return actors

    async def release(self, actors):
        for actor in actors:
            actor.leases -= 1
            if actor.stop_requested:
                await actor.stop()
        self.publish()

    async def shutdown(self):
        await asyncio.gather(*(actor.stop(force=True) for actor in [*self.actors.values(), *self.workers]))
        self.publish()

    def log_tail(self, actor, *, owner=None, identity=None):
        if actor is not None:
            owner, identity, path = actor.owner, actor.record['id'], actor.log_path
        elif owner and identity:
            path = self.store.path(owner,identity) / 'runtime' / 'stderr.log'
        else:return ''
        if not path.is_file():return ''
        try:
            with path.open('rb') as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - 8000))
                text = handle.read().decode('utf-8', errors='replace')
            return self.redact(text,owner,identity)
        except OSError:
            return ''

    def catalog(self, allowed=None, *, session_id=None, branch='main'):
        reason = self.access_reason(allowed)
        groups = self.store.owners()
        for group in groups:
            group['servers'] = []
        by_owner = {x['id']: x for x in groups}
        for owner, record in self.store.records():
            value = self.store.public(owner, record)
            actor = self.actors.get((owner, record['id']))
            workers = [w for w in self.workers if w.owner == owner and w.record['id'] == record['id']]
            value.update(owner=owner, status=(('stopping' if actor.stop_requested and not actor.task.done() else actor.status) if actor else 'stopped'),
                         connection_error=actor.error if actor else '',
                         authorization_url=next((w.authorization_url for w in [*workers, *([actor] if actor else [])] if w.authorization_url), ''),
                         log_tail=self.log_tail(actor,owner=owner,identity=record['id']),
                         management_reason=self.access_reason(owner=owner),
                         access_reason=self.access_reason(allowed, owner))
            value['tools'] = [{**tool, 'runtime_name': tool_name(owner, record['id'], tool['name']),
                'enabled': tool['name'] not in record.get('disabled_tools', [])} for tool in self.all_tools(record)]
            value['pending'] = [{k: v for k, v in item.items() if k != 'future'} for item in self.interactions.values()
                if item['owner'] == owner and item['server_id'] == record['id'] and item['session_id'] == session_id and item['branch'] == branch]
            value['retry_count'] = actor.retry_count if actor else 0
            value['active_calls'] = sum(w.calls for w in [*workers, *([actor] if actor else [])])
            by_owner[owner]['servers'].append(value)
        management_reason = self.access_reason()
        return {'available': not bool(reason), 'reason': reason, 'groups': groups,
            'management_available': not bool(management_reason), 'management_reason': management_reason,
            'apps': self.apps.catalog(session_id, branch, allowed)}

    async def update(self, owner, identity, updates):
        async with self.lock:
            actor = self.actors.get((owner, identity))
            if actor and not actor.task.done() and set(updates) - {'name', 'autostart', 'disabled_tools'}:
                raise ValueError('Выключите сервер перед изменением настроек')
            record = self.store.read(owner, identity)
            for key in ('name', 'autostart', 'visibility', 'command', 'args', 'transport', 'url', 'timeout', 'oauth', 'disabled_tools', 'reconnect', 'reconnect_attempts', 'sampling', 'elicitation', 'roots', 'apps'):
                if key in updates:
                    record[key] = updates[key]
            if not isinstance(record.get('args'), list) or not all(isinstance(x, str) for x in record['args']):
                raise ValueError('Аргументы запуска: нужен JSON-массив строк')
            if record['transport'] not in {'stdio', 'sse', 'streamable-http'}:
                raise ValueError('Неизвестный транспорт')
            record['timeout'] = max(1, min(float(record.get('timeout', 60)), 600))
            if record['transport'] != 'stdio':
                parsed = urlsplit(record.get('url', ''))
                if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Укажите HTTP(S) URL MCP без пароля в адресе')
            if not isinstance(record.get('disabled_tools', []), list) or not all(isinstance(x, str) for x in record.get('disabled_tools', [])):
                raise ValueError('disabled_tools: нужен список имён инструментов')
            record['disabled_tools'] = sorted(set(record.get('disabled_tools', [])))
            record['reconnect_attempts'] = max(0, min(int(record.get('reconnect_attempts', 5)), 10))
            for key in ('autostart', 'oauth', 'reconnect', 'sampling', 'elicitation', 'roots', 'apps'):
                if key in record and not isinstance(record[key], bool):
                    raise ValueError(key + ': нужен переключатель Boolean')
            values_update = updates.get('values', {})
            if not isinstance(values_update, dict):
                raise ValueError('values: нужен JSON-объект')
            for section in ('env', 'headers'):
                if section in updates and (not isinstance(updates[section], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in updates[section].items())):
                    raise ValueError(section + ': нужен JSON-объект со строками')
            storage = []
            for key, value in updates.get('values', {}).items():
                field = record.get('user_config', {}).get(key, {})
                if field.get('sensitive'):
                    secret = 'config:' + key
                    storage.append(set_secret(owner, identity, secret, value))
                    record.setdefault('secret_fields', [])
                    if secret not in record['secret_fields']:
                        record['secret_fields'].append(secret)
                else:
                    record.setdefault('values', {})[key] = value
            # Arbitrary environment/header entries are treated as credentials;
            # templates from the package remain declarative and untouched.
            for section in ('env', 'headers'):
                if section in updates:
                    for key, value in updates[section].items():
                        secret = section + ':' + key
                        storage.append(set_secret(owner, identity, secret, value))
                        record.setdefault('secret_fields', [])
                        if secret not in record['secret_fields']:
                            record['secret_fields'].append(secret)
                        record.setdefault(section, {})[key] = {'secret': secret}
            if record.get('installation') == 'needs_command' and record.get('command'):
                record['installation'] = 'ready'
            self.store.write(owner, record)
            if actor:
                actor.record = record
            self.publish()
            return {'success': True, 'message': ('Секреты сохранены до закрытия ядра: защищённое хранилище ОС недоступно.'
                    if 'runtime' in storage else 'Настройки сохранены.')}

    async def add(self, owner, **options):
        async with self.lock:
            record = await asyncio.to_thread(install, self.store, owner, **options)
            return {'success': record['installation'] != 'failed', 'server': self.store.public(owner, record)}

    async def remove(self, owner, identity):
        async with self.lock:
            actor = self.actors.get((owner, identity))
            workers = [worker for worker in self.workers if worker.owner == owner and worker.record['id'] == identity]
            if any(item.leases or item.calls or item.reserved for item in ([actor] if actor else []) + workers):
                raise ValueError('Сервер используется активным запуском. Повторите удаление после завершения.')
            if actor:
                await actor.stop(force=True)
            for worker in workers:
                await worker.stop(force=True)
                self.workers.discard(worker)
            for key, entry in list(self.apps.entries.items()):
                if entry['owner'] == owner and entry['server_id'] == identity:
                    await self.apps.close(entry)
                    self.apps.entries.pop(key, None)
            await asyncio.to_thread(self.store.remove, owner, identity)
            self.actors.pop((owner, identity), None)
            self.manual_stops.discard((owner, identity))
            self.publish()


class Module(BaseModule):
    def __init__(self):
        self.manager = McpManager()

    def set_context(self, registry):
        self.manager.registry = registry

    async def async_init(self):
        pass

    async def modules_changed(self):
        await self.manager.initialize()

    def refresh_tools(self):
        self.manager.publish()

    def filter_execution_tools(self, tools, allowed_modules):
        return {name: tool for name, tool in tools.items() if not isinstance(tool, McpToolBridge)
            or (not self.manager.access_reason(allowed_modules, tool.mcp_owner)
                and self.manager.tool_enabled(tool.mcp_owner, tool.server_id, tool.original_name))}

    @asynccontextmanager
    async def execution_scope(self, tools):
        actors = self.manager.acquire(tools)
        try:
            yield
        finally:
            await self.manager.release(actors)

    def get_request_messages(self, context, tools):
        actors = {(tool.mcp_owner, tool.server_id) for tool in tools.values() if isinstance(tool, McpToolBridge)}
        messages = []
        for owner, identity in sorted(actors):
            record = self.manager.store.read(owner, identity)
            if record.get('instructions'):
                messages.append(f"MCP server instructions for {owner}/{record['name']} for this request:\n" + record['instructions'])
        return messages

    def get_tools(self):
        return []

    def allows_public_request(self, request):
        return request.method == 'GET' and request.url.path == '/v1/modules/mcp_client/service/oauth/callback'

    async def handle_request(self, request, path, *, session_manager):
        from .api import handle_request
        return await handle_request(self.manager, request, path, session_manager)

    async def session_closed(self, session_id):
        await self.manager.close_session(session_id)

    async def deactivate(self):
        await self.manager.shutdown()

    async def shutdown(self):
        await self.manager.shutdown()
