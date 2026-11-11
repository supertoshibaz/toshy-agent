"""Server-to-host requests, bound to the currently executing call."""
import asyncio
import re
import time
import uuid
from collections import deque
from contextlib import nullcontext
from urllib.parse import urlsplit

from core.context import bind_execution_context


def field(value, snake, camel=None, default=None):
    if isinstance(value, dict):
        return value.get(camel or snake, value.get(snake, default))
    return getattr(value, snake, getattr(value, camel or snake, default))


class Callbacks:
    def __init__(self, actor):
        self.actor = actor
        self.samples = deque()

    async def roots(self, request_context):
        from mcp.types import ListRootsResult, Root
        job = self.actor.active_job
        roots = [Root(uri=job.context.workspace_root.as_uri(), name=job.context.workspace_root.name)] if job else []
        return ListRootsResult(roots=roots)

    async def sampling(self, request_context, params):
        from mcp.types import CreateMessageResult, TextContent, ErrorData
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
        from core.system_prompt import build_core_system_prompt
        job, manager = self.actor.active_job, self.actor.manager
        client = getattr(manager.registry, 'client', None)
        if job is None or client is None:
            return ErrorData(code=-32600, message='Sampling requires an active Toshy tool call.')
        if field(params, 'tools') or field(params, 'include_context', 'includeContext', 'none') not in {None, 'none'}:
            return ErrorData(code=-32602, message='Tool-enabled sampling and implicit conversation access are not supported.')
        now = time.monotonic()
        while self.samples and now - self.samples[0] >= 60:
            self.samples.popleft()
        if len(self.samples) >= 10:
            return ErrorData(code=-32000, message='Sampling rate limit: 10 requests per minute.')
        self.samples.append(now)
        messages = [SystemMessage(content=build_core_system_prompt(job.context.workspace_root))]
        instruction = field(params, 'system_prompt', 'systemPrompt')
        if instruction:
            messages.append(HumanMessage(content='MCP server instructions for this request:\n' + instruction))
        try:
            for message in field(params, 'messages', default=[]):
                blocks = field(message, 'content')
                blocks = blocks if isinstance(blocks, list) else [blocks]
                content = []
                for block in blocks:
                    kind = field(block, 'type')
                    if kind == 'text':
                        content.append({'type': 'text', 'text': field(block, 'text', default='')})
                    elif kind == 'image':
                        content.append({'type': 'image_url', 'image_url': {'url': 'data:' + field(block, 'mime_type', 'mimeType') + ';base64,' + field(block, 'data')}})
                    elif kind == 'audio':
                        mime = field(block, 'mime_type', 'mimeType', 'audio/wav')
                        content.append({'type': 'input_audio', 'input_audio': {'data': field(block, 'data'), 'format': 'mp3' if mime == 'audio/mpeg' else 'wav'}})
                    else:
                        raise ValueError('Unsupported sampling content: ' + str(kind))
                cls = AIMessage if field(message, 'role') == 'assistant' else HumanMessage
                messages.append(cls(content=content))
            maximum = max(1, min(int(field(params, 'max_tokens', 'maxTokens', 4096)), 4096))
            # Server instructions are request messages, never system additions.
            with bind_execution_context(job.context), (client.bind_connection(job.connection) if job.connection else nullcontext()):
                response = await asyncio.wait_for(client.chat_completion(messages, session_id=job.context.session_id,
                    max_tokens=maximum), timeout=60)
            choice = response['choices'][0]
            if choice['message'].get('tool_calls'):
                raise ValueError('Sampling response unexpectedly requested tools.')
            text = re.sub(r'<think>[\s\S]*?</think>', '', choice['message'].get('content') or '').strip()
            reason = 'maxTokens' if choice.get('finish_reason') == 'length' else 'endTurn'
            return CreateMessageResult(role='assistant', content=TextContent(type='text', text=text),
                model=response.get('model') or (job.connection.model if job.connection else client.model_name), stopReason=reason)
        except Exception as error:
            return ErrorData(code=-32603, message=manager.redact(str(error), self.actor.owner, self.actor.record['id']))

    async def elicitation(self, request_context, params):
        from mcp.types import ElicitResult
        job = self.actor.active_job
        if job is None:
            return ElicitResult(action='cancel')
        mode = field(params, 'mode', default='form')
        url = field(params, 'url', default='')
        if mode == 'url':
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                return ElicitResult(action='decline')
        schema = field(params, 'requested_schema', 'requestedSchema', {}) if mode == 'form' else {}
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        entry = {'request_id': request_id, 'owner': self.actor.owner, 'server_id': self.actor.record['id'],
            'server_name': self.actor.record['name'], 'mode': mode, 'message': field(params, 'message', default=''),
            'schema': schema, 'url': url, 'session_id': job.context.session_id,
            'branch': job.context.branch, 'run_id': job.context.run_id, 'call_id': job.call_id, 'future': future}
        self.actor.manager.interactions[request_id] = entry
        started = time.monotonic()
        job.interactive += 1
        try:
            response = await asyncio.wait_for(future, timeout=300)
            return ElicitResult(**response)
        except asyncio.TimeoutError:
            return ElicitResult(action='cancel')
        finally:
            job.interactive -= 1
            job.pause_seconds += time.monotonic() - started
            self.actor.manager.interactions.pop(request_id, None)
