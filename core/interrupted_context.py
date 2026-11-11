"""Постоянная проекция рабочих заметок оборванного ответа в контекст модели."""
import re

from langchain_core.messages import AIMessage

RETAINED_NOTES = 'retained_interrupted_notes'


def restore_interrupted_notes(messages, store):
    """Возвращаем закреплённые заметки и для присланной GUI истории."""
    if store.exists():
        from core.sqlite_storage import SQLiteStore
        with store.connection() as db:
            for message in messages:
                if not isinstance(message, AIMessage):continue
                run_id = (message.additional_kwargs.get('run_state') or {}).get('run_id')
                if run_id:
                    saved = SQLiteStore._state(db, 'interrupted_notes:' + str(run_id))
                    if saved:message.additional_kwargs[RETAINED_NOTES] = saved
    freeze_interrupted_notes(messages)


def freeze_interrupted_notes(messages):
    """Фиксируем исключение один раз; следующие ответы его не отменяют."""
    last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if last is None or last.additional_kwargs.get(RETAINED_NOTES):
        return
    kwargs = last.additional_kwargs
    state = kwargs.get('run_state') or {}
    if state.get('status') not in {'stopped', 'interrupted', 'unavailable'}:
        return
    segments = kwargs.get('run_segments') or []
    content = str(last.content or '')
    visible = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
    notes = '\n'.join(str(s.get('text') or '') for s in segments if s.get('type') == 'reasoning').strip()
    if not notes:
        notes = '\n'.join(re.findall(r'<think>([\s\S]*?)</think>', content)).strip()
    completed = []
    for segment in segments:
        if segment.get('type') == 'tool' and segment.get('status') not in {'running','unknown','incomplete','queued'}:
            if segment.get('output') is not None:
                completed.append(str(segment.get('name') or 'tool') + ':\n' + str(segment['output']))
    if not notes and not completed:
        return
    kwargs[RETAINED_NOTES] = (
        'Saved working notes from an interrupted response. This work was not completed. '
        'Use these notes to continue; do not treat plans as completed actions.\n\n' + notes + ('\n\nPartial answer:\n' + visible if visible else '')
        + ('\n\nSaved tool results:\n' + '\n\n'.join(completed) if completed else '')
    )
