"""Branch-scoped page receipts, atomic with successful context compaction."""
import asyncio
import hashlib
import json
import sqlite3
import uuid

from core.background import run_data_task
from core.context import get_execution_context
from core.sqlite_storage import SQLiteStore


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS memory_page_receipts (
        branch TEXT NOT NULL, scope TEXT NOT NULL, page_key TEXT NOT NULL, receipt TEXT NOT NULL,
        PRIMARY KEY(branch, scope, page_key))''')


def reset_receipts(db, branch, scope=''):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_page_receipts'").fetchone():
        db.execute('DELETE FROM memory_page_receipts WHERE branch=? AND scope=?', (branch, scope))


def _claim(context, matches, source, limit, receipt):
    store = SQLiteStore(context.storage_dir / 'session.sqlite')
    selected = []
    with store.connection(write=True) as db:
        schema(db)
        for match in matches:
            identity = [source, match.get('database_id'), match['doc_id'], match['page_num'], match['text']]
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()
            inserted = db.execute('INSERT OR IGNORE INTO memory_page_receipts VALUES (?,?,?,?)',
                                  (context.branch, context.subagent_id, digest, receipt)).rowcount
            if not inserted:
                continue
            selected.append(match)
            if limit is not None and len(selected) >= limit:
                break
    return selected


def _release(context, receipt):
    store = SQLiteStore(context.storage_dir / 'session.sqlite')
    # Cancellation must never recreate a deleted/renamed session database.
    try:
        with store.connection(write=True, create=False) as db:
            db.execute('DELETE FROM memory_page_receipts WHERE branch=? AND scope=? AND receipt=?',
                       (context.branch, context.subagent_id, receipt))
    except (FileNotFoundError, sqlite3.OperationalError):
        pass


async def new_pages(matches, source, limit):
    if not matches:
        return []
    context, receipt = get_execution_context(), uuid.uuid4().hex
    task = asyncio.create_task(run_data_task(_claim, context, matches, source, limit, receipt))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The worker can finish after the awaiting call was cancelled. Release
        # only this call's receipts after it finishes; other searches stay seen.
        async def release():
            try:
                await task
            finally:
                await run_data_task(_release, context, receipt)
        cleanup = asyncio.create_task(release())
        _cleanup.add(cleanup)
        cleanup.add_done_callback(_cleanup.discard)
        cleanup.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        raise


_cleanup = set()
