"""Move a UI selection without inheriting unselected conversation history."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import shutil
import uuid

from langchain_core.messages import message_to_dict, messages_from_dict
from core.attachments import _safe_filename


class StaleConversation(ValueError):
    pass


def forward_messages(manager, source_session, *, branch, snapshot, selected,
                     destination_session, destination_branch='main'):
    """Append display copies to another chat; never mutate the source history."""
    from core.chat_references import CHAT_ID, PINNED, REPLY, REQUEST_MEMORY, new_chat_id
    branch = manager.clean_branch_name(branch)
    destination_branch = manager.clean_branch_name(destination_branch)
    if manager.storage(source_session).path == manager.storage(destination_session).path:
        raise ValueError('Выберите другой чат')
    if not manager.has_branch(source_session, branch) or not manager.has_branch(destination_session, destination_branch):
        raise FileNotFoundError('Исходная или целевая ветка отсутствует')
    messages, _, _ = manager.load_session(source_session, branch=branch)
    if _comparison([message_to_dict(message) for message in messages]) != _comparison(snapshot):
        raise StaleConversation('История изменилась. Повторите пересылку.')
    copied = _restore_hidden(deepcopy(_normalize(selected)), [message_to_dict(message) for message in messages])
    if not copied or any(item['type'] not in {'human', 'ai'} for item in copied):
        raise ValueError('Выберите пользовательское сообщение, ответ или размышления')
    target = manager.storage(destination_session)
    original = target.read_branch(destination_branch)
    if original is None:
        raise FileNotFoundError('Целевая ветка отсутствует')
    payload = deepcopy(original)
    source_root = Path(manager.get_session_dir(source_session, create=False)).resolve()
    target_root = Path(manager.get_session_dir(destination_session, create=False)).resolve()
    directories = []
    identities = {}
    def copied_id(value):
        if not value:
            return new_chat_id()
        if value not in identities:
            identities[value] = new_chat_id()
        return identities[value]
    try:
        for item in copied:
            data = item['data']
            kwargs = data.setdefault('additional_kwargs', {})
            kwargs[CHAT_ID] = copied_id(kwargs.get(CHAT_ID))
            kwargs[PINNED] = False
            for key in ('run_state', REQUEST_MEMORY, 'provider_data', 'provider_deltas'):
                kwargs.pop(key, None)
            data['id'] = None
            if item['type'] == 'ai':
                # A forwarded tool display must not become a live tool exchange
                # or an interrupted generation belonging to the receiving chat.
                data['tool_calls'] = []
                data['invalid_tool_calls'] = []
            for segment in kwargs.get('run_segments') or []:
                segment[CHAT_ID] = copied_id(segment.get(CHAT_ID))
            if isinstance(kwargs.get(REPLY), dict):
                kwargs[REPLY]['source_session'] = source_session
                kwargs[REPLY]['target'] = None
            for number, attachment in enumerate(kwargs.get('attachments') or [], 1):
                path = Path(str(attachment.get('path') or ''))
                if not path.is_absolute():
                    path = (source_root / path).resolve()
                    if not path.is_relative_to(source_root):
                        raise ValueError('Некорректный путь вложения')
                if not path.is_file():
                    raise FileNotFoundError(f'Вложение отсутствует: {attachment.get("filename")}')
                identity = 'att_' + uuid.uuid4().hex
                relative = Path('attachments') / identity / _safe_filename(attachment.get('filename') or path.name)
                directory = target_root / relative.parent
                directory.mkdir(parents=True, exist_ok=False)
                directories.append(directory)
                shutil.copy2(path, target_root / relative)
                attachment.update(id=identity, path=relative.as_posix(), storage='copy', position=number)
                from core.attachment_memory import copy_memory_reference
                copy_memory_reference(attachment, manager.storage(source_session), target)
        payload['messages'] = (original.get('messages') or []) + copied
        with target.connection(write=True) as db:
            if target._read_branch(db, destination_branch) != original:
                raise StaleConversation('Целевой чат изменился. Повторите пересылку.')
            target._write_branch(db, destination_branch, payload)
    except Exception:
        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return {'success': True, 'session_id': destination_session, 'branch': destination_branch,
            'message_count': len(copied)}


def _normalize(messages):
    if not isinstance(messages, list):
        raise ValueError('Сообщения должны быть списком')
    return [message_to_dict(m) for m in messages_from_dict(messages)]



def _comparison(messages):
    # GUI не хранит служебные дополнения ядра. Они не меняют выбранный текст.
    from core.chat_references import REQUEST_MEMORY
    from core.interrupted_context import RETAINED_NOTES
    result = deepcopy(_normalize(messages))
    for item in result:
        data = item['data']
        kwargs = data.get('additional_kwargs', {})
        for key in (REQUEST_MEMORY, 'module_context', 'requested_resources', RETAINED_NOTES):
            kwargs.pop(key, None)
    return result


def _restore_hidden(messages, originals):
    from core.chat_references import REQUEST_MEMORY
    from core.interrupted_context import RETAINED_NOTES
    by_id = {item['data'].get('additional_kwargs', {}).get('chat_id'): item['data'] for item in originals}
    by_run = {item['data'].get('additional_kwargs', {}).get('run_state', {}).get('run_id'):
              item['data'].get('additional_kwargs', {}).get(RETAINED_NOTES) for item in originals}
    for item in messages:
        data = item['data']
        kwargs = data.setdefault('additional_kwargs', {})
        original = by_id.get(kwargs.get('chat_id'))
        if item['type'] == 'human' and original and original.get('content') == data.get('content'):
            for key in (REQUEST_MEMORY, 'module_context', 'requested_resources'):
                if key not in kwargs and key in original.get('additional_kwargs', {}):
                    kwargs[key] = deepcopy(original['additional_kwargs'][key])
        notes = by_run.get(kwargs.get('run_state', {}).get('run_id'))
        if item['type'] == 'ai' and notes:
            kwargs.setdefault(RETAINED_NOTES, deepcopy(notes))
    return messages

def move_selection(manager, source_session, *, branch, snapshot, selected, remaining,
                   destination_session, destination_branch='main', new_branch=False):
    branch = manager.clean_branch_name(branch)
    destination_branch = manager.clean_branch_name(destination_branch)
    source_store = manager.storage(source_session)
    target_store = manager.storage(destination_session)
    if source_store.path == target_store.path and branch == destination_branch:
        raise ValueError('Выберите другую сессию или новую ветку')
    if not manager.has_branch(source_session, branch):
        raise FileNotFoundError('Исходная ветка отсутствует')
    if not manager.has_session(destination_session):
        raise FileNotFoundError('Целевая сессия отсутствует')
    original_messages, source_meta, _ = manager.load_session(source_session, branch=branch)
    if _comparison([message_to_dict(m) for m in original_messages]) != _comparison(snapshot):
        raise StaleConversation('История изменилась. Повторите выделение после обновления чата.')
    originals = [message_to_dict(m) for m in original_messages]
    selected = _restore_hidden(_normalize(selected), originals)
    remaining = _restore_hidden(_normalize(remaining), originals)
    if not selected:
        raise ValueError('Не выбраны сообщения')
    old_target = target_store.read_branch(destination_branch)
    if new_branch:
        if old_target is not None or destination_session != source_session or destination_branch == 'main':
            raise ValueError('Ветка уже существует или имеет недопустимое название')
        target_meta = deepcopy(source_meta)
        target_meta.pop('branch_project_settings', None)
        target_meta['active_branch'] = destination_branch
        target_payload = {'branch_name': destination_branch, 'metadata': target_meta,
                          'parent_branch': branch, 'fork_message_index': 0, 'created_at': datetime.now().isoformat()}
        target_messages = []
    else:
        if old_target is None:
            raise FileNotFoundError('Целевая ветка отсутствует')
        messages, target_meta, _ = manager.load_session(destination_session, branch=destination_branch)
        target_messages = [message_to_dict(m) for m in messages]
        target_payload = deepcopy(old_target) if isinstance(old_target, dict) else {'metadata': target_meta}
        target_payload['branch_name'] = destination_branch

    copied_dirs = []
    target_written = False
    source_original = source_store.read_branch(branch)
    try:
        if source_session != destination_session:
            source_root = Path(manager.get_session_dir(source_session, create=False))
            target_root = Path(manager.get_session_dir(destination_session, create=False))
            for message in selected:
                attachments = message['data'].get('additional_kwargs', {}).get('attachments', [])
                for number, attachment in enumerate(attachments, 1):
                    path = Path(str(attachment.get('path') or ''))
                    if not path.is_absolute():
                        path = (source_root / path).resolve()
                        if not path.is_relative_to(source_root.resolve()):
                            raise ValueError('Некорректный путь вложения')
                    if not path.is_file():
                        raise FileNotFoundError(f'Вложение отсутствует: {attachment.get("filename")}')
                    identity = 'att_' + uuid.uuid4().hex
                    relative = Path('attachments') / identity / _safe_filename(attachment.get('filename') or path.name)
                    directory = target_root / relative.parent
                    directory.mkdir(parents=True, exist_ok=False)
                    copied_dirs.append(directory)
                    shutil.copy2(path, target_root / relative)
                    attachment.update(id=identity, path=relative.as_posix(), storage='copy', position=number)
                    from core.attachment_memory import copy_memory_reference
                    copy_memory_reference(attachment, source_store, target_store)
        # Destination first: an interrupted move can leave a duplicate, never
        # lose the only copy of a message or its attached file.
        target_payload['messages'] = target_messages + selected
        source_payload = deepcopy(source_original)
        source_payload['messages'] = remaining
        if source_store.path == target_store.path:
            # Both branches live in one session: one atomic transaction.
            with source_store.connection(write=True) as db:
                if source_store._read_branch(db, branch) != source_original:
                    raise StaleConversation('История изменилась. Повторите выделение.')
                source_store._write_branch(db, destination_branch, target_payload)
                source_store._write_branch(db, branch, source_payload)
        else:
            # Independent databases: destination first. A hard process crash
            # can leave duplicates, but cannot discard the only message copy.
            target_store.write_branch(destination_branch, target_payload)
            target_written = True
            with source_store.connection(write=True) as db:
                if source_store._read_branch(db, branch) != source_original:
                    raise StaleConversation('История изменилась. Повторите выделение.')
                source_store._write_branch(db, branch, source_payload)
    except Exception:
        if target_written:
            # If an error happened after the source commit, restore it before
            # removing the destination copy. A failed rollback keeps that copy.
            if source_store.read_branch(branch) == source_payload:
                source_store.write_branch(branch, source_original)
            if old_target is None:
                target_store.delete_branch(destination_branch)
            else:
                target_store.write_branch(destination_branch, old_target)
        for directory in copied_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    # Cleanup is optional housekeeping after both histories are committed.
    try:
        # Do not collect journals inside the two-database transfer: rollback
        # must still be able to restore the original source conversation.
        identities = {item.get('data', {}).get('additional_kwargs', {}).get('run_state', {}).get('run_id')
                      for item in source_original.get('messages') or []}
        source_store.prune_unreferenced_runs(candidates=identities - {None})
        manager.cleanup_orphan_attachments(source_session)
    except (OSError, ValueError):
        pass
    return {'success': True, 'session_id': destination_session, 'branch': destination_branch}
