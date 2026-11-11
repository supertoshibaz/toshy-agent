"""Явная загрузка текстового вложения в память до отправки запроса."""
import asyncio

from core.attachments import AttachmentError, read_text_attachment, resolve_attachment_path
from core.context import ExecutionContext, bind_execution_context
from core.sqlite_storage import SQLiteStore


async def import_attachment(manager, memory, client, session_id, source_path, *, branch='main',
                            force_copy=False, to_memory=False, request_id='', context=None):
    branch = manager.clean_branch_name(branch)
    if not manager.has_branch(session_id, branch):
        raise AttachmentError('Ветка сессии не найдена.')
    metadata, active = await asyncio.to_thread(manager.load_session_metadata, session_id, branch=branch)
    if not manager.has_branch(session_id, active):
        raise AttachmentError('Ветка сессии не найдена.')
    store = manager.storage(session_id)
    cache_key = 'memory_attachment:' + active + ':' + str(request_id) if to_memory and request_id else ''
    cached_reference = None
    if cache_key:
        with store.connection() as db:
            cached = SQLiteStore._state(db, cache_key)
        if cached and store.read_document_header(cached['temp_memory']['document_id']):
            cached_reference = dict(cached['temp_memory'])
            try:
                resolve_attachment_path(cached, session_id)
            except AttachmentError:
                pass
            else:
                if cached.get('memory_only') and not cached.get('workspace_path'):
                    return cached
    attachment = await asyncio.to_thread(manager.import_attachment, session_id, source_path,
                                         force_copy=force_copy, memory_only=to_memory)
    if not to_memory:
        return attachment
    if attachment.get('media_type') != 'text':
        raise AttachmentError('Во временную память можно загрузить только текстовое вложение.')
    if cached_reference is not None:
        # Очистка после сетевой ошибки могла удалить копию файла, но не документ памяти.
        attachment['temp_memory'] = cached_reference
        with store.connection(write=True) as db:
            SQLiteStore._set_state(db, cache_key, attachment)
        return attachment
    path = resolve_attachment_path(attachment, session_id)
    content = await asyncio.to_thread(read_text_attachment, path)
    if context is None:
        effective = await asyncio.to_thread(manager.resolve_session_project_settings, session_id, metadata, branch=active)
        context = ExecutionContext.create(session_id=session_id, branch=active,
            workspace_root=effective['workspace_path'], temp_memory_mode=effective['memory_mode'],
            store_temp_memory_both_formats=effective['store_temp_memory_both'])
    with bind_execution_context(context):
        doc_id = await memory.save_temp_data(content, source='attachment: ' + attachment['filename'], client=client)
        header = await asyncio.to_thread(store.read_document_header, doc_id)
    attachment['temp_memory'] = {'document_id': doc_id, 'formats': (header or {}).get('formats') or [context.temp_memory_mode]}
    if cache_key:
        with store.connection(write=True) as db:
            SQLiteStore._set_state(db, cache_key, attachment)
    return attachment


def copy_memory_reference(attachment, source_store, target_store):
    reference = attachment.get('temp_memory')
    if not isinstance(reference, dict):
        return
    doc_id = reference.get('document_id')
    documents = source_store.read_documents(doc_id)
    if doc_id not in documents:
        raise AttachmentError('Документ вложения отсутствует во временной памяти: ' + str(doc_id))
    key = 'copied_attachment_memory:' + str(source_store.path) + ':' + str(doc_id)
    with target_store.connection() as db:
        copied = SQLiteStore._state(db, key)
    if not copied or not target_store.read_document_header(copied):
        copied = target_store.add_document(documents[doc_id])
        with target_store.connection(write=True) as db:
            SQLiteStore._set_state(db, key, copied)
    attachment['temp_memory'] = {**reference, 'document_id': copied}
