"""Durable checkpoints in the session SQLite database."""
import copy
import re
import time
from pathlib import Path
from core.sqlite_storage import SQLiteStore

from langchain_core.messages import AIMessage, ToolMessage, message_to_dict, messages_from_dict


def checkpoint_path(storage_dir, run_id):
    if not run_id or not str(run_id).isalnum():
        raise ValueError('Неверный ID запуска')
    return Path(storage_dir) / 'session.sqlite'


def load_checkpoint(storage_dir, run_id):
    return SQLiteStore(checkpoint_path(storage_dir, run_id)).read_run(run_id)


def save_checkpoint(storage_dir, data):
    SQLiteStore(checkpoint_path(storage_dir, data['run_id'])).write_run(data)


def fold_completed_segments(segments):
    """Не объединяем мысли, ответы и время через границу пользовательской правки."""
    if not any(item.get('type') == 'user_update' for item in segments):
        return _fold_completed_phase(segments)
    result, phase = [], []
    for item in segments:
        if item.get('type') != 'user_update':
            phase.append(item)
            continue
        folded = _fold_completed_phase(phase)
        for segment in folded:
            if segment.get('type') == 'assistant_final':
                segment['type'] = 'assistant_interim'
        result.extend(folded)
        result.append(copy.deepcopy(item))
        phase = []
    result.extend(_fold_completed_phase(phase))
    return result


def _fold_completed_phase(segments):
    """Проекция одной фазы; исходный протокол и исходные сегменты неизменны."""
    result = copy.deepcopy(segments)
    answers = [index for index, item in enumerate(result)
               if item.get('type') in {'assistant_stream', 'assistant_interim', 'assistant_final'}
               and str(item.get('text') or '').strip()]
    last = answers[-1] if answers else None
    first_id = next((item.get('chat_id') for item in result
                     if item.get('type') in {'reasoning', 'tool'} and item.get('chat_id')), None)
    if first_id is None:
        first_id = next((item.get('chat_id') for index, item in enumerate(result)
                         if index != last and item.get('chat_id')), None)
    seconds = sum(float(item.get('phase_seconds') or 0) for item in result)
    thought = []
    for index, item in enumerate(result):
        if index == last:
            continue
        if item.get('type') in {'assistant_stream', 'assistant_interim', 'assistant_final'}:
            item['type'] = 'reasoning'
        item.pop('phase_seconds', None)
        if item.get('type') in {'reasoning', 'tool', 'subagents', 'user_update', 'pipeline_notification'}:
            if first_id and item.get('type') not in {'subagents', 'pipeline_notification'}:
                item['chat_id'] = first_id
            thought.append(item)
    if thought:
        thought[0]['phase_seconds'] = round(seconds, 3)
    thought.sort(key=lambda item: item.get('type') == 'subagents')
    if last is not None:
        final = result[last]
        final['type'] = 'assistant_final'
        final.pop('phase_seconds', None)
        thought.append(final)
    return thought


