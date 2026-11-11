"""Возможности reasoning определяются моделью, а не одним общим списком для всех API."""
import re
import ast
from core.context import get_execution_context

def valid_level(value):
    return (isinstance(value, str) and 0 < len(value) <= 64
            and all(character.isprintable() and not character.isspace() for character in value))


def normalize_levels(values):
    # Имена и порядок принадлежат серверу/шаблону, включая будущие уровни.
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value for value in values if valid_level(value)))


def _local_template(props):
    template = (props or {}).get('chat_template')
    if isinstance(template, dict):
        template = template.get('default') or next(iter(template.values()), '')
    return template if isinstance(template, str) else ''


def supports_non_reasoning(connection, props=None):
    if 'none' in capabilities(connection, props):
        return True
    if connection.mode != 'local':
        return False
    # Переключатель thinking независим от списка уровней effort.
    # Старый /props не сообщает отдельный флаг, поэтому читаем сам шаблон.
    expressions = re.findall(r'{[%{]-?([\s\S]*?)-?[%}]}', _local_template(props))
    return any(re.search(r'\benable_thinking\b', expression) for expression in expressions)


def _template_levels(template):
    if not isinstance(template, str) or 'reasoning_effort' not in template:
        return []
    # Читаем только явную проверку допустимых значений в Jinja, без исполнения шаблона.
    expressions = re.findall(r'{[%{]-?([\s\S]*?)-?[%}]}', template)
    values = []
    for expression in expressions:
        for match in re.finditer(r'\b\w*reasoning_effort\s+(?:not\s+)?in\s*(\([^)]*\)|\[[^\]]*\])', expression):
            try:
                values.extend(normalize_levels(ast.literal_eval(match[1])))
            except (ValueError, SyntaxError):
                continue
    if values:
        return normalize_levels(values)
    # Некоторые шаблоны описывают режимы отдельными ветвями if/elif.
    for expression in expressions:
        values.extend(match[2] for match in re.finditer(
            r'''\b\w*reasoning_effort\s*==\s*(['"])([^'"\n]+)\1''', expression))
    return normalize_levels(values)


def capabilities(connection, props=None):
    explicit = connection.options.get('reasoning_effort_levels')
    if explicit is not None:
        return normalize_levels(explicit)
    if connection.mode == 'local':
        caps = (props or {}).get('chat_template_caps') or {}
        advertised = caps.get('reasoning_effort_levels')
        if advertised is None:
            advertised = (props or {}).get('reasoning_effort_levels')
        if isinstance(advertised, list):
            return normalize_levels(advertised)
        # В старом llama.cpp списка нет: извлекаем только явно объявленные имена.
        # Не угадываем уровни по названию GGUF или семейства модели.
        return _template_levels(_local_template(props))
    model = connection.model.lower().split('/')[-1]
    if connection.protocol in {'responses', 'chat_completions'}:
        if model.startswith(('qwen3.8-27b', 'qwen3.8-max', 'qwen3.8-flash', 'qwen3.8-2.4t')):
            return ['low', 'medium', 'xhigh']
        if re.match(r'^(o[134](?:-|$)|gpt-oss-)', model):
            return ['low', 'medium', 'high']
        if model.startswith('gpt-6.1-sol'):
            return ['low', 'medium', 'high', 'xhigh', 'max']
        if model.startswith('gpt-6-astra'):
            return ['minimal', 'low', 'medium', 'high', 'xhigh', 'max']
        if re.match(r'^gpt-5(?:[.-]|$)', model):
            if '-pro' in model:
                return ['high']
            if model.startswith(('gpt-5.2', 'gpt-5.4', 'gpt-5.5')):
                return ['none', 'low', 'medium', 'high', 'xhigh']
            if model.startswith('gpt-5.1'):
                return ['none', 'low', 'medium', 'high']
            return ['minimal', 'low', 'medium', 'high']
    if connection.protocol == 'anthropic_messages':
        if re.match(r'^claude-(?:fable|mythos|opus|sonnet|haiku)-5', model) or model.startswith(('claude-opus-4-7', 'claude-opus-4-8')):
            return ['low', 'medium', 'high', 'xhigh', 'max']
        if model.startswith('claude-opus-4-6'):
            return ['low', 'medium', 'high', 'max']
        if model.startswith(('claude-opus-4-5', 'claude-sonnet-4-6')):
            return ['low', 'medium', 'high']
    if connection.protocol == 'gemini':
        if model.startswith(('gemini-3.8-flash', 'gemini-4-argon')):
            return ['low', 'medium', 'high']
        if model.startswith(('gemini-3.6-flash', 'gemini-3-flash', 'gemini-3.1-flash-lite', 'gemini-3.5-flash-lite')) and 'image' not in model:
            return ['minimal', 'low', 'medium', 'high']
        if model.startswith('gemini-3-pro'):
            return ['low', 'high']
    return []


def apply(body, connection):
    context = get_execution_context()
    effort = getattr(context, 'reasoning_effort', '') if context else ''
    if not effort:
        return
    if not valid_level(effort):
        raise ValueError('Некорректное имя уровня reasoning effort.')
    if connection.protocol == 'responses':
        body.setdefault('reasoning', {})['effort'] = effort
    elif connection.protocol == 'anthropic_messages':
        body.setdefault('output_config', {})['effort'] = effort
    elif connection.protocol == 'gemini':
        body.setdefault('generationConfig', {}).setdefault('thinkingConfig', {})['thinkingLevel'] = effort
    else:
        body['reasoning_effort'] = effort
        if connection.mode == 'local':
            # Явные template kwargs не должны перекрывать выбранный уровень.
            kwargs = body.setdefault('chat_template_kwargs', {})
            kwargs['reasoning_effort'] = effort
            if effort == 'none':
                kwargs['enable_thinking'] = False
            else:
                # Финальный ответ/продолжение ответа могут явно запрещать новые мысли.
                kwargs.setdefault('enable_thinking', True)
