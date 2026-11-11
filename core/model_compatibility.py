"""Profile-scoped wire compatibility. Never changes saved chat or signed blocks."""
import copy
import json
import re

REASONING = {'auto', 'reasoning_content', 'reasoning', 'inline', 'omit'}
CAPABILITIES = {'auto', 'on', 'off'}

def validate_options(options):
    levels = options.get('reasoning_effort_levels')
    if levels is not None:
        from core.reasoning_effort import valid_level
        if not isinstance(levels, list) or any(not valid_level(level) for level in levels):
            raise ValueError('Уровни effort должны быть списком непустых имён без пробелов, не длиннее 64 символов.')
    for key in ('tools', 'images'):
        if options.get('compat_' + key, 'auto') not in CAPABILITIES:
            raise ValueError('Неизвестный режим совместимости: ' + key)
    if options.get('compat_reasoning', 'auto') not in REASONING:
        raise ValueError('Неизвестный формат размышлений.')

def annotate(content, text):
    if isinstance(content, list):
        return [{'type': 'text', 'text': text}] + copy.deepcopy(content)
    content = str(content or '')
    # Keep a local thought prefix intact, including assistant-prefill detection.
    match = re.match(r'^(<think>[\s\S]*?</think>\s*)', content)
    if match:
        return match[1] + text + '\n\n' + content[match.end():]
    return text + '\n\n' + content

def available_tools(connection, tools):
    return None if connection.options.get('compat_tools') == 'off' else tools

def prepare_messages(connection, messages):
    result = copy.deepcopy(messages)
    mode = connection.options.get('compat_reasoning', 'auto')
    seen_non_system = False
    for message in result:
        role = message['role']
        if role == 'system':
            if seen_non_system:
                raise ValueError('Системная инструкция внутри истории несовместима с шаблонами моделей. Оставьте её в начале запроса.')
        else:
            seen_non_system = True
        content = message.get('content')
        if isinstance(content, list) and connection.options.get('compat_images') == 'off':
            if any(p.get('type') in {'image_url', 'input_image'} for p in content):
                raise ValueError('Изображения отключены в разделе «Совместимость» этого подключения.')
        if role != 'assistant' or message.get('_native'):
            continue
        # Provider-native messages are already exact protocol data. Changing a
        # reasoning mode must not invalidate signed history.
        if message.get('_toshy_native'):
            message.pop('_toshy_native', None)
            continue
        thoughts = []
        if isinstance(content, str):
            thoughts = re.findall(r'<think>([\s\S]*?)</think>', content)
            content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content)
        existing = message.pop('reasoning_content', None) or message.pop('reasoning', None)
        if existing:
            thoughts.insert(0, existing)
        message['content'] = content
        if not thoughts:
            continue
        text = '\n'.join(thoughts)
        selected = ('reasoning_content' if connection.mode == 'local' else 'omit') if mode == 'auto' else mode
        if selected == 'inline':
            message['content'] = '<think>' + text + '</think>\n' + str(content or '')
        elif selected in {'reasoning', 'reasoning_content'}:
            message[selected] = text
    # Some templates also require exactly one leading system message.
    while len(result) > 1 and result[0]['role'] == result[1]['role'] == 'system':
        result[0]['content'] = str(result[0]['content']) + '\n\n' + str(result.pop(1)['content'])
    return result

def normalize_delta(connection, delta):
    delta = copy.deepcopy(delta)
    if connection.options.get('compat_tools') == 'off' and delta.get('tool_calls'):
        raise ValueError('Модель запросила инструмент, хотя инструменты отключены в профиле. Вызов не выполнен.')
    # Both widely used fields normalize to the UI's reasoning channel.
    if delta.get('reasoning') and not delta.get('reasoning_content'):
        delta['reasoning_content'] = delta.pop('reasoning')
    for call in delta.get('tool_calls') or []:
        function = call.get('function') or {}
        if 'parameters' in function:
            # Some compatible servers send an object under parameters instead
            # of an OpenAI arguments fragment. Never silently turn it into {}.
            parameters = function.pop('parameters')
            if 'arguments' in function:
                raise ValueError('Ambiguous tool arguments: arguments and parameters are both present. No tool was executed.')
            if not isinstance(parameters, dict):
                raise ValueError('Tool parameters must be an object. No tool was executed.')
            function['arguments'] = json.dumps(parameters, ensure_ascii=False)
    return delta

async def discover(connection, client):
    """Use explicit llama.cpp capability flags only; absence means unknown.

    No paid generation probes. Manual settings take precedence. Short-lived cache
    is client-owned, so changing models/server state is eventually observed.
    """
    if connection.mode != 'local':
        return connection
    import time
    import httpx
    from dataclasses import replace
    cache = getattr(client, '_compatibility_cache', None)
    if cache is None:
        client._compatibility_cache = cache = {}
    now = time.monotonic()
    key = (connection.fingerprint, connection.key)
    cached = cache.get(key)
    if cached and now - cached[0] < 30:
        flags = cached[1]
    else:
        flags = {}
        try:
            root = connection.base_url.rstrip('/').removesuffix('/v1')
            async with httpx.AsyncClient(timeout=2, headers=connection.headers(),
                    trust_env=connection.options.get('trust_env', True)) as http:
                response = await http.get(root + '/props')
                if response.status_code == 200:
                    props = response.json()
                    if isinstance(props, dict):
                        flags['_llama_cpp'] = bool(props.get('build_info') or 'chat_template' in props
                                                   or 'chat_template_caps' in props)
                        tools = (props.get('chat_template_caps') or {}).get('supports_tool_calls')
                        vision = (props.get('modalities') or {}).get('vision')
                        if isinstance(tools, bool):
                            flags['tools'] = tools
                        if isinstance(vision, bool):
                            flags['images'] = vision
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        cache[key] = (now, flags)
    options = dict(connection.options)
    for capability, supported in flags.items():
        if options.get('compat_' + capability, 'auto') == 'auto':
            options['compat_' + capability] = 'on' if supported else 'off'
    return replace(connection, options=options)