def repair_steered_projection(metadata, content=None):
    """Восстанавливаем порядок старой ошибочной UI-проекции по сохранённому протоколу.

    Сам протокол и SQLite не переписываем. Утраченное время отдельных фаз
    не восстанавливаем догадками: сохраняем общий итог и отмечаем неизвестность.
    """
    source = metadata.get('run_segments') or []
    state = metadata.get('run_state') or {}
    updates = [item for item in source if item.get('type') == 'user_update']
    def user_id(item):
        return item.get('message', {}).get('data', {}).get('additional_kwargs', {}).get('chat_id')
    if (not state.get('steered_protocol') or state.get('projection_layout_version') == 2
            or not any(user_id(item) and user_id(item) != item.get('chat_id') for item in updates)):
        return metadata
    if content is not None and 'projection_thought_text' in state:
        shown = '\n'.join(re.findall(r'<think>([\s\S]*?)</think>', str(content)))
        if shown.replace('\n', '') != str(state['projection_thought_text']).replace('\n', ''):
            return metadata  # Не заменяем вручную отредактированные мысли.
    from core.chat_references import stable_chat_id
    originals = {user_id(item): item for item in updates if user_id(item)}
    tools = {item.get('id'): item for item in source if item.get('type') == 'tool'}
    restored, used_tools, phase = [], set(), 0
    run_id = str(state.get('run_id') or 'legacy-steering')
    trace = messages_from_dict(state['steered_protocol'])
    for number, message in enumerate(trace):
        identity = message.additional_kwargs.get('chat_id')
        if message.type == 'human' and identity in originals:
            item = copy.deepcopy(originals[identity])
            item['chat_id'] = identity
            restored.append(item)
            phase += 1
            continue
        if not isinstance(message, AIMessage):
            continue
        if message.additional_kwargs.get('interrupted_reasoning_snapshot'):
            continue
        text = str(message.content or '')
        if message.additional_kwargs.get('working_notes') and text.startswith(
                ('Saved working notes from', 'Saved work before interruption')):
            continue
        thought_id = stable_chat_id(f'{run_id}:phase:{phase}')
        for part_index, part in enumerate(re.split(r'(<think>[\s\S]*?</think>)', text)):
            if part.startswith('<think>') and part.endswith('</think>'):
                restored.append({'type': 'reasoning', 'text': part[7:-8], 'chat_id': thought_id,
                                 'phase_timing_unknown': True})
            elif part.strip():
                restored.append({'type': 'assistant_stream', 'text': part.removeprefix('\n'),
                                 'chat_id': stable_chat_id(f'{run_id}:answer:{number}:{part_index}')})
        for call in message.tool_calls:
            if call['id'] in tools:
                item = copy.deepcopy(tools[call['id']])
                item.update(chat_id=thought_id, phase_timing_unknown=True)
                restored.append(item)
                used_tools.add(call['id'])
    if not any(item.get('type') == 'user_update' for item in restored):
        return metadata
    # Дополнительные UI-данные не теряются, даже если старый протокол неполон.
    restored.extend(copy.deepcopy(item) for identity, item in tools.items() if identity not in used_tools)
    restored.extend(copy.deepcopy(item) for item in source if item.get('type') in {'subagents', 'pipeline_notification'})
    total = sum(float(item.get('phase_seconds') or 0) for item in source)
    first = next((item for item in restored if item.get('type') in {'reasoning', 'tool'}), None)
    if first is not None:
        first['phase_seconds'] = total
    result = copy.deepcopy(metadata)
    result['run_segments'] = fold_completed_segments(restored)
    # Сохраняем последнюю видимую реплику и её идентификатор, включая ручные правки.
    original_final = next((item for item in reversed(source) if item.get('type') == 'assistant_final'), None)
    rebuilt_final = next((item for item in reversed(result['run_segments']) if item.get('type') == 'assistant_final'), None)
    if original_final is not None and rebuilt_final is not None:
        rebuilt_final.update(text=original_final.get('text', ''), chat_id=original_final.get('chat_id'))
    result.setdefault('run_state', {})['projection_layout_version'] = 2
    return result


def model_segments_from_protocol(trace):
    """Text history before display folding, without historical tool payloads.

    Native tool exchanges remain in the run journal for recovery and in signed
    provider history. Ordinary local history keeps its previous text-only policy.
    """
    result = []
    for message in messages_from_dict(trace):
        if isinstance(message, ToolMessage) and message.additional_kwargs.get('subagent_result'):
            result.append({'type':'subagent_result','text':message.content})
            continue
        if not isinstance(message, AIMessage) or not isinstance(message.content, str):
            continue
        content = message.content
        after_thought = False
        for part in re.split(r'(<think>[\s\S]*?</think>)', content):
            if not part:
                continue
            if part.startswith('<think>') and part.endswith('</think>'):
                result.append({'type': 'reasoning', 'text': part[7:-8]})
                after_thought = True
            elif part.strip():
                if after_thought:
                    part = part.removeprefix('\n')  # Separator inserted by AgentCore.
                result.append({'type': 'assistant_interim', 'text': part})
                after_thought = False
    if result and result[-1]['type'] == 'assistant_interim':
        result[-1]['type'] = 'assistant_final'
    return result


