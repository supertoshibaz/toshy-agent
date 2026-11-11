"""Protocol adapters: shared agent messages, provider-native tool/reasoning replay."""
from __future__ import annotations

import copy
from contextlib import aclosing
import json
import re

import httpx
from langchain_core.messages import AIMessage, messages_from_dict
from core.logger import core_logger
from core.chat_references import CHAT_ID, model_message_metadata, valid_chat_id


class ProviderStreamError(RuntimeError):
    pass


def _without_thinks(text):
    if not isinstance(text, str):
        return text
    return re.sub(r'<think>[\s\S]*?</think>\s*', '', text)


def _without_native_reasoning(message, protocol):
    """Remove historical reasoning from the wire copy, never from saved data.

    Current-run tool continuations opt into reasoning in _processed_history.
    Gemini signatures attached to function calls stay beside those calls.
    """
    message = copy.deepcopy(message)
    if protocol == 'chat_completions':
        for key in ('reasoning', 'reasoning_content', 'reasoning_details'):
            message.pop(key, None)
        message['content'] = _without_thinks(message.get('content'))
    elif protocol == 'responses':
        message['output'] = [item for item in message.get('output', []) if item.get('type') != 'reasoning']
    elif protocol == 'anthropic_messages':
        message['content'] = [block for block in message.get('content', [])
                              if block.get('type') not in {'thinking', 'redacted_thinking'}]
    elif protocol == 'gemini':
        original = message.get('parts', [])
        kept = [(index, part) for index, part in enumerate(original) if not part.get('thought')]
        message['parts'] = [part for _, part in kept]
        ids = message.get('call_ids', {})
        message['call_ids'] = {str(new): ids[str(old)] for new, (old, _) in enumerate(kept) if str(old) in ids}
    return message


def canonical_messages(client, messages, connection, session_id=None):
    from core.tool_visuals import with_tool_visuals
    messages = with_tool_visuals(messages, images_enabled=connection.options.get('compat_images') != 'off')
    result = []
    for message in messages:
        kwargs = message.additional_kwargs or {}
        run_state = kwargs.get('run_state') or {}
        from core.interrupted_context import RETAINED_NOTES
        if isinstance(message, AIMessage) and kwargs.get(RETAINED_NOTES):
            # Обычный текст сохраняется и у провайдеров, отбрасывающих reasoning_content.
            result.extend(client._convert_messages_to_openai([
                AIMessage(content=str(kwargs[RETAINED_NOTES]).replace('<think>', '').replace('</think>', ''), additional_kwargs={
                    CHAT_ID: kwargs.get(CHAT_ID), 'working_notes': True})], session_id=session_id))
            continue
        if isinstance(message, AIMessage) and run_state.get('status') in {'running', 'interrupted', 'stopped', 'unavailable'}:
            continue  # Unfinished runs enter context through resume_protocol, not a display placeholder.
        trace = kwargs.get('provider_history') or (kwargs.get('run_state') or {}).get('provider_history')
        if isinstance(message, AIMessage) and trace and (kwargs.get('provider_fingerprint') or (kwargs.get('run_state') or {}).get('provider_fingerprint')) == connection.fingerprint:
            native_history = messages_from_dict(copy.deepcopy(trace))
            if kwargs.get('_include_model_thoughts') is False:
                for native_message in native_history:
                    if isinstance(native_message, AIMessage):
                        native_message.additional_kwargs['_include_model_thoughts'] = False
                        native_message.content = _without_thinks(native_message.content)
            segments = kwargs.get('run_segments')
            has_answer = segments is None or any(item.get('type') == 'assistant_final' for item in segments)
            if 'projection_final_text' in run_state and native_history and isinstance(native_history[-1], AIMessage) and not native_history[-1].tool_calls:
                current_text = _without_thinks(message.content).strip()
                if not has_answer:
                    native_history.pop()
                elif current_text != str(run_state['projection_final_text']).strip():
                    # A user edit must reach the model. Signed tool exchanges are
                    # retained; the edited final message becomes ordinary text.
                    native_history[-1] = AIMessage(content=current_text)
            # Public card IDs belong to the display projection. Replay keeps
            # signed/native payloads intact, but must reattach the final card's
            # identity outside those payloads instead of dropping it.
            if has_answer and valid_chat_id(kwargs.get(CHAT_ID)):
                for native_message in reversed(native_history):
                    if isinstance(native_message, AIMessage) and not native_message.tool_calls:
                        native_message.additional_kwargs[CHAT_ID] = kwargs[CHAT_ID]
                        break
            result.extend(canonical_messages(client, native_history, connection, session_id))
            continue
        native = kwargs.get('provider_data') or {}
        if isinstance(message, AIMessage) and native.get('fingerprint') == connection.fingerprint:
            # Keep exact native blocks/items rather than rebuilding signed content.
            item = copy.deepcopy(native.get('message') or {})
            if kwargs.get('_include_model_thoughts') is False:
                item = _without_native_reasoning(item, connection.protocol)
            if connection.protocol == 'chat_completions':
                item.setdefault('role', 'assistant')
                item.setdefault('content', _without_thinks(message.content))
            else:
                item = {'role': 'assistant', 'content': _without_thinks(message.content), '_native': item}
            metadata = model_message_metadata('assistant', kwargs.get(CHAT_ID))
            if metadata:
                result.append(metadata)
            if connection.protocol == 'chat_completions':
                item['_toshy_native'] = True
            result.append(item)
        else:
            result.extend(client._convert_messages_to_openai([message], session_id=session_id))
    return result


