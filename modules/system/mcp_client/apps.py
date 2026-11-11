"""Optional MCP Apps host boundary. No dependencies on Qt or the chat editor."""
import asyncio
import base64
import copy
import time
import uuid
from contextlib import nullcontext
from urllib.parse import urlsplit
from core.context import bind_execution_context

MIME = 'text/html;profile=mcp-app'


def ui_meta(tool):
    meta = tool.get('_meta') or {}
    return meta.get('ui') or {'resourceUri': meta.get('ui/resourceUri')}


def session_class(base, enabled):
    if not enabled:
        return base
    class AppsClientSession(base):
        def _build_capabilities(self, *args, **kwargs):
            capabilities = super()._build_capabilities(*args, **kwargs)
            extensions = dict(getattr(capabilities, 'extensions', None) or {})
            extensions['io.modelcontextprotocol/ui'] = {'mimeTypes': [MIME]}
            capabilities.extensions = extensions
            return capabilities

        async def send_request(self, request, result_type, *args, **kwargs):
            root = getattr(request, 'root', request)
            method = getattr(root, 'method', None)
            if enabled and method == 'initialize':
                capabilities = root.params.capabilities
                extensions = dict(getattr(capabilities, 'extensions', None) or {})
                extensions['io.modelcontextprotocol/ui'] = {'mimeTypes': [MIME]}
                capabilities.extensions = extensions
            return await super().send_request(request, result_type, *args, **kwargs)
    return AppsClientSession


class Apps:
    def __init__(self, manager):
        self.manager = manager
        self.entries = {}

    def capture(self, owner, server_id, tool, arguments, result, context, connection):
        record = self.manager.store.read(owner, server_id)
        if not record.get('apps', True):
            return
        raw = next((t for t in record.get('tools', []) if t['name'] == tool), {})
        uri = ui_meta(raw).get('resourceUri')
        if not isinstance(uri, str) or not uri.startswith('ui://'):
            return
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if now - entry['updated'] > 8 * 3600 and not entry['inflight']:
                self.entries.pop(key)
        while len(self.entries) >= 64:
            removable = next((key for key, e in self.entries.items() if not e['inflight']), None)
            if removable is None:
                return
            self.entries.pop(removable)
        identity = uuid.uuid4().hex
        self.entries[identity] = {'id': identity, 'owner': owner, 'server_id': server_id,
            'title': record['name'] + ' · ' + tool, 'tool': tool, 'uri': uri,
            'arguments': copy.deepcopy(arguments), 'result': copy.deepcopy(result), 'context': context,
            'connection': connection, 'updated': now, 'inflight': set()}

    def catalog(self, session_id, branch, allowed):
        return [{k: entry[k] for k in ('id', 'owner', 'server_id', 'title')} for entry in self.entries.values()
            if entry['context'].session_id == session_id and entry['context'].branch == branch
            and not self.manager.access_reason(allowed, entry['owner'])]

    def get(self, identity, session_id, branch, allowed):
        entry = self.entries.get(identity)
        if not entry or entry['context'].session_id != session_id or entry['context'].branch != branch:
            raise ValueError('Приложение не найдено в этой сессии')
        reason = self.manager.access_reason(allowed, entry['owner'])
        record = self.manager.store.read(entry['owner'], entry['server_id'])
        if reason or not record.get('apps', True):
            raise ValueError(reason or 'MCP Apps выключены для этого сервера')
        entry['updated'] = time.monotonic()
        return entry

    async def invoke(self, entry, name, arguments, operation='call_tool'):
        client = getattr(self.manager.registry, 'client', None)
        task = asyncio.current_task()
        entry['inflight'].add(task)
        try:
            with bind_execution_context(entry['context']), (client.bind_connection(entry['connection']) if client and entry['connection'] else nullcontext()):
                return await self.manager.call(entry['owner'], entry['server_id'], name, arguments, operation=operation)
        finally:
            entry['inflight'].discard(task)

    async def document(self, entry):
        result = await self.invoke(entry, '__toshy_mcp_read_resource', {'uri': entry['uri']}, 'read_resource')
        contents = [r for r in result.get('contents', []) if r.get('uri') == entry['uri'] and r.get('mimeType') == MIME]
        if not contents:
            raise ValueError('Сервер не вернул HTML-ресурс MCP Apps')
        resource = contents[0]
        html = resource.get('text')
        if html is None:
            html = base64.b64decode(resource.get('blob', ''), validate=True).decode('utf-8')
        if len(html.encode('utf-8')) > 2 * 1024 * 1024:
            raise ValueError('HTML MCP Apps превышает 2 MiB')
        return {'html': html, 'ui': (resource.get('_meta') or {}).get('ui') or {},
            'arguments': entry['arguments'], 'result': entry['result'], 'title': entry['title']}

    async def message(self, entry, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ValueError('Некорректное сообщение MCP Apps')
        identity = message.get('id')
        method, params = message.get('method'), message.get('params') or {}
        reply = {'jsonrpc': '2.0', 'id': identity}
        try:
            if method == 'ui/initialize':
                if params.get('protocolVersion') != '2026-01-26':
                    raise ValueError('Неподдерживаемая версия протокола MCP Apps')
                value = {'protocolVersion': '2026-01-26', 'hostInfo': {'name': 'Toshy', 'version': '1.0'},
                    'hostCapabilities': {'serverTools': {}, 'serverResources': {}, 'openLinks': {}},
                    'hostContext': {'theme': 'dark', 'displayMode': 'inline',
                        'availableDisplayModes': ['inline'], 'locale': 'ru-RU'}}
            elif method == 'ping':
                value = {}
            elif method == 'tools/call':
                raw = next((tool for tool in self.manager.store.read(entry['owner'], entry['server_id']).get('tools', [])
                    if tool['name'] == params.get('name')), None)
                if not raw or 'app' not in ui_meta(raw).get('visibility', ['model', 'app']):
                    raise ValueError('Инструмент недоступен приложению этого сервера')
                from jsonschema import validate
                arguments = params.get('arguments') or {}
                validate(arguments, raw.get('inputSchema') or {'type': 'object'})
                value = await self.invoke(entry, raw['name'], arguments)
            elif method == 'resources/read':
                value = await self.invoke(entry, '__toshy_mcp_read_resource', {'uri': params['uri']}, 'read_resource')
            elif method in {'ui/notifications/initialized', 'ui/notifications/size-changed', 'notifications/message'}:
                return None
            else:
                reply['error'] = {'code': -32601, 'message': 'Возможность хоста не поддерживается: ' + str(method)}
                return reply if identity is not None else None
            reply['result'] = value
        except Exception as error:
            reply['error'] = {'code': -32602, 'message': self.manager.redact(str(error), entry['owner'], entry['server_id'])}
        return reply if identity is not None else None

    async def close(self, entry):
        tasks = list(entry['inflight'])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