class ChatRunJournal:
    """Token snapshots are coalesced; protocol boundaries and Stop are committed durably."""
    def __init__(self, context, input_messages, collector, previous=None):
        self.path = checkpoint_path(context.storage_dir, context.run_id)
        self.collector = collector
        self._written_at = 0.0
        self._history_dirty = True
        self.data = copy.deepcopy(previous) if previous else {
            'version': 1, 'run_id': context.run_id, 'session_id': context.session_id,
            'branch': context.branch, 'input_messages': [message_to_dict(m) for m in input_messages],
            'protocol_messages': [message_to_dict(m) for m in input_messages],
            'partial': {}, 'tools': {}, 'steps_count': 0, 'segments': [],
            'selected_pipeline': context.selected_pipeline,
            'model_profile_id': getattr(context, 'model_profile_id', ''),
            'reasoning_effort': getattr(context, 'reasoning_effort', ''),
            'full_web_pages': getattr(context, 'full_web_pages', False),
            'raw_request': getattr(context,'raw_request',False),
        }
        self.data.update(run_id=context.run_id, status='running', error='')
        self.save(force=True)
        from core.logger import core_logger
        self.context = context
        core_logger.event('storage', 'RUN_CHECKPOINT_OPEN', 'Run journal opened', context=context,
            details={'path':str(self.path), 'restored':bool(previous)})

    def save(self, force=False):
        if not force and time.monotonic() - self._written_at < 0.25:
            return
        self.data['segments'] = copy.deepcopy(self.collector.segments)
        self.data['updated_at'] = time.time()
        SQLiteStore(self.path).write_run(self.data, message_to_dict(self.visible_message()),
                                        write_history=self._history_dirty)
        self._history_dirty = False
        self._written_at = time.monotonic()

    def consume(self, event):
        kind = event.get('type')
        if kind=='run_finalizing':self.data['finalizing']=True
        if kind in {'user_update', 'steering_applied'}:
            self._history_dirty = True
        if kind in {'_model_start', '_model_complete', 'tool_result'}:
            self._history_dirty = True
        if kind == '_model_start':
            self.data['model_started'] = True
            self.data['model_inflight'] = True
            self.data['model_request'] = copy.deepcopy(event.get('model_request'))
            self.data['protocol_messages'] = copy.deepcopy(event['messages'])
            applied_ids = {item.get('data', {}).get('additional_kwargs', {}).get('chat_id') for item in event['messages']}
            self.data['pending_updates'] = [item for item in self.data.get('pending_updates', [])
                if item.get('data', {}).get('additional_kwargs', {}).get('chat_id') not in applied_ids]
            # Persist frozen per-request memory metadata on the original users.
            # Model/tool passes append messages after this same input prefix.
            if not self.data.get('raw_request'):
                self.data['input_messages'] = copy.deepcopy(event['messages'][:len(self.data['input_messages'])])
            self.data['steps_count'] = event.get('steps_count', 0)
            self.data['partial'] = copy.deepcopy(event.get('seed_partial') or {
                'content': '', 'reasoning': '', 'tool_calls': {}, 'provider_deltas': []})
        elif kind == '_model_delta':
            partial = self.data['partial']
            delta = event['delta']
            partial['content'] += delta.get('content') or ''
            partial['reasoning'] += delta.get('reasoning_content') or delta.get('reasoning') or ''
            if 'reasoning_content' in delta or 'reasoning' in delta:
                partial['parsed_reasoning'] = True
            # Keep provider-specific reasoning/signature fields without reducing
            # them to the human-readable transcript. Do not execute fragments.
            opaque = {k: v for k, v in delta.items() if k not in {'content', 'reasoning_content', 'reasoning', 'tool_calls'}}
            if opaque:
                if '_provider_data' in opaque:
                    partial['provider_data'] = opaque.pop('_provider_data')
                if opaque:
                    partial['provider_deltas'].append(opaque)
            for call in delta.get('tool_calls') or []:
                key = str(call.get('index', 0))
                saved = partial['tool_calls'].setdefault(key, {'id': '', 'name': '', 'arguments': ''})
                saved['id'] = call.get('id') or saved['id']
                function = call.get('function') or {}
                saved['name'] = function.get('name') or saved['name']
                saved['arguments'] += function.get('arguments') or ''
                extras = {name: copy.deepcopy(value) for name, value in call.items() if name not in {'index', 'id', 'function'}}
                if extras:
                    saved.setdefault('provider_data', {}).update(extras)
        elif kind == '_model_complete':
            self.data['model_inflight'] = False
            self.data['protocol_messages'] = copy.deepcopy(event['messages'])
            self.data['partial'] = {}
        elif kind == 'tool_start':
            self.data['tools'][event['id']] = {'name': event['name'], 'args': event.get('args', {}), 'status': 'running'}
        elif kind == 'tool_result':
            tool = self.data['tools'].setdefault(event['id'], {'name': event['name']})
            tool.update(status=event.get('status') or ('failed' if event.get('error') else 'completed'), output=str(event.get('output', '')))
            if event.get('document_images'):
                tool['document_images'] = copy.deepcopy(event['document_images'])
            protocol = messages_from_dict(self.data['protocol_messages'])
            declared = {call['id'] for m in protocol if isinstance(m, AIMessage) for call in m.tool_calls}
            resolved = {m.tool_call_id for m in protocol if isinstance(m, ToolMessage)}
            if event['id'] in declared and event['id'] not in resolved:
                self.data['protocol_messages'].append(message_to_dict(ToolMessage(
                    content=str(event.get('output', '')), tool_call_id=event['id'], name=event['name'],
                    additional_kwargs={
                        **({'subagent_result':True} if event.get('subagent_result') else {}),
                        **({'document_images':copy.deepcopy(event['document_images'])} if event.get('document_images') else {})})))
        self.collector.consume(event)
        self.save(force=kind in {'_model_start', '_model_complete', 'tool_start', 'tool_result', 'subagent', 'run_finalizing', 'user_update', 'steering_applied', 'pipeline_notification'} and not event.get('streaming'))

    def finish(self, status, error=''):
        self._history_dirty = True
        self.data.update(status=status, error=str(error))
        for tool in self.data['tools'].values():
            if tool.get('status') == 'running':
                tool['status'] = 'unknown'
        self.save(force=True)
        from core.logger import core_logger
        core_logger.event('storage', 'RUN_CHECKPOINT', f'Run checkpoint saved: {status}', context=self.context,
            details={'path':str(self.path), 'error':str(error)})

    def visible_message(self):
        return visible_run_message(self.data)


