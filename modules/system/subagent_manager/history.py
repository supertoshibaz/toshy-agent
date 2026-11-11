"""Subagent conversation projections live in their parent session's run journal."""

import json
from langchain_core.messages import HumanMessage, AIMessage, messages_from_dict
from core.sqlite_storage import SQLiteStore
from core.system_prompt import build_core_system_prompt


def subagent_system_prompt(config, context):
    if not config.get("use_system_prompt", True):
        return ""
    parts = [build_core_system_prompt(str(context.workspace_root))]
    if context.project_system_prompt:
        parts.append(context.project_system_prompt)
    if context.knowledge_base_snapshot_text:
        parts.append(
            "Knowledge bases at session start:\n" + context.knowledge_base_snapshot_text
        )
    parts.append(str(config.get("system_prompt") or "Complete the assigned task."))
    parts.append(
        "Use only the supplied tools. Finish the task with a substantive answer."
    )
    return "\n\n".join(parts)


def subagent_connection(config, parent, *, credentials=True):
    from core.model_connections import ModelConnection, snapshot, load_settings

    identity = config.get("model_profile_id")
    if identity:
        if credentials:
            return snapshot(connection_id=identity)
        record = next(
            (v for v in load_settings()["connections"] if v["id"] == identity), None
        )
        if record is None:
            raise ValueError("Профиль субагента недоступен")
        mode = record.get("mode", "api")
        return ModelConnection(
            mode,
            str(record.get("base_url") or "").rstrip("/"),
            str(record.get("model") or ""),
            (
                record.get("protocol", "chat_completions")
                if mode == "api"
                else "chat_completions"
            ),
            dict(record),
        )
    legacy = config.get("llm") or {}
    if legacy.get("api_url"):
        return ModelConnection(
            "local",
            str(legacy["api_url"]).rstrip("/"),
            str(legacy.get("model_name") or (parent.model if parent else "")),
        )
    if parent is None:
        raise ValueError("Модель основного чата не выбрана")
    return parent


def retained_messages(
    store, branch, identity, fingerprint, *, exclude_run="", parent_runs=None
):
    """Completed siblings append their own interaction, never a whole copied prefix."""
    try:
        with store.connection() as db:
            rows = db.execute(
                "SELECT id,payload FROM runs WHERE branch=? AND json_extract(payload,'$.subagent_id')=? ORDER BY json_extract(payload,'$.started_at'),id",
                (branch, identity),
            ).fetchall()
    except FileNotFoundError:
        return []
    result = []
    for run_id, raw in rows:
        if run_id == exclude_run:
            continue
        header = json.loads(raw)
        if parent_runs is not None and header.get("parent_run_id") not in parent_runs:
            continue
        if (
            header.get("status") not in {"completed", "error", "stopped"}
            or header.get("subagent_context_version") != 1
            or not header.get("model_fingerprint")
        ):
            continue
        if header.get("model_fingerprint") != fingerprint:
            result = []
            continue
        try:
            run = store.read_run(run_id)
        except FileNotFoundError:
            continue
        protocol = messages_from_dict(run.get("protocol_messages") or [])
        start = int(header.get("interaction_start", len(protocol)))
        if header.get("status") == "completed":
            result.extend(protocol[start:])
        else:
            interaction = protocol[start:]
            task = next((m for m in interaction if isinstance(m, HumanMessage)), None)
            if task is None:
                continue
            result.append(task)
            record = header.get("result") or {}
            known = [str(m.content) for m in interaction if m.type == "tool"]
            text = (
                "Previous task "
                + str(header.get("status"))
                + ". Do not automatically repeat its actions."
            )
            if known:
                text += "\nKnown tool results:\n" + "\n".join(known)
            if record.get("output"):
                text += "\nPartial answer:\n" + str(record["output"])
            if record.get("error"):
                text += "\n" + str(record["error"])
            result.append(AIMessage(content=text))
    return result


def request_context(store, run_id):
    empty = {"input": None, "output": None, "total": None, "limit": None}
    try:
        with store.connection() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_usage)")}
            if not {"run_id", "context_limit"} <= columns:
                return empty
            purpose = " AND purpose='model'" if 'purpose' in columns else ''
            row = db.execute(
                "SELECT context_input,context_output,context_tokens,context_limit,started FROM model_usage WHERE run_id=?"+purpose+" ORDER BY started DESC,rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if not row:
                return empty
            result = dict(zip(empty, row[:4]))
            run = db.execute('SELECT branch,json_extract(payload,\'$.subagent_id\') FROM runs WHERE id=?',(run_id,)).fetchone()
            if run:
                compressed = SQLiteStore._state(db,'compression:'+run[0]+':'+str(run[1] or ''),{})
                if compressed.get('status') == 'completed' and row[4]<compressed.get('updated',0):
                    result.update(input=None,output=None,total=None)
            return result
    except FileNotFoundError:
        return empty


def context_statistics(store, branch, configs, parent_connection):
    result = {
        identity: {"total": None, "limit": None, "status": ""} for identity in configs
    }
    fingerprints = {}
    for identity, config in configs.items():
        try:
            fingerprints[identity] = subagent_connection(
                config, parent_connection, credentials=False
            ).fingerprint
        except ValueError:
            pass
    try:
        with store.connection() as db:
            rows = db.execute(
                "SELECT json_extract(payload,'$.subagent_id'),json_extract(payload,'$.model_fingerprint'),status,json_extract(payload,'$.context') FROM runs WHERE branch=? AND json_extract(payload,'$.subagent_context_version')=1 ORDER BY json_extract(payload,'$.started_at'),id",
                (branch,),
            ).fetchall()
    except FileNotFoundError:
        return result
    for identity, fp, status, raw in rows:
        if identity not in fingerprints or not fp:
            continue
        if fp != fingerprints[identity]:
            result[identity] = {"total": None, "limit": None, "status": status}
            continue
        stats = json.loads(raw) if raw else {}
        result[identity] = {
            "total": stats.get("total"),
            "limit": stats.get("limit"),
            "status": status,
        }
    return result
