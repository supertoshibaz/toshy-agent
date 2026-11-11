"""Preserve model input and partial assistant text across native continuation."""
import copy
import re
from langchain_core.messages import AIMessage, message_to_dict, messages_from_dict


def is_native_resume(client, context, resume):
    return bool(resume and context.resume_mode == 'native'
                and hasattr(client, 'model_connection')
                and client.model_connection().mode == 'local')


def can_continue_partial(resume):
    partial = (resume or {}).get('partial') or {}
    # Partial tool JSON is not an executable call or a text prefill.
    return bool((partial.get('content') or partial.get('reasoning')) and not partial.get('tool_calls'))


def _request_context(context):
    if context is None:
        return None
    names = ('workspace_root', 'project_system_prompt', 'knowledge_base_snapshot_text',
             'allowed_modules', 'include_thoughts_in_context', 'include_replied_thoughts_in_context',
             'reasoning_effort', 'full_web_pages')
    values = {name: getattr(context, name) for name in names}
    values['workspace_root'] = str(values['workspace_root'])
    if values['allowed_modules'] is not None:
        values['allowed_modules'] = list(values['allowed_modules'])
    return values


def request_snapshot(client, messages, tools, context=None):
    connection = client.model_connection() if hasattr(client, 'model_connection') else None
    return {'messages': [message_to_dict(m) for m in messages], 'tools': copy.deepcopy(tools),
            'fingerprint': getattr(connection, 'fingerprint', ''), 'context': _request_context(context)}


def restored_request(client, resume, context=None):
    saved = (resume or {}).get('model_request') or {}
    connection = client.model_connection() if hasattr(client, 'model_connection') else None
    if not saved.get('messages') or saved.get('fingerprint') != getattr(connection, 'fingerprint', None):
        return None
    if saved.get('context') is not None and saved['context'] != _request_context(context):
        return None
    messages = messages_from_dict(copy.deepcopy(saved['messages']))
    if not messages or messages[0].type != 'system':
        return None
    return messages, copy.deepcopy(saved.get('tools'))


def partial_message(client, partial):
    content, reasoning = str(partial.get('content') or ''), str(partial.get('reasoning') or '')
    kwargs = {'continuation_prefix': True}
    if partial.get('parsed_reasoning') or reasoning:
        kwargs['resume_reasoning_content'] = reasoning
    return AIMessage(content=content, additional_kwargs=kwargs)


def visible_prefix(partial):
    content = str(partial.get('content') or '')
    if partial.get('parsed_reasoning') or partial.get('reasoning'):
        return content
    return re.sub(r'<think>[\s\S]*?(?:</think>|$)', '', content)


def complete_provider_data(data, partial):
    if not data or not isinstance(data.get('message'), dict):
        return data
    result = copy.deepcopy(data)
    message = result['message']
    content, reasoning = str(partial.get('content') or ''), str(partial.get('reasoning') or '')
    if not (partial.get('parsed_reasoning') or reasoning):
        notes = re.match(r'^<think>([\s\S]*?)(?:</think>\s*|$)', content)
        if notes:
            reasoning, content = notes.group(1), content[notes.end():]
    message['content'] = content + str(message.get('content') or '')
    if reasoning:
        field = 'reasoning' if 'reasoning' in message and 'reasoning_content' not in message else 'reasoning_content'
        message[field] = reasoning + str(message.get(field) or '')
    return result