def visible_run_message(data):
    segments = copy.deepcopy(data.get('segments') or [])
    # Принятые правки сохраняются даже при остановке до ближайшего шага модели.
    visible_updates = {item.get('chat_id') for item in segments if item.get('type') == 'user_update'}
    for update in data.get('pending_updates') or []:
        identity = update.get('data', {}).get('additional_kwargs', {}).get('chat_id')
        if identity and identity not in visible_updates:
            segments.append({'type': 'user_update', 'message': copy.deepcopy(update), 'chat_id': identity,
                             'run_id': data['run_id'], 'applied': False})
            visible_updates.add(identity)
    if data.get('status') == 'completed':
        segments = fold_completed_segments(segments)
    if data.get('status') != 'running':
        for segment in segments:
            if segment.get('type') == 'subagents':
                for result in segment.get('results') or []:
                    if result.get('status') in {'queued','running'}:result['status'] = 'stopped'
    partial = data.get('partial') or {}
    # Tool JSON interrupted in transport is retained as data, never executable.
    for index, call in (partial.get('tool_calls') or {}).items():
        segments.append({'type': 'tool', 'id': call.get('id') or 'partial_' + index,
                         'name': call.get('name') or 'Незавершённый вызов',
                         'args': {'_partial_arguments': call.get('arguments', '')},
                         'output': 'Вызов получен не полностью и не выполнялся.', 'status': 'incomplete',
                         'chat_id': next((s.get('chat_id') for s in reversed(segments) if s.get('type') in {'reasoning', 'tool'}), None)})
    for segment in segments:
        if segment.get('type') == 'assistant_stream':
            segment['type'] = 'assistant_final'
        if segment.get('type') == 'tool':
            state = data.get('tools', {}).get(segment.get('id'), {})
            if state.get('status') in {'failed', 'stopped'}:
                segment['status'] = state['status']
            if state.get('status') == 'unknown':
                segment.update(status='unknown', output=segment.get('output') or 'Выполнение прервано. Результат действия неизвестен; автоматически не повторяется.')
    visible = [s for s in segments if s.get('type') in {'assistant_final', 'assistant_interim'}]
    latest = segments[-1] if segments else {}
    stage = 'answer' if latest.get('type') == 'assistant_final' else 'thought'
    target = latest.get('chat_id')
    text = str(visible[-1].get('text', '')) if visible else ''
    thoughts = '\n'.join(str(s.get('text', '')) for s in segments if s.get('type') == 'reasoning')
    content = ('<think>' + thoughts + '</think>\n' if thoughts else '') + text
    kwargs = {'run_segments': segments, 'run_state': {
        'run_id': data['run_id'], 'status': data.get('status', 'interrupted'),
        'stage': stage, 'target_chat_id': target, 'error': data.get('error', ''),
        'model_profile_id': data.get('model_profile_id', ''),
        'reasoning_effort': data.get('reasoning_effort', ''),
    }}
    # Keep the real per-pass conversation beside the folded display. It is
    # replayed only to the same endpoint/model/protocol; credentials are absent.
    protocol = data.get('protocol_messages') or []
    trace = protocol[1 if data.get('raw_request') else len(data.get('input_messages') or []):]
    if data.get('steering_ids'):
        kwargs['run_state']['steered_protocol'] = copy.deepcopy(trace)
        kwargs['run_state']['projection_final_text'] = text
        kwargs['run_state']['pending_updates'] = copy.deepcopy(data.get('pending_updates') or [])
    if data.get('raw_request'):kwargs['run_state']['raw_request']=True
    if data.get('finalizing'):kwargs['run_state']['finalizing']=True
    if data.get('status') == 'completed' and trace:
        kwargs['run_state']['model_segments'] = model_segments_from_protocol(trace)
        kwargs['run_state']['projection_thought_text'] = thoughts
    fingerprints = (item.get('data', {}).get('additional_kwargs', {}).get('provider_data', {}).get('fingerprint')
                    for item in reversed(trace) if item.get('type') == 'ai')
    fingerprint = next((value for value in fingerprints if value), None)
    if fingerprint:
        kwargs['run_state']['provider_history'] = copy.deepcopy(trace)
        kwargs['run_state']['provider_fingerprint'] = fingerprint
        kwargs['run_state']['projection_final_text'] = text
    if visible:
        kwargs['chat_id'] = visible[-1].get('chat_id')
    if not segments:
        # This is UI status, not an assistant answer passed to the model.
        content = ('Ожидание ответа модели…' if data.get('status') == 'running'
                   else 'Генерация прервана до получения ответа.')
        kwargs['run_state']['stage'] = 'answer'
    if data.get('status') == 'completed':
        kwargs = repair_steered_projection(kwargs, content)
    return AIMessage(content=content, additional_kwargs=kwargs)


