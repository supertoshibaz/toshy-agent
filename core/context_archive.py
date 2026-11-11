"""Additive compaction checkpoints; never rewrite the displayed transcript."""

import hashlib
import json
import time
import uuid

from langchain_core.messages import AIMessage
from core.chat_references import CHAT_ID, model_assistant_parts
from core.sqlite_storage import SQLiteStore

OWNER = uuid.uuid4().hex


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS context_compactions (
        id TEXT PRIMARY KEY, branch TEXT NOT NULL, scope TEXT NOT NULL,
        fingerprint TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL)""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS context_compactions_scope ON context_compactions(scope,fingerprint,created DESC)"
    )


def digest_messages(messages):
    """Ignore display-only state and LangGraph's transient UUIDs, retain edits."""
    digest = hashlib.sha256()
    result = []
    for message in messages:
        data = message.additional_kwargs or {}
        body = (
            model_assistant_parts(message)
            if isinstance(message, AIMessage)
            else message.content
        )
        semantic = [
            message.type,
            body,
            getattr(message, "tool_calls", None),
            getattr(message, "tool_call_id", None),
            {
                key: data[key]
                for key in (CHAT_ID, "attachments", "tags", "chat_reply")
                if key in data
            },
        ]
        digest.update(
            json.dumps(
                semantic, ensure_ascii=False, sort_keys=True, default=str
            ).encode()
        )
        result.append(digest.hexdigest())
    return result


def checkpoint(store, branch, scope, fingerprint, messages):
    if not store.exists():
        return None
    hashes = digest_messages(messages)
    with store.connection() as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_compactions'"
        ).fetchone():
            return None
        rows = db.execute(
            "SELECT payload FROM context_compactions WHERE scope=? AND fingerprint=? ORDER BY created DESC,rowid DESC",
            (scope, fingerprint),
        ).fetchall()
    # A fork can inherit a checkpoint only if its entire covered prefix still
    # matches. An edit, deletion or regeneration falls back to an older match.
    for row in rows:
        entry = json.loads(row[0])
        cut = entry["cut"]
        if 0 < cut <= len(hashes) and hashes[cut - 1] == entry["prefix_hash"]:
            return entry
    return None


def save_checkpoint(store, branch, scope, fingerprint, entry):
    entry = dict(entry, id=uuid.uuid4().hex, created=time.time())
    with store.connection(write=True, create=False) as db:
        schema(db)
        # No network work occurs under this transaction. Previous versions
        # remain available for edit/branch rollback.
        db.execute(
            "INSERT INTO context_compactions VALUES (?,?,?,?,?,?)",
            (
                entry["id"],
                branch,
                scope,
                fingerprint,
                entry["created"],
                json.dumps(entry, ensure_ascii=False),
            ),
        )
        from core.memory.page_delivery import reset_receipts
        reset_receipts(db, branch, scope)
    return entry


def set_status(store, branch, scope, **status):
    with store.connection(write=True, create=False) as db:
        SQLiteStore._set_state(
            db,
            "compression:" + branch + ":" + scope,
            dict(status, owner=OWNER, updated=time.time()),
        )


def get_status(store, branch="main", scope=""):
    try:
        with store.connection() as db:
            status = SQLiteStore._state(db, "compression:" + branch + ":" + scope, {})
        if status.get("owner") != OWNER and status.get("status") in {
            "running",
            "queued",
        }:
            status = dict(
                status,
                status="interrupted",
                message="Сжатие было прервано вместе с ядром.",
            )
        return status
    except FileNotFoundError:
        return {}


def archive_rows(store, context):
    """Read only this branch and subagent scope, including complete tool output."""
    with store.connection() as db:
        if not context.subagent_id:
            for position, raw in db.execute(
                "SELECT position,payload FROM messages WHERE branch=? ORDER BY position",
                (context.branch,),
            ):
                yield "message:" + str(position), json.loads(raw)
        # Old abandoned/regenerated runs must not leak future answers into a
        # rewound conversation. Only runs still referenced by this branch count.
        query = """SELECT r.run_id,r.position,r.payload FROM run_messages r JOIN runs n ON n.id=r.run_id
            WHERE n.branch=? AND r.kind='protocol_messages' AND COALESCE(json_extract(n.payload,'$.subagent_id'),'')=?
            AND (r.run_id=? OR r.run_id IN (SELECT run_id FROM messages WHERE branch=? AND run_id IS NOT NULL))
            ORDER BY n.updated_at,r.position"""
        if context.subagent_id:
            query = query.replace(
                "AND (r.run_id=? OR r.run_id IN (SELECT run_id FROM messages WHERE branch=? AND run_id IS NOT NULL))",
                "AND (r.run_id=? OR json_extract(n.payload,'$.parent_run_id')=? OR json_extract(n.payload,'$.parent_run_id') IN (SELECT run_id FROM messages WHERE branch=? AND run_id IS NOT NULL))",
            )
            args = (
                context.branch,
                context.subagent_id,
                context.run_id,
                context.parent_run_id,
                context.branch,
            )
        else:
            args = (context.branch, "", context.run_id, context.branch)
        for run, position, raw in db.execute(query, args):
            yield f"run:{run}:{position}", json.loads(raw)


def archive_text(item):
    from langchain_core.messages import messages_from_dict

    message = messages_from_dict([item])[0]
    if isinstance(message, AIMessage):
        text = "\n\n".join(str(body) for _, body in model_assistant_parts(message))
        if message.tool_calls:
            text += "\n" + json.dumps(message.tool_calls, ensure_ascii=False)
    else:
        text = str(message.content)
    return message.type, text, message.additional_kwargs.get(CHAT_ID)


def search_archive(store, context, query, limit=6):
    terms = str(query).casefold().split()
    if not terms:
        raise ValueError("Укажите слова для поиска.")
    found = []
    for source, item in archive_rows(store, context):
        role, text, identity = archive_text(item)
        folded = text.casefold()
        hits = sum(term in folded for term in terms)
        if hits:
            start = max(
                0, min(folded.find(term) for term in terms if term in folded) - 150
            )
            found.append(
                (
                    hits,
                    dict(
                        source=source,
                        role=role,
                        message_id=identity,
                        excerpt=text[start : start + 1200],
                        offset=start,
                        characters=len(text),
                    ),
                )
            )
    found.sort(key=lambda value: value[0], reverse=True)
    return [value for _, value in found[: max(1, min(int(limit), 12))]]


def read_archive(store, context, source, offset=0, length=16000):
    for identity, item in archive_rows(store, context):
        if identity == source:
            role, text, chat_id = archive_text(item)
            offset = max(0, int(offset))
            length = max(1, min(int(length), 32000))
            return dict(
                source=source,
                role=role,
                message_id=chat_id,
                text=text[offset : offset + length],
                offset=offset,
                characters=len(text),
                has_more=offset + length < len(text),
            )
    raise ValueError("Сообщение не найдено в текущей ветке.")
