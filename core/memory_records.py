"""Human-managed memory records, without models, vectors or prompt rewriting."""
import json
from core.context import sanitize_session_id
from core.paths import APP_PATHS
from core.sqlite_storage import SQLiteStore


def _store(kind, session_id=None):
    if kind == 'permanent':
        return SQLiteStore(APP_PATHS.root / 'core/permanent_memory.sqlite', 'permanent')
    if kind != 'temporary':
        raise ValueError('Unknown memory kind.')
    if not session_id:
        raise ValueError('Select a session first.')
    return SQLiteStore(APP_PATHS.sessions / sanitize_session_id(session_id) / 'session.sqlite')


def list_records(kind, session_id=None):
    store = _store(kind, session_id)
    records = []
    if store.exists():
        with store.connection() as db:
            if kind == 'permanent':
                records = [json.loads(row[0]) for row in db.execute(
                    "SELECT json_remove(payload,'$.vector') FROM facts ORDER BY position")]
            else:
                for identity, raw in db.execute('SELECT id,payload FROM documents ORDER BY position'):
                    header = json.loads(raw)
                    text = []
                    # Prefer the text representation, never duplicate both formats.
                    for table in ('pages', 'chunks'):
                        text = [str(json.loads(row[0]).get('text', '')) for row in db.execute(
                            f"SELECT json_remove(payload,'$.vector') FROM {table} WHERE document_id=? ORDER BY position", (identity,))]
                        if text:
                            break
                    records.append({'id': identity, 'source': str(header.get('source') or ''),
                                    'timestamp': str(header.get('timestamp') or ''), 'text': '\n\n'.join(text)})
    return {'records': records, 'kind': kind, 'session_id': session_id}


def delete_record(kind, identity, session_id=None):
    store = _store(kind, session_id)
    if not store.exists():
        return {'deleted': False}
    # MemoryManager reads these tables for every operation; there is no stale
    # permanent/temp document cache to invalidate. All branches share temp memory.
    deleted = store.delete_fact(identity) if kind == 'permanent' else store.delete_document(identity)
    return {'deleted': deleted}