def _responses_content(content, assistant=False):
    if isinstance(content, str):
        return content
    result = []
    for part in content or []:
        kind = part.get('type')
        if kind == 'text':
            result.append({'type': 'output_text' if assistant else 'input_text', 'text': part['text']})
        elif kind == 'image_url' and not assistant:
            result.append({'type': 'input_image', 'image_url': part['image_url']['url']})
        else:
            raise ValueError('Этот тип вложения не поддержан транспортом Responses. Используйте поддерживаемый формат или Chat Completions.')
    return result


def responses_input(messages):
    result = []
    for message in messages:
        role = message['role']
        if message.get('_native'):
            result.extend(copy.deepcopy(message['_native'].get('output') or []))
        elif role == 'tool':
            result.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
        else:
            content = _without_thinks(message.get('content') or '') if role == 'assistant' else message.get('content') or ''
            if content:
                result.append({'role': role, 'content': _responses_content(content, role == 'assistant')})
            for call in message.get('tool_calls') or []:
                result.append({'type': 'function_call', 'call_id': call['id'], **call['function']})
    return result


def _anthropic_content(content):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    result = []
    for part in content or []:
        if part.get('type') == 'text':
            result.append({'type': 'text', 'text': part['text']})
        elif part.get('type') == 'image_url':
            url = part['image_url']['url']
            if url.startswith('data:'):
                header, data = url.split(',', 1)
                result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': header[5:].split(';')[0], 'data': data}})
            else:
                result.append({'type': 'image', 'source': {'type': 'url', 'url': url}})
        else:
            raise ValueError('Anthropic Messages: поддержаны текст и изображения; это вложение не поддерживается.')
    return result


