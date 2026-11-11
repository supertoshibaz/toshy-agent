"""Gemini GenerateContent REST adapter, retaining original ordered parts."""
import copy
import json
import uuid
from urllib.parse import quote


def parts(content):
    if isinstance(content, str):
        return [{'text': content}] if content else []
    result = []
    for part in content or []:
        if part.get('type') == 'text':
            result.append({'text': part['text']})
        elif part.get('type') == 'image_url':
            url = part['image_url']['url']
            if not url.startswith('data:'):
                raise ValueError('Gemini: передайте изображение как вложение, а не внешний URL.')
            header, data = url.split(',', 1)
            result.append({'inlineData': {'mimeType': header[5:].split(';')[0], 'data': data}})
        else:
            raise ValueError('Gemini: этот тип вложения пока не поддерживается; поддержаны текст и изображения.')
    return result


def build(connection, messages, tools, response_format):
    system, contents, names = [], [], {}
    for message in messages:
        role = message['role']
        if role == 'system':
            system.extend(parts(message.get('content', '')))
            continue
        if role == 'tool':
            call_id = message['tool_call_id']
            if call_id not in names:
                raise ValueError('Gemini: не найден исходный вызов инструмента ' + call_id)
            name, native_id = names[call_id]
            response = {'name': name, 'response': {'result': message.get('content', '')}}
            if native_id:
                response['id'] = native_id
            body = [{'functionResponse': response}]
            role = 'user'
        elif message.get('_native'):
            body = copy.deepcopy(message['_native'].get('parts') or [])
            role = 'model'
            for index, part in enumerate(body):
                call = part.get('functionCall')
                if call:
                    identity = call.get('id') or message['_native'].get('call_ids', {}).get(str(index)) or f'gemini_call_{index}'
                    names[identity] = (call['name'], call.get('id'))
        else:
            body = parts(message.get('content', ''))
            role = 'model' if role == 'assistant' else 'user'
            for call in message.get('tool_calls') or []:
                function = call['function']
                names[call['id']] = (function['name'], None)
                body.append({'functionCall': {'name': function['name'], 'args': json.loads(function['arguments'])}})
        if not body:
            continue
        # Keep signed model parts in their original blocks and order.
        if contents and role == 'user' and contents[-1]['role'] == role:
            contents[-1]['parts'].extend(body)
        else:
            contents.append({'role': role, 'parts': body})
    config = {}
    if connection.options.get('max_tokens'):
        config['maxOutputTokens'] = int(connection.options['max_tokens'])
    if connection.options.get('temperature') is not None:
        config['temperature'] = float(connection.options['temperature'])
    if response_format:
        config['responseMimeType'] = 'application/json'
        if response_format.get('type') == 'json_schema':
            config['responseJsonSchema'] = response_format['json_schema']['schema']
    body = copy.deepcopy(connection.options.get('extra_body') or {})
    body['generationConfig'] = {**body.get('generationConfig', {}), **config}
    from core.reasoning_effort import apply
    apply(body, connection)
    body['contents'] = contents
    if system:
        body['systemInstruction'] = {'parts': system}
    if tools:
        body['tools'] = [{'functionDeclarations': [
            {'name': t['function']['name'], 'description': t['function'].get('description', ''),
             'parametersJsonSchema': t['function'].get('parameters', {})} for t in tools]}]
    else:
        body.pop('tools', None)
    model = quote(connection.model.removeprefix('models/'), safe='')
    return connection.base_url + '/models/' + model + ':streamGenerateContent?alt=sse', body


class GeminiDecoder:
    def __init__(self):
        self.parts = []
        self.call_ids = {}

    def feed(self, event):
        candidates = event.get('candidates') or []
        if not candidates:
            if (event.get('promptFeedback') or {}).get('blockReason'):
                raise ValueError('Gemini отклонил запрос: ' + event['promptFeedback']['blockReason'])
            return [], None
        candidate = candidates[0]
        result = []
        for part in candidate.get('content', {}).get('parts', []):
            index = len(self.parts)
            self.parts.append(copy.deepcopy(part))
            if 'text' in part:
                result.append({'reasoning_content' if part.get('thought') else 'content': part['text']})
            if part.get('functionCall'):
                call = part['functionCall']
                identity = call.get('id') or 'gemini_' + uuid.uuid4().hex
                self.call_ids[str(index)] = identity
                result.append({'tool_calls': [{'index': index, 'id': identity,
                    'type': 'function', 'function': {'name': call['name'], 'arguments': json.dumps(call.get('args') or {})}}]})
            if not any(k in part for k in ('text', 'functionCall', 'thoughtSignature', 'thought')):
                raise ValueError('Gemini вернул неподдерживаемый тип результата. Выберите модель с текстовым выводом.')
        return result, candidate.get('finishReason')
