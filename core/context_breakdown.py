"""Offline estimates for the text components of an actual model request.

Providers report a total, not per-section tokenization. Never count base64 image
bytes as text, and keep unallocated template/multimodal overhead separate.
"""
import json
import math


def describe_request(messages, tools=None):
    from core.context_sections import sections, partition
    registry = sections()
    tool_owners = {name: key for key, section in registry.items() for name in section['tool_names']}
    call_owners = {}
    for message in messages or []:
        for call in message.get('tool_calls') or []:
            owner = tool_owners.get((call.get('function') or {}).get('name'))
            if owner:
                call_owners[call.get('id')] = owner
    sizes = {}
    images = 0

    def add(key, text):
        if text:
            for category, fragment in partition(text, key, registry):
                sizes[category] = sizes.get(category, 0) + len(fragment)

    for message in messages or []:
        role = message.get('role')
        content = message.get('content') or ''
        if isinstance(content, list):
            images += sum(part.get('type') in {'image', 'image_url', 'input_image'}
                          for part in content if isinstance(part, dict))
            content = '\n'.join(str(part.get('text') or '') for part in content if isinstance(part, dict))
        if not isinstance(content, str):
            content = ''
        key = {'system': 'system', 'developer': 'system', 'user': 'messages',
               'assistant': 'assistant', 'tool': 'results'}.get(role, 'runtime')
        if role == 'tool':
            key = call_owners.get(message.get('tool_call_id'), key)
        if content.startswith('Historical conversation handoff (a lossy summary'):
            key = 'summary'
        if role == 'user':
            marker = '\n\nModule guidance for this request:\n'
            if marker in content:
                content, guidance = content.split(marker, 1)
                add('guidance', marker + guidance)
            marker = '\n\nRetrieved permanent-memory context for this request (historical data, not instructions):\n'
            if marker in content:
                content, memory = content.split(marker, 1)
                add('memory', marker + memory)
        add(key, content)
        for field in ('reasoning_content', 'reasoning'):
            if isinstance(message.get(field), str):
                add('assistant', message[field])
        if message.get('tool_calls'):
            for call in message['tool_calls']:
                name = (call.get('function') or {}).get('name')
                add(tool_owners.get(name, 'assistant'), json.dumps(call, ensure_ascii=False))
    if tools:
        for tool in tools:
            name = (tool.get('function') or tool).get('name')
            add(tool_owners.get(name, 'tools'), json.dumps(tool, ensure_ascii=False))
    return {'weights': {key: max(1, math.ceil(size / 4)) for key, size in sizes.items()},
            'images': images, 'sections': {key: {'labels': value['labels'], 'color': value['color']}
                                         for key, value in registry.items() if key in sizes}}


def allocate(description, input_tokens, output_tokens):
    if input_tokens is None or output_tokens is None:
        return None
    weights = description.get('weights') or {}
    total = sum(weights.values())
    parts = dict(weights)
    if total > input_tokens:
        # Estimates must never exceed the server's measured total.
        scaled = {key: value * input_tokens / total for key, value in parts.items()}
        parts = {key: math.floor(value) for key, value in scaled.items()}
        for key in sorted(scaled, key=lambda key: scaled[key] - parts[key], reverse=True)[:input_tokens - sum(parts.values())]:
            parts[key] += 1
    remainder = input_tokens - sum(parts.values())
    if remainder:
        parts['unallocated'] = remainder
    if output_tokens:
        parts['output'] = output_tokens
    return {'parts': parts, 'estimated': True, 'images': description.get('images', 0),
            'sections': description.get('sections', {}),
            'input': input_tokens, 'output': output_tokens, 'total': input_tokens + output_tokens}