def anthropic_input(messages):
    system, result, metadata = [], [], []
    for message in messages:
        role = message['role']
        if role == 'system':
            # Only the main system prompt is hoisted. Per-message IDs remain beside
            # their message, so a provider cannot relocate all IDs to the beginning.
            if result or system:
                metadata.extend(_anthropic_content(message.get('content', '')))
            else:
                system.extend(_anthropic_content(message.get('content', '')))
            continue
        if role == 'tool':
            role, content = 'user', [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': message['content']}]
        elif message.get('_native'):
            content = copy.deepcopy(message['_native'].get('content') or [])
        else:
            content = _anthropic_content(_without_thinks(message.get('content') or '') if role == 'assistant' else message.get('content') or '')
            for call in message.get('tool_calls') or []:
                content.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
        if metadata:
            # Do not insert blocks inside an authenticated thinking sequence.
            if role == 'user':
                content = metadata + content
            else:
                # Anthropic has no per-message system role. Put the annotation
                # immediately before the assistant as user-side app context;
                # never modify authenticated thinking/text blocks themselves.
                if result and result[-1]['role'] == 'user':
                    result[-1]['content'].extend(metadata)
                else:
                    result.append({'role': 'user', 'content': metadata})
            metadata = []
        if not content:
            continue
        if result and result[-1]['role'] == role:
            result[-1]['content'].extend(content)
        else:
            result.append({'role': role, 'content': content})
    return system, result


def build_request(connection, messages, tools=None, response_format=None):
    from core.model_compatibility import prepare_messages, available_tools
    messages = prepare_messages(connection, messages)
    tools = available_tools(connection, tools)
    if connection.protocol == 'gemini':
        from core.model_gemini import build
        return build(connection, messages, tools, response_format)
    protocol, options = connection.protocol, connection.options
    body = {'model': connection.model, 'stream': True}
    if protocol == 'responses':
        endpoint = '/responses'
        body.update(input=responses_input(messages), store=False, include=['reasoning.encrypted_content'])
        if tools:
            body['tools'] = [{'type': 'function', **tool['function']} for tool in tools]
        if response_format:
            body['text'] = {'format': ({'type': 'json_schema', **response_format['json_schema']}
                                      if response_format.get('type') == 'json_schema' else response_format)}
        body['max_output_tokens'] = int(options.get('max_tokens') or 8192)
    elif protocol == 'anthropic_messages':
        endpoint = '/messages'
        system, native = anthropic_input(messages)
        body.update(system=system, messages=native, max_tokens=int(options.get('max_tokens') or 8192))
        if tools:
            body['tools'] = [{'name': tool['function']['name'], 'description': tool['function'].get('description', ''),
                              'input_schema': tool['function'].get('parameters') or {'type': 'object', 'properties': {}}} for tool in tools]
        if response_format:
            fmt = response_format.get('json_schema') or {}
            if response_format.get('type') == 'json_schema':
                body['output_config'] = {'format': {'type': 'json_schema', 'schema': fmt.get('schema', {})}}
            else:
                body['system'].append({'type': 'text', 'text': 'Return valid JSON only, without markdown fences.'})
    else:
        endpoint = '/chat/completions'
        body['messages'] = messages
        if tools:
            body['tools'] = tools
        if response_format:
            body['response_format'] = response_format
        if options.get('max_tokens'):
            body[str(options.get('token_parameter') or 'max_tokens')] = int(options['max_tokens'])
        if options.get('include_usage', True):
            body['stream_options'] = {'include_usage': True}
    # Omit temperature by default. Models that forbid it should never receive an
    # invented value. Arbitrary future parameters can be entered by the user.
    if options.get('temperature') is not None:
        body['temperature'] = float(options['temperature'])
    body.update(copy.deepcopy(options.get('extra_body') or {}))
    # These fields belong to the transport, never to user overrides.
    body['model'], body['stream'] = connection.model, True
    if protocol == 'responses':
        body['input'], body['store'] = responses_input(messages), False
    else:
        body['messages'] = anthropic_input(messages)[1] if protocol == 'anthropic_messages' else messages
    from core.reasoning_effort import apply
    apply(body, connection)
    from core.model_providers import provider_for
    provider = provider_for(connection.options)
    if provider is not None and callable(getattr(provider, 'prepare_request', None)):
        body = provider.prepare_request(connection, body)
    return connection.base_url + endpoint, body


async def sse_events(response):
    """SSE records, including multiline data, comments, and a final unterminated record."""
    data = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield '\n'.join(data)
                data = []
        elif line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
    if data:
        yield '\n'.join(data)


def _merge_details(target, fragments):
    for fragment in fragments or []:
        index = fragment.get('index', len(target))
        block = target.setdefault(index, {})
        for key, value in fragment.items():
            if key in {'text', 'summary', 'data', 'signature'} and isinstance(value, str):
                block[key] = block.get(key, '') + value
            elif value is not None:
                block[key] = copy.deepcopy(value)


class StreamDecoder:
    def __init__(self, connection):
        self.connection = connection
        self.terminal = False
        self.finish_reason = None
        self.message = {'role': 'assistant', 'content': ''}
        self.calls, self.details, self.blocks, self.outputs = {}, {}, {}, {}
        from core.model_gemini import GeminiDecoder
        self.gemini = GeminiDecoder()

    def feed(self, event):
        protocol = self.connection.protocol
        if event.get('error') or event.get('type') in {'error', 'response.failed'}:
            detail = str(event.get('error') or event.get('response', {}).get('error') or event)
            if self.connection.key:
                detail = detail.replace(self.connection.key, '[ключ скрыт]')
            raise ProviderStreamError('Ошибка API в потоке: ' + detail[:1200])
        if event.get('usage'):
            core_logger.event('stats', 'MODEL_USAGE', json.dumps(event['usage'], ensure_ascii=False), details=event['usage'])
        if protocol == 'chat_completions':
            return self._chat(event)
        if protocol == 'responses':
            return self._responses(event)
        if protocol == 'gemini':
            deltas, reason = self.gemini.feed(event)
            if reason:
                self.terminal = True
                self.finish_reason = 'stop' if reason == 'STOP' else ('length' if reason == 'MAX_TOKENS' else 'error')
            return deltas
        return self._anthropic(event)

    def _chat(self, event):
        choices = event.get('choices') or []
        if not choices:
            return []
        choice = choices[0]
        delta = copy.deepcopy(choice.get('delta') or {})
        for field in ('content', 'reasoning', 'reasoning_content'):
            if delta.get(field):
                self.message[field] = self.message.get(field, '') + delta[field]
        for field, value in delta.items():
            if field not in {'role', 'content', 'reasoning', 'reasoning_content', 'tool_calls', 'reasoning_details'}:
                self.message[field] = copy.deepcopy(value)
        for call in delta.get('tool_calls') or []:
            index = call.get('index', 0)
            saved = self.calls.setdefault(index, {'type': 'function', 'id': '', 'function': {'name': '', 'arguments': ''}})
            for key, value in call.items():
                if key == 'function':
                    for name, fragment in value.items():
                        saved['function'][name] = saved['function'].get(name, '') + fragment if name in {'arguments', 'name'} else fragment
                elif key != 'index':
                    if isinstance(value, dict):
                        saved.setdefault(key, {}).update(copy.deepcopy(value))
                    else:
                        saved[key] = copy.deepcopy(value)
        _merge_details(self.details, delta.get('reasoning_details'))
        if delta.get('reasoning_details') and not (delta.get('reasoning') or delta.get('reasoning_content')):
            text = ''.join(str(block.get('text') or block.get('summary') or '') for block in delta['reasoning_details'])
            if text:
                delta['reasoning_content'] = text
        if choice.get('finish_reason') is not None:
            self.finish_reason, self.terminal = choice['finish_reason'], True
        return [delta] if delta else []

    def _responses(self, event):
        kind, index = event.get('type'), event.get('output_index', 0)
        if kind == 'response.output_item.added':
            item = copy.deepcopy(event['item'])
            self.outputs[index] = item
            if item.get('type') == 'function_call':
                return [{'tool_calls': [{'index': index, 'id': item['call_id'], 'type': 'function',
                                        'function': {'name': item['name'], 'arguments': item.get('arguments', '')}}]}]
        elif kind == 'response.output_item.done':
            self.outputs[index] = copy.deepcopy(event['item'])
        elif kind == 'response.function_call_arguments.delta':
            return [{'tool_calls': [{'index': index, 'function': {'arguments': event['delta']}}]}]
        elif kind in {'response.output_text.delta', 'response.refusal.delta'}:
            return [{'content': event['delta']}]
        elif kind in {'response.reasoning_summary_text.delta', 'response.reasoning_text.delta'}:
            return [{'reasoning_content': event['delta']}]
        elif kind in {'response.completed', 'response.incomplete'}:
            response = event.get('response') or {}
            if response.get('output') is not None:
                self.outputs = dict(enumerate(copy.deepcopy(response['output'])))
            self.terminal = True
            self.finish_reason = 'length' if kind == 'response.incomplete' else 'stop'
            if response.get('usage'):
                core_logger.event('stats', 'MODEL_USAGE', json.dumps(response['usage'], ensure_ascii=False), details=response['usage'])
        return []

    def _anthropic(self, event):
        kind, index = event.get('type'), event.get('index', 0)
        if kind == 'content_block_start':
            block = copy.deepcopy(event['content_block'])
            self.blocks[index] = block
            if block['type'] == 'tool_use':
                self.calls[index] = ''
                return [{'tool_calls': [{'index': index, 'id': block['id'], 'type': 'function',
                                        'function': {'name': block['name'], 'arguments': ''}}]}]
            if block['type'] == 'text' and block.get('text'):
                return [{'content': block['text']}]
            if block['type'] == 'thinking' and block.get('thinking'):
                return [{'reasoning_content': block['thinking']}]
        elif kind == 'content_block_delta':
            delta, block = event['delta'], self.blocks[index]
            typ = delta['type']
            if typ == 'text_delta':
                block['text'] = block.get('text', '') + delta['text']
                return [{'content': delta['text']}]
            if typ == 'thinking_delta':
                block['thinking'] = block.get('thinking', '') + delta['thinking']
                return [{'reasoning_content': delta['thinking']}]
            if typ == 'signature_delta':
                block['signature'] = block.get('signature', '') + delta['signature']
            if typ == 'input_json_delta':
                self.calls[index] += delta['partial_json']
                return [{'tool_calls': [{'index': index, 'function': {'arguments': delta['partial_json']}}]}]
        elif kind == 'content_block_stop':
            if index in self.calls and self.calls[index]:
                self.blocks[index]['input'] = json.loads(self.calls[index])
        elif kind == 'message_delta':
            self.finish_reason = event.get('delta', {}).get('stop_reason')
        elif kind == 'message_stop':
            self.terminal = True
        return []

    def provider_data(self):
        if self.connection.protocol == 'gemini':
            message = {'parts': copy.deepcopy(self.gemini.parts), 'call_ids': dict(self.gemini.call_ids)}
        elif self.connection.protocol == 'responses':
            message = {'output': [self.outputs[index] for index in sorted(self.outputs)]}
        elif self.connection.protocol == 'anthropic_messages':
            message = {'content': [self.blocks[index] for index in sorted(self.blocks)]}
        else:
            message = self.message
            if self.calls:
                message['tool_calls'] = [self.calls[index] for index in sorted(self.calls)]
            if self.details:
                message['reasoning_details'] = list(self.details.values())
        return {'fingerprint': self.connection.fingerprint, 'message': copy.deepcopy(message)}


def safe_error(response, raw, connection):
    """Provider errors may echo headers/prompts. Display a bounded, redacted message."""
    try:
        obj = json.loads(raw)
        detail = obj.get('error') or obj
        detail = detail.get('message', '') if isinstance(detail, dict) else str(detail)
    except ValueError:
        detail = 'Сервер вернул ошибку без JSON-описания.'
    if connection.key:
        detail = str(detail).replace(connection.key, '[ключ скрыт]')
    if any(token in str(detail).lower() for token in ('system message must be at the beginning', 'system role must be at the beginning', 'only one system')):
        detail = 'Шаблон модели допускает системную инструкцию только в начале запроса. Проверьте историю и расширения, добавляющие сообщения system.'
    normalized = str(detail).lower()
    if any(word in normalized for word in ('tool', 'function calling')) and any(word in normalized for word in ('unsupported', 'not support', 'not allowed')):
        detail = 'Подключение не поддерживает инструменты. Отключите их в разделе «Совместимость». ' + str(detail)
    if any(word in normalized for word in ('image', 'vision', 'multimodal')) and any(word in normalized for word in ('unsupported', 'not support', 'not allowed')):
        detail = 'Подключение не поддерживает этот тип изображения. Проверьте модель и раздел «Совместимость». ' + str(detail)
    hints = {401: 'Проверьте ключ API.', 403: 'Проверьте права ключа и доступ к модели.',
             402: 'Проверьте баланс API.', 429: 'Лимит запросов или токенов. Подождите перед повтором.',
             400: 'Проверьте имя модели, протокол и дополнительные параметры.', 404: 'Проверьте базовый URL и имя модели.'}
    error = ProviderStreamError(f'API HTTP {response.status_code}. {hints.get(response.status_code, "")} {str(detail)[:1200]}'.strip())
    error.usage_rejected = 400 <= response.status_code < 500
    return error


async def stream_completion(client, connection, messages, tools=None, response_format=None, session_id=None,*,raw_request=False,control_reasoning=False,system_prompt_enabled=True):
    try:
        from core.model_providers import authenticate
        connection = await authenticate(connection)
        from core.model_queue import model_slot
        async with model_slot(connection), aclosing(_stream_completion(client, connection, messages, tools, response_format, session_id,raw_request=raw_request,control_reasoning=control_reasoning,system_prompt_enabled=system_prompt_enabled)) as events:
            async for delta in events:yield delta
    except Exception as error:
        # HTTPX can echo an invalid Authorization header in a transport error.
        # Redact before the exception reaches run checkpoints or model logging.
        detail = str(error)
        if connection.key and connection.key in detail:
            raise ProviderStreamError(detail.replace(connection.key, '[ключ скрыт]')) from None
        raise


async def _stream_completion(client, connection, messages, tools=None, response_format=None, session_id=None,*,raw_request=False,control_reasoning=False,system_prompt_enabled=True):
    from core.model_compatibility import discover
    connection = await discover(connection, client)
    if raw_request:
        from dataclasses import replace
        connection=replace(connection,options={k:v for k,v in connection.options.items() if k!='extra_body'})
        tools=None;response_format=None
    formatted = canonical_messages(client, messages, connection, session_id)
    endpoint, payload = build_request(connection, formatted, tools, response_format)
    if raw_request or not system_prompt_enabled:
        payload.pop('system', None)
        payload.pop('systemInstruction', None)
        if not system_prompt_enabled and isinstance(payload.get('messages'), list):
            payload['messages'] = [m for m in payload['messages'] if m.get('role') not in {'system', 'developer'}]
    decoder = StreamDecoder(connection)
    core_logger.event('model', 'MODEL_SEND', f'{connection.model} · {connection.protocol}',
                      details={'messages': len(formatted), 'tools': len(tools or [])})
    from core.token_usage import usage_request
    from core.reasoning_control import completion_control
    async with completion_control(client, connection, payload, enabled=control_reasoning) as control, httpx.AsyncClient(timeout=client.timeout, headers=connection.headers(),
                                 trust_env=connection.options.get('trust_env', True)) as http, usage_request(connection, session_id, messages=formatted, tools=tools) as meter:
        if control_reasoning:
            yield control.change()
        # Only retry an explicitly rejected optional usage field, before any token
        # or tool has been emitted. No silent restarts after a broken stream.
        for attempt in range(4):
            retry = False
            async with http.stream('POST', endpoint, json=payload) as response:
                from core.model_providers import observe
                observe(connection, 'observe_response', response.status_code, dict(response.headers))
                meter.rejected = 400 <= response.status_code < 500
                if response.is_error:
                    raw = (await response.aread()).decode('utf-8', errors='replace')
                    observe(connection, 'observe_error', response.status_code, raw)
                    from core.model_providers import retry_request
                    if attempt < 3 and retry_request(connection, payload, response.status_code, raw):
                        retry = True
                    elif attempt < 3 and response.status_code in {400, 422} and 'reasoning_control' in payload and 'reasoning_control' in raw:
                        payload.pop('reasoning_control')
                        control.armed = False
                        retry = True
                    elif attempt < 3 and response.status_code in {400, 422} and 'stream_options' in payload and any(field in raw for field in ('stream_options', 'include_usage')):
                        payload.pop('stream_options')
                        retry = True
                    elif (attempt < 3 and response.status_code in {400, 422}
                            and (payload.get('response_format') or {}).get('type') == 'json_schema'
                            and any(term in raw.lower() for term in ('response_format', 'json_schema', 'structured output'))
                            and any(term in raw.lower() for term in ('unsupported', 'not supported', 'not support', 'not allowed', 'unknown', 'unrecognized'))):
                        # Some OpenAI-compatible local endpoints only implement
                        # JSON mode. This is an explicit pre-stream rejection;
                        # the pipeline still validates every generated argument.
                        payload['response_format'] = {'type': 'json_object'}
                        retry = True
                    elif (attempt < 3 and response.status_code in {400,422} and payload.get('tool_choice')=='none'
                            and not payload.get('tools') and 'tool_choice' in raw):
                        # Some compatible servers reject this redundant field
                        # when there are no schemas. The runtime gate remains.
                        payload.pop('tool_choice');retry=True
                    else:
                        raise safe_error(response, raw, connection)
                if not retry:
                    async for raw in sse_events(response):
                        if raw == '[DONE]':
                            if connection.protocol != 'responses':
                                decoder.terminal = True
                            break
                        try:
                            event = json.loads(raw)
                        except ValueError as error:
                            raise ProviderStreamError('API прислал повреждённое событие.') from error
                        meter.observe(event)
                        observe(connection, 'observe_event', event)
                        status = control.observe(event)
                        if status and control_reasoning:
                            yield status
                        for delta in decoder.feed(event):
                            from core.model_compatibility import normalize_delta
                            yield normalize_delta(connection, delta)
                        meter.complete |= decoder.terminal
                        # Checkpoint native blocks during streaming too. Incomplete
                        # signed blocks are retained as data, never replayed as valid.
                        if event.get('type') in {'content_block_start', 'content_block_stop', 'response.output_item.added', 'response.output_item.done', 'response.completed'} or connection.protocol == 'gemini':
                            yield {'_provider_data': decoder.provider_data()}
            if not retry:
                break
        meter.complete |= decoder.terminal
        control.active = False
        status = control.change()
        if status and control_reasoning:
            yield status
    if not decoder.terminal:
        raise ProviderStreamError('Соединение с API прервано до завершения ответа.')
    if decoder.finish_reason in {'length', 'max_tokens', 'content_filter', 'error'}:
        raise ProviderStreamError('Генерация прервана сервером: ' + str(decoder.finish_reason))
    yield {'_provider_data': decoder.provider_data()}


async def completion(client, connection, messages, tools=None, response_format=None, session_id=None):
    content, reasoning, calls, provider_data = '', '', {}, {}
    async for delta in stream_completion(client, connection, messages, tools, response_format, session_id):
        content += delta.get('content') or ''
        reasoning += delta.get('reasoning_content') or delta.get('reasoning') or ''
        provider_data = delta.get('_provider_data') or provider_data
        for call in delta.get('tool_calls') or []:
            saved = calls.setdefault(call.get('index', 0), {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
            saved['id'] = call.get('id') or saved['id']
            function = call.get('function') or {}
            saved['function']['name'] = function.get('name') or saved['function']['name']
            saved['function']['arguments'] += function.get('arguments') or ''
    return {'choices': [{'message': {'role': 'assistant', 'content': content, 'reasoning_content': reasoning,
                                    'tool_calls': list(calls.values()), '_provider_data': provider_data},
                         'finish_reason': 'tool_calls' if calls else 'stop'}]}