def resume_protocol(data):
    """Resolve only interrupted *started* calls. Untouched calls may execute."""
    messages = messages_from_dict(data['protocol_messages'])
    resolved = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    for message in list(messages):
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call['id'] in resolved:
                continue
            state = data.get('tools', {}).get(call['id'], {})
            if state.get('status') in {'running', 'unknown'}:
                text = ('System: This action was interrupted after execution started. Its effect is unknown. '
                        'Do not automatically repeat it. Inspect the resulting state before deciding what to do.')
                delegated = call['name'] in {'delegate_to_subagent','delegate_to_subagents'}
                if delegated:
                    saved = [result for segment in data.get('segments') or [] if segment.get('type') == 'subagents'
                             for result in segment.get('results') or [] if result.get('group') == call['id']]
                    for result in saved:
                        text += '\n\nSubagent '+str(result.get('name') or result.get('agent_id') or '')+' · '+str(result.get('status') or '')
                        if result.get('output'):text += '\n'+str(result['output'])
                messages.append(ToolMessage(tool_call_id=call['id'], name=call['name'], content=text,
                    additional_kwargs={'subagent_result':True} if delegated else {}))
                resolved.add(call['id'])
    if (not data.get('partial') and data.get('segments')
            and data['segments'][-1].get('type') in {'assistant_stream', 'assistant_final'}
            and messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls):
        messages.pop()  # regenerate the interrupted answer, not its prefix
    return messages
