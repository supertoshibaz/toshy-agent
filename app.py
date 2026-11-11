import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import replace
from collections import defaultdict
from contextlib import asynccontextmanager, AsyncExitStack, aclosing
from typing import Any, Dict, List, Optional, Tuple

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, message_to_dict, messages_from_dict

from core.context import ExecutionContext
from core.attachments import AttachmentError
from core.graph import AgentCore
from core.llm_client import LlmClient
from core.logger import core_logger
from core.memory import MemoryManager
from core.paths import APP_PATHS
from core.registry import ModuleRegistry
from core.security import load_or_create_core_token, token_matches
from core.session_manager import SessionManager
from core.system_prompt import render_system_prompt_preview_markdown


APP_PATHS.ensure_runtime_dirs()
from core.backups import register_process
register_process()
# Совместимость с пользовательскими модулями старого формата, использующими относительные пути.
os.chdir(APP_PATHS.root)
from core.log_events import UiPollAccessFilter
logging.getLogger("uvicorn.access").addFilter(UiPollAccessFilter())

with open(APP_PATHS.config, "r", encoding="utf-8") as config_file:
    config = yaml.safe_load(config_file) or {}

llm_cfg = config.get("llm", {})
embedding_cfg = config.get("embedding", {})
llm_url = llm_cfg.get("api_url", "http://localhost:8080/v1")
embed_url = embedding_cfg.get("api_url", "http://localhost:8081/v1")
model_name = llm_cfg.get("model_name", "local-agent-core")
embedding_model_name = embedding_cfg.get("model_name", "embedding-model")
default_workspace = str((APP_PATHS.root / config.get("workspace_root", "./workspace")).resolve())

client = LlmClient(
    llm_url=llm_url,
    embed_url=embed_url,
    model_name=model_name,
    embedding_model_name=embedding_model_name,
)
memory = MemoryManager(permanent_path=str(APP_PATHS.root / "core" / "permanent_memory.sqlite"), client=client)
registry = ModuleRegistry(config_path=str(APP_PATHS.config), memory=memory)
def _default_agent():
    return AgentCore(client=client, memory=memory, registry=registry)


def _validate_agent(instance):
    if not isinstance(instance, AgentCore):
        raise TypeError('core.agent replacement must inherit AgentCore.')
    for field in ('client', 'memory', 'registry', 'graph', 'pipeline_service'):
        if not hasattr(instance, field):
            raise TypeError(f'core.agent replacement does not provide {field}.')


agent_core = registry.plugins.build('core.agent', _default_agent, host=registry,
    validate=_validate_agent, data={'client': client, 'memory': memory, 'registry': registry})
session_manager = SessionManager(sessions_dir=str(APP_PATHS.sessions))
core_token = load_or_create_core_token()
from core.toshy_mcp import CoreToshyMcp
from core.toshy_mcp.api import install as install_toshy_mcp
toshy_mcp = CoreToshyMcp(agent_core, session_manager)
registry.core_components = [toshy_mcp]

_session_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_sessions_catalog_lock = asyncio.Lock()
_active_chat_runs: Dict[str, asyncio.Task] = {}
_active_chat_runs_lock = asyncio.Lock()
_recovered_pipeline_tasks = set()


def _session_lock(session_id: Optional[str]) -> asyncio.Lock:
    return _session_locks[session_id or "_no_session"]


@asynccontextmanager
async def _chat_guard(session_id: Optional[str]):
    """Сериализует только запросы одной сессии, не блокируя независимые чаты."""
    if session_id:
        async with _session_lock(session_id):
            yield
    else:
        yield


@asynccontextmanager
async def lifespan(_: FastAPI):
    await registry.async_init_all()
    core_logger.info("APP_INIT", "Все модули успешно инициализированы.")
    async def history_monitor():
        from core.file_history import get_history, _finish_thread
        while True:
            await asyncio.sleep(30)
            try:
                await _finish_thread(get_history().poll_external)
            except Exception as error:
                core_logger.error('FILE_HISTORY_POLL', str(error))
    async with toshy_mcp_endpoint.lifespan():
        monitor = asyncio.create_task(history_monitor())
        try:
            yield
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
    from core.session_titles import shutdown as stop_titles
    await stop_titles()
    await registry.shutdown_all()


app = FastAPI(title="Modular AI Agent Core", lifespan=lifespan)
toshy_mcp_endpoint = install_toshy_mcp(app, toshy_mcp)
from core.file_history_api import install as install_file_history
install_file_history(app, session_manager, default_workspace, registry, lambda: bool(_active_chat_runs))


@app.middleware("http")
async def authenticate_local_client(request: Request, call_next):
    auth = request.headers.get("Authorization", "")
    candidate = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if request.url.path == '/mcp' or request.url.path.startswith('/mcp/'):
        return await call_next(request)  # Endpoint checks its own separate credential.
    if any(module.allows_public_request(request) for module in list(registry.loaded_modules.values())):
        return await call_next(request)
    if not token_matches(candidate, core_token):
        return JSONResponse({"detail": "Unauthorized local client"}, status_code=401)
    return await call_next(request)


from core.backup_gate import BackupWriteBarrier
app.add_middleware(BackupWriteBarrier, state=app.state)


@app.api_route('/v1/modules/{module_name}/service', methods=['GET', 'POST', 'PATCH', 'DELETE'])
@app.api_route('/v1/modules/{module_name}/service/{path:path}', methods=['GET', 'POST', 'PATCH', 'DELETE'])
async def module_service(module_name: str, request: Request, path: str = ''):
    module = registry.loaded_modules.get(module_name)
    handler = getattr(module, 'handle_request', None)
    if handler is None:
        raise HTTPException(status_code=404, detail='Сервис модуля недоступен.')
    return await handler(request, path, session_manager=session_manager)


@app.post("/v1/shutdown")
async def shutdown_server():
    async def delayed_shutdown():
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(delayed_shutdown())
    return {"success": True, "message": "Сервер останавливается..."}


@app.post("/v1/chat/runs/{run_id}/cancel")
async def cancel_chat_run(run_id: str, request: Request):
    """Cancel the server-side task that owns one streaming agent run.

    Disconnecting a GUI HTTP reader is not enough for every provider: the
    graph can keep consuming a remote/local LLM stream.  Stop is therefore an
    explicit control operation keyed by the run id supplied by the GUI.
    """
    run_key = str(run_id or "").strip()
    if not run_key:
        raise HTTPException(status_code=400, detail="run_id is required")

    async with _active_chat_runs_lock:
        task = _active_chat_runs.get(run_key)

    active = bool(task and not task.done())
    try:
        cancel_options = await request.json()
    except Exception:
        cancel_options = {}
    keep_pause = bool(cancel_options.get("preserve_pause"))
    found_pause = False
    for pending in agent_core.pipeline_service.broker.pending():
        if pending.get("run_id") == run_key:
            found_pause = True
            if not keep_pause:
                agent_core.pipeline_service.broker.abandon(pending["request_id"])
    if run_key.isalnum() and not (keep_pause and found_pause):
        (agent_core.pipeline_service.root / f'{run_key}_request.json').unlink(missing_ok=True)
    if active:
        task.cancel()
        core_logger.info("CHAT_CANCEL", f"Отмена запуска {run_key}")
    from core.child_runs import cancel_child_runs
    children = await cancel_child_runs(run_key, whole_chat=True)
    return {"success": True, "active": active, "cancelled": active or bool(children)}


@app.post('/v1/chat/runs/{run_id}/steer')
async def steer_chat_run(run_id: str, request: Request):
    from core.steering import inbox_for
    from core.chat_references import ensure_history_ids
    from core.request_clock import stamp_latest_user
    inbox = inbox_for(run_id)
    if inbox is None or not inbox.accepting:
        raise HTTPException(status_code=409, detail='Запуск уже завершён или ещё не готов принять уточнение.')
    body = await request.json()
    if (body.get('session_id'), body.get('branch', 'main')) != (inbox.context.session_id, inbox.context.branch):
        raise HTTPException(status_code=409, detail='Уточнение относится к другой сессии или ветке.')
    raw = body.get('message') or {}
    text = raw.get('content')
    if not isinstance(text, str) or len(text) > 2000000:
        raise HTTPException(status_code=400, detail='Некорректный текст уточнения.')
    if inbox.context.raw_request:
        raise HTTPException(status_code=400, detail='Чистый запрос не поддерживает контекстные уточнения. Отправьте его после завершения либо используйте обычный режим.')
    allowed = {'chat_id', 'chat_reply', 'tags', 'requested_resources', 'full_web_pages', 'ui_resource_badges'}
    kwargs = {key: value for key, value in (raw.get('additional_kwargs') or {}).items() if key in allowed}
    imported = []
    sources = body.get('sources') or []
    if not isinstance(sources, list) or len(sources) > 100:
        raise HTTPException(status_code=400, detail='Слишком много вложений.')
    try:
        for source in sources:
            from core.attachment_memory import import_attachment
            attachment = await import_attachment(session_manager, memory, client,
                inbox.context.session_id, str(source.get('source_path') or ''), branch=inbox.context.branch,
                force_copy=source.get('_temporary_source') is True,
                to_memory=source.get('to_memory') is True, request_id=source.get('pending_id') or '', context=inbox.context)
            attachment['position'] = len(imported) + 1
            imported.append(attachment)
        if imported:
            kwargs['attachments'] = imported
        if not text.strip() and not imported:
            raise ValueError('Пустое уточнение.')
        message = HumanMessage(content=text, additional_kwargs=kwargs)
        ensure_history_ids([message])
        stamp_latest_user([message])
        await inbox.accept(message)
    except (ValueError, AttachmentError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {'accepted': True, 'id': message.additional_kwargs['chat_id']}


@app.post('/v1/chat/runs/{run_id}/stop-thinking')
async def stop_chat_thinking(run_id: str, request: Request):
    from core.reasoning_control import stop_thinking
    body = await request.json()
    return await stop_thinking(run_id, body.get('control_id'))


@app.get('/v1/memory/records')
async def memory_records_api(kind: str = 'permanent', session_id: str = ''):
    from core.memory_records import list_records
    try:
        return await asyncio.to_thread(list_records, kind, session_id or None)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete('/v1/memory/records/{identity}')
async def delete_memory_record_api(identity: str, kind: str = 'permanent', session_id: str = ''):
    from core.memory_records import delete_record
    try:
        return await asyncio.to_thread(delete_record, kind, identity, session_id or None)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get('/v1/resources/{kind}')
async def resource_catalog(kind: str, session_id: Optional[str] = None, branch: str = 'main', project_id: Optional[str] = None):
    from core.context import ExecutionContext
    allowed = None
    if session_id:
        if not session_manager.has_branch(session_id, branch):
            raise HTTPException(status_code=404, detail='Сессия или ветка не найдена.')
        meta, active = await asyncio.to_thread(session_manager.load_session_metadata, session_id, branch=branch)
        allowed = session_manager.resolve_session_project_settings(session_id, meta, branch=active).get('modules')
    elif project_id:
        for folder in session_manager._folder_chain(project_id):
            if folder['settings'].get('modules') is not None:
                allowed = folder['settings']['modules']
    context = ExecutionContext.create(session_id=session_id, branch=branch, allowed_modules=tuple(allowed) if allowed is not None else None)
    return {'resources': await asyncio.to_thread(registry.resource_catalog, kind, context)}


@app.get("/v1/pipelines")
async def pipeline_catalog(session_id: Optional[str] = None, branch: str = "main"):
    allowed = None
    if session_id:
        meta, active = await asyncio.to_thread(session_manager.load_session_metadata, session_id, branch=branch)
        allowed = session_manager.resolve_session_project_settings(session_id, meta, branch=active).get("modules")
    registry.refresh_package_tools()
    return {"pipelines": agent_core.pipeline_service.catalog(allowed)}


@app.get("/v1/pipelines/pending")
async def pipeline_pending(session_id: Optional[str] = None):
    return {"requests": agent_core.pipeline_service.broker.pending(session_id), "active_runs": list(_active_chat_runs)}


@app.post("/v1/pipelines/requests/{request_id}/resume")
async def pipeline_resume(request_id: str, request: Request):
    try:
        broker = agent_core.pipeline_service.broker
        has_waiter = request_id in broker.waiters
        answered = broker.respond(request_id, await request.json())
        if not has_waiter:
            from modules.pipelines.storage import load_layout
            rid = answered.get("run_id", "")
            replay = load_layout(agent_core.pipeline_service.root / f'{rid}_request.json')
            if replay.get("status") == "pending" and rid not in _active_chat_runs:
                async def recover():
                    async for _ in event_stream_generator(replay["body"]):
                        pass
                task = asyncio.create_task(recover())
                _recovered_pipeline_tasks.add(task)
                task.add_done_callback(_recovered_pipeline_tasks.discard)
        return answered
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/packages/{module}/knowledge/sources")
async def package_knowledge_sources(module: str, session_id: str = ""):
    from modules.pipelines.knowledge import temporary_sources
    try:
        agent_core.pipeline_service.store.read(module)
        sessions = await asyncio.to_thread(session_manager.list_sessions)
        if session_id and session_id not in sessions:
            raise ValueError("Сессия временной памяти не найдена")
        documents = await asyncio.to_thread(temporary_sources, session_manager.storage(session_id)) if session_id else []
        return {"sessions": sessions, "session_id": session_id, "documents": documents}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/packages/{module}/knowledge/{action}")
async def package_knowledge(module: str, action: str, request: Request):
    from modules.pipelines.knowledge import create_database, import_database
    store = agent_core.pipeline_service.store
    try:
        store.read(module)
        body = await request.json()
        if action == "create":
            session_id = body.pop("session_id", "")
            temporary_store = None
            if body.get("source_kind") == "temporary":
                if not session_id or not session_manager.has_session(session_id):
                    raise ValueError("Сессия временной памяти не найдена")
                temporary_store = session_manager.storage(session_id)
            result = await create_database(store, module, client, temporary_store=temporary_store, **body)
        elif action == "import":
            result = await asyncio.to_thread(import_database, store, module, body["path"])
        elif action == "refresh":
            result = {"success": True}
        else:
            raise ValueError("Неизвестное действие")
        registry.refresh_module_knowledge_bases(module)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/models")
async def list_models():
    return {
        "service": "toshy-core",
        "object": "list",
        "data": [{"id": model_name, "object": "model", "created": 1700000000, "owned_by": "core"}],
    }


@app.get("/v1/modules")
async def get_modules():
    return {"modules": registry.get_modules_status()}


@app.get("/v1/graph")
async def get_graph_snapshot():
    from core.graph_projection import attach_pipelines
    snapshot = registry.get_graph_snapshot()
    return await asyncio.to_thread(attach_pipelines, snapshot)


@app.post("/v1/modules/{module_name}/enable")
async def enable_module_api(module_name: str):
    return await registry.enable_module(module_name)


@app.post("/v1/modules/{module_name}/disable")
async def disable_module_api(module_name: str):
    return await registry.disable_module(module_name)


from modules.system.subagent_manager.api import install as install_subagent_api
install_subagent_api(app, registry, session_manager, lambda body: _prepare_chat(body))

# -------------------------------------------------------------------------
# Сессии: ядро является единственным владельцем файлов и истории.
# -------------------------------------------------------------------------
@app.get("/v1/sessions")
async def list_sessions_api():
    session_ids = session_manager.list_sessions()
    items = []
    for session_id in session_ids:
        try:
            _, metadata, active_branch = session_manager.load_session(session_id)
            items.append({
                "session_id": session_id,
                "metadata": metadata,
                "active_branch": active_branch,
                "branches": session_manager.list_branches(session_id),
            })
        except Exception as error:
            items.append({"session_id": session_id, "error": str(error), "branches": []})
    return {"sessions": session_ids, "items": items}


@app.post("/v1/sessions")
async def create_session_api(request: Request):
    body = await request.json()
    metadata = body.get("metadata", {}) or {}
    from core.memory_preference import last_memory_mode
    metadata.setdefault("temp_memory_mode", last_memory_mode())
    metadata.setdefault("include_thoughts_in_context", True)
    metadata.setdefault("include_replied_thoughts_in_context", False)
    folder_id = body.get("folder_id") or None
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else None
    async with _sessions_catalog_lock:
        try:
            number = session_manager.get_next_project_session_number(folder_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        metadata['project_session_number'] = number
        session_id = session_manager.get_next_default_session_id()
        session_manager.save_session(session_id, [], metadata, branch="main")
        try:
            placed = session_manager.set_session_folder(session_id, folder_id)
            if not placed.get("success"):
                raise ValueError(placed.get("message", "Invalid folder"))
            folder_id = placed["folder_id"]
            title = session_manager.set_default_session_title(session_id, number)
            if settings is not None:
                configured = session_manager.update_session_project_settings(session_id, settings)
                if not configured.get("success"):
                    raise ValueError(configured.get("message", "Invalid session settings"))
        except Exception as error:
            session_manager.delete_session(session_id)
            raise HTTPException(status_code=400, detail=str(error))

        # Capture the initial KB catalog at actual session creation time. Later
        # module/database changes intentionally do not rewrite this metadata or
        # the session system prompt; list_knowledge_bases provides the live view.
        _, metadata, active_branch = session_manager.load_session(session_id, branch="main")
        if "knowledge_base_snapshot" not in metadata:
            effective = session_manager.resolve_session_project_settings(
                session_id, metadata, branch=active_branch
            )
            allowed_modules = effective.get("modules")
            current_kbs = memory.list_knowledge_bases(
                allowed_modules,
                use_execution_context=False,
            )
            metadata["knowledge_base_snapshot"] = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "manifest": str(item.get("manifest") or ""),
                    "owner": str(item.get("owner") or ""),
                }
                for item in current_kbs
            ]
            session_manager.save_session(session_id, [], metadata, branch=active_branch)
    return {
        "success": True,
        "session_id": session_id,
        "title": title,
        "branch": "main",
        "metadata": metadata,
        "folder_id": folder_id,
        "settings": settings or {},
    }


# -------------------------------------------------------------------------
# Projects: logical hierarchy over sessions. Session files remain core-owned.
# -------------------------------------------------------------------------
@app.get("/v1/projects")
async def get_projects_api():
    return await asyncio.to_thread(session_manager.get_projects_snapshot)


@app.post('/v1/log-session')
async def attach_log_session(request: Request):
    from pathlib import Path
    from core.paths import APP_PATHS
    from core.log_session import LOG_ENV
    body = await request.json()
    try:
        path = Path(str(body.get('path') or '')).resolve(strict=True)
        if (path.parent != APP_PATHS.logs.resolve() or not path.is_file()
                or not re.fullmatch(r'app-session-[0-9-]+-[0-9a-f]{6}\.log', path.name)):
            raise ValueError('Invalid application log path')
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    os.environ[LOG_ENV] = str(path)
    core_logger.event('app', 'LOG_ATTACHED', 'Ядро подключено к журналу сеанса приложения')
    return {'success': True}


@app.get("/v1/system-prompt-preview")
async def get_system_prompt_preview_api(session_id: Optional[str] = None, branch: str = "main", raw_request: bool = False):
    if raw_request:
        from core.raw_request import context_preview
        return context_preview(session_id, branch)
    clean_session_id = str(session_id or "").strip()
    try:
        messages, metadata, active_branch, context = _prepare_chat({
            "session_id": clean_session_id or None,
            "branch": branch or "main",
            "messages": [],
        })
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    preview = await agent_core.preview_hidden_model_context(messages, context)
    markdown = render_system_prompt_preview_markdown(
        session_id=clean_session_id or None,
        branch=active_branch,
        base_system_prompt=str(preview.get("base_system_prompt") or ""),
        core_tools=preview.get("core_tools") or [],
        module_separator=str(preview.get("module_separator") or ""),
        module_sections=preview.get("module_sections") or [],
        hidden_history=preview.get("hidden_history") or [],
    )
    tool_count = len(preview.get("core_tools") or []) + sum(
        len(section.get("tools") or []) for section in (preview.get("module_sections") or [])
    )
    return {
        "session_id": clean_session_id or None,
        "branch": active_branch,
        "markdown": markdown,
        "tool_count": tool_count,
    }


@app.post("/v1/projects/folders")
async def create_project_folder_api(request: Request):
    body = await request.json()
    async with _sessions_catalog_lock:
        result = session_manager.create_project_folder(
            body.get("name", "New Folder"),
            body.get("parent_id"),
        )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unable to create folder"))
    return result


@app.patch("/v1/projects/folders/{folder_id}")
async def update_project_folder_api(folder_id: str, request: Request):
    body = await request.json()
    async with _sessions_catalog_lock:
        result = session_manager.update_project_folder(folder_id, body)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unable to update folder"))
    return result


@app.delete("/v1/projects/folders/{folder_id}")
async def delete_project_folder_api(folder_id: str):
    async with _sessions_catalog_lock:
        result = session_manager.delete_project_folder(folder_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unable to delete folder"))
    return result


@app.patch("/v1/projects/sessions/{session_id}")
async def update_project_session_api(session_id: str, request: Request):
    body = await request.json()
    async with _session_lock(session_id):
        result: Dict[str, Any] = {"success": True}
        if "folder_id" in body:
            result = session_manager.set_session_folder(session_id, body.get("folder_id"))
            if not result.get("success"):
                raise HTTPException(status_code=400, detail=result.get("message", "Unable to move session"))
        if "settings" in body:
            settings_result = session_manager.update_session_project_settings(session_id, body.get("settings") or {})
            if not settings_result.get("success"):
                raise HTTPException(status_code=400, detail=settings_result.get("message", "Unable to update settings"))
            result["settings"] = settings_result.get("settings")
        if "memory_mode" in body:
            memory_result = session_manager.set_session_memory_mode(session_id, body.get("memory_mode"))
            if not memory_result.get("success"):
                raise HTTPException(status_code=400, detail=memory_result.get("message", "Unable to update memory mode"))
            result["memory_mode"] = memory_result.get("memory_mode")
        if "include_thoughts_in_context" in body:
            thoughts_result = session_manager.set_session_include_thoughts_in_context(
                session_id,
                bool(body.get("include_thoughts_in_context")),
            )
            if not thoughts_result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=thoughts_result.get("message", "Unable to update thoughts context setting"),
                )
            result["include_thoughts_in_context"] = thoughts_result.get(
                "include_thoughts_in_context", True
            )
        if "include_replied_thoughts_in_context" in body:
            replied_result = session_manager.set_session_include_replied_thoughts_in_context(
                session_id, bool(body['include_replied_thoughts_in_context']))
            if not replied_result.get('success'):
                raise HTTPException(status_code=400, detail=replied_result.get('message'))
            result['include_replied_thoughts_in_context'] = replied_result['include_replied_thoughts_in_context']
    return result


@app.patch("/v1/projects/sessions/{session_id}/branches/{branch_name}")
async def update_project_branch_api(session_id: str, branch_name: str, request: Request):
    body = await request.json()
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    async with _session_lock(session_id):
        result = session_manager.update_branch_project_settings(
            session_id, branch_name, settings
        )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unable to update branch settings"))
    return result


@app.get("/v1/sessions/{session_id}/tasks")
async def get_session_tasks_api(session_id: str, branch: str = "main"):
    return {"success": True, **await asyncio.to_thread(session_manager.get_tasks_state, session_id, branch)}


@app.post("/v1/sessions/{session_id}/tasks")
async def add_session_tasks_api(session_id: str, request: Request):
    body = await request.json()
    raw_tasks = body.get("tasks")
    if raw_tasks is None and "text" in body:
        raw_tasks = [body.get("text")]
    if not isinstance(raw_tasks, list):
        raise HTTPException(status_code=400, detail="tasks must be a list")
    create_empty = bool(body.get("create_empty", False))
    if create_empty:
        raw_tasks = [""]
    async with _session_lock(session_id):
        result = session_manager.add_tasks(session_id, raw_tasks, allow_empty=create_empty, branch=body.get("branch", "main"))
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unable to add tasks"))
    return result


@app.patch("/v1/sessions/{session_id}/tasks/{task_id}")
async def update_session_task_api(session_id: str, task_id: str, request: Request):
    body = await request.json()
    allow_empty_text = bool(body.pop("allow_empty_text", False))
    async with _session_lock(session_id):
        result = session_manager.update_task(
            session_id,
            task_id,
            body,
            allow_empty_text=allow_empty_text,
            branch=body.get("branch", "main"),
        )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Unable to update task"))
    return result


@app.delete("/v1/sessions/{session_id}/tasks/{task_id}")
async def delete_session_task_api(session_id: str, task_id: str, branch: str = "main"):
    async with _session_lock(session_id):
        result = session_manager.delete_task(session_id, task_id, branch)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("message", "Task not found"))
    return result


@app.post("/v1/sessions/{session_id}/tasks/statuses")
async def apply_session_task_statuses_api(session_id: str, request: Request):
    body = await request.json()
    statuses = body.get("statuses", [])
    if not isinstance(statuses, list):
        raise HTTPException(status_code=400, detail="statuses must be a list")
    async with _session_lock(session_id):
        return session_manager.apply_task_statuses(
            session_id,
            statuses,
            mark_checked=bool(body.get("mark_checked", False)),
            branch=body.get("branch", "main"),
        )


@app.get("/v1/tasks/dirty-sessions")
async def get_dirty_task_sessions_api():
    return {"sessions": session_manager.get_dirty_task_sessions()}


@app.post("/v1/sessions/{session_id}/attachments/import")
async def import_session_attachment_api(session_id: str, request: Request):
    body = await request.json()
    source_path = str(body.get("source_path") or "").strip()
    if not source_path:
        raise HTTPException(status_code=400, detail="source_path is required")
    async with _session_lock(session_id):
        # Refuse attachment import into a non-existent session. This also keeps
        # empty/stale attachment directories from being created by bad IDs.
        if not session_manager.has_session(session_id):
            raise HTTPException(status_code=404, detail=f"Сессия '{session_id}' не найдена.")
        try:
            from core.attachment_memory import import_attachment
            attachment = await import_attachment(session_manager, memory, client, session_id, source_path,
                branch=body.get('branch') or 'main', force_copy=body.get('force_copy') is True,
                to_memory=body.get('to_memory') is True, request_id=body.get('request_id') or '')
        except AttachmentError as error:
            raise HTTPException(status_code=400, detail=str(error))
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"Не удалось добавить вложение: {error}")
    return {"success": True, "attachment": attachment}


@app.post("/v1/sessions/{session_id}/attachments/cleanup")
async def cleanup_session_attachments_api(session_id: str):
    async with _session_lock(session_id):
        if not session_manager.has_session(session_id):
            raise HTTPException(status_code=404, detail=f"Сессия '{session_id}' не найдена.")
        session_manager.cleanup_orphan_attachments(session_id)
    return {"success": True}


@app.get("/v1/sessions/{session_id}")
async def load_session_api(session_id: str, branch: Optional[str] = None, background_tasks: BackgroundTasks = None,
                           limit: Optional[int] = None, before: Optional[int] = None):
    # The generation mutex serializes mutations, not readers. SQLite supplies
    # committed snapshots while another task streams into the same session.
    active_runs = {identity for identity, task in _active_chat_runs.items() if not task.done()}

    def read_snapshot():
        page = {}
        if limit is None:
            messages, metadata, active_branch = session_manager.load_session(session_id, branch=branch)
        else:
            messages, metadata, active_branch, page = session_manager.load_session_page(
                session_id, branch=branch, limit=limit, before=before)
        effective = session_manager.resolve_session_project_settings(session_id, metadata, branch=active_branch)
        from core.chat_runs import visible_run_message
        for index, message in enumerate(messages):
            run = message.additional_kwargs.get('run_state', {})
            if run.get('status') in {'running', 'interrupted', 'stopped', 'unavailable'} and run.get('run_id'):
                try:
                    data = session_manager.storage(session_id).read_run_projection(run['run_id'])
                    if data.get('branch') != active_branch:
                        continue
                    if data.get('status') == 'running' and run['run_id'] not in active_runs:
                        data['status'] = 'interrupted'
                    messages[index] = visible_run_message(data)
                except (OSError, ValueError, KeyError):
                    pass
        response_meta = dict(metadata)
        response_meta["effective_project_settings"] = effective
        response_meta["workspace_root"] = effective.get("workspace_path") or default_workspace
        return {
            "messages": [message_to_dict(message) for message in messages],
            "metadata": response_meta,
            "branch": active_branch,
            "page": page,
            "timeline": session_manager.storage(session_id).read_timeline(active_branch, session_id),
        }

    result = await asyncio.to_thread(read_snapshot)
    if background_tasks is not None:
        # Repair orphan journals/free pages left by older versions after the
        # response has been sent. Opening a chat must not wait for VACUUM.
        background_tasks.add_task(_maintain_session_storage, session_id)
    return result


async def _maintain_session_storage(session_id):
    lock = _session_lock(session_id)
    if lock.locked():
        return  # Do not queue maintenance behind a running generation.
    async with lock:
        try:
            await asyncio.to_thread(session_manager.storage(session_id).prune_unreferenced_runs,
                                    protected=tuple(_active_chat_runs))
        except (OSError, ValueError) as error:
            core_logger.event('storage', 'SQLITE_MAINTENANCE_DEFERRED', str(error), level='WARNING',
                              context={'session_id': session_id})


@app.patch("/v1/sessions/{session_id}")
async def rename_session_api(session_id: str, request: Request):
    body = await request.json()
    lock = _session_lock(session_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Сессия сейчас выполняет запрос; повторите после его завершения.")
    async with _sessions_catalog_lock:
        async with lock:
            from core.session_titles import manual_title
            try:
                return manual_title(session_manager, session_id, body.get("new_name", ""))
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error))


@app.delete("/v1/sessions/{session_id}")
async def delete_session_api(session_id: str):
    lock = _session_lock(session_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail="Нельзя удалить сессию во время выполнения запроса.")
    async with _sessions_catalog_lock:
        async with lock:
            result = session_manager.delete_session(session_id)
            for module in list(registry.loaded_modules.values()):
                hook = getattr(module, 'session_closed', None)
                if hook:
                    await hook(session_id)
            return result


@app.get('/v1/compressed-context-preview')
async def compressed_context_preview_api(session_id: Optional[str] = None, branch: str = 'main'):
    from core.context_api import compressed_context_preview
    try:
        return await asyncio.to_thread(compressed_context_preview, session_manager, session_id, branch)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post('/v1/sessions/{session_id}/context/compact')
async def compact_context(session_id: str, request: Request):
    body = await request.json()
    branch = session_manager.clean_branch_name(body.get('branch') or 'main')
    if not session_manager.has_branch(session_id, branch):
        raise HTTPException(status_code=404, detail='Ветка не найдена.')
    from core.context_api import start, cancel
    if body.get('cancel'):
        return {'cancelled': cancel(session_id, branch)}
    try:
        return await start(agent_core, session_manager, session_id, branch, _session_lock(session_id))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get('/v1/sessions/{session_id}/token-usage')
async def session_token_usage_api(session_id: str, branch: str = 'main', app_id: str = '', refresh_model: bool = False):
    from core.token_usage import usage_snapshot
    if not await asyncio.to_thread(session_manager.has_session, session_id):
        raise HTTPException(status_code=404, detail='Session not found')
    if app_id and not re.fullmatch(r'[0-9a-f]{64}', app_id):
        raise HTTPException(status_code=400, detail='Invalid Studio session id')
    result = await usage_snapshot(session_manager, session_id, branch, app_id or None, refresh_model=refresh_model)
    from core.context_api import snapshot as compression_snapshot
    from core.context_settings import load_settings as context_settings
    result['compression'] = await compression_snapshot(session_manager, session_id, branch)
    options = await asyncio.to_thread(context_settings)
    result['compression_offer'] = {'enabled': options['enabled'], 'threshold': options['threshold']}
    if result.get('limit') is None:
        result['limit'] = options['context_limit'] or None
    return result


@app.patch("/v1/sessions/{session_id}/metadata")
async def update_session_metadata_api(session_id: str, request: Request):
    body = await request.json()
    branch = body.get("branch")
    updates = body.get("metadata", {}) or {}
    async with _session_lock(session_id):
        if not await asyncio.to_thread(session_manager.has_session, session_id):
            raise HTTPException(status_code=404, detail='Session not found')
        metadata, active_branch = await asyncio.to_thread(
            session_manager.update_session_metadata, session_id, updates, branch=branch)
    return {"success": True, "metadata": metadata, "branch": active_branch}


@app.put("/v1/sessions/{session_id}/messages")
async def replace_session_messages_api(session_id: str, request: Request):
    body = await request.json()
    branch = body.get("branch")
    try:
        messages = messages_from_dict(body.get("messages", []))
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Некорректный формат сообщений: {error}")
    def save_snapshot():
        metadata, active_branch = session_manager.load_session_metadata(session_id, branch=branch)
        updated_messages = messages
        old, _, _ = session_manager.load_session(session_id, branch=active_branch)
        from core.request_clock import preserve_clocks
        preserve_clocks(messages, old)
        prefix_count = body.get('preserve_prefix', 0)
        if prefix_count:
            if type(prefix_count) is not int or prefix_count < 0:
                raise HTTPException(status_code=400, detail='Некорректная граница истории')
            anchor = body.get('prefix_anchor')
            if len(old) < prefix_count or (anchor and (len(old) <= prefix_count or old[prefix_count].additional_kwargs.get('chat_id') != anchor)):
                raise HTTPException(status_code=409, detail='История изменилась. Обновите сессию перед сохранением.')
            updated_messages = old[:prefix_count] + messages
        session_manager.save_session(session_id, updated_messages, metadata, branch=active_branch)
        # Also collect journals left behind by older versions. A just-created
        # active run may not yet have its visible placeholder: protect it.
        session_manager.storage(session_id).prune_unreferenced_runs(protected=protected)
        # GUI deletions/attachment crosses arrive through replace_messages.
        # Only copied files unreferenced by every branch are removed.
        session_manager.cleanup_orphan_attachments(session_id)
        return active_branch

    async with _session_lock(session_id):
        protected = tuple(_active_chat_runs)
        active_branch = await asyncio.to_thread(save_snapshot)
    return {"success": True, "branch": active_branch}


@app.patch("/v1/sessions/{session_id}/messages/{identity}/pin")
async def set_message_pin_api(session_id: str, identity: str, request: Request):
    from core.chat_references import valid_chat_id, set_message_pin
    if not valid_chat_id(identity):
        raise HTTPException(status_code=400, detail='Некорректное сообщение')
    body = await request.json()
    lock = _session_lock(session_id)
    if lock.locked():
        raise HTTPException(status_code=409, detail='Сессия сейчас выполняет запрос')
    async with lock:
        messages, metadata, branch = session_manager.load_session(session_id, branch=body.get('branch', 'main'))
        if branch != body.get('branch', 'main'):
            raise HTTPException(status_code=404, detail='Ветка удалена')
        if not set_message_pin(messages, identity, bool(body.get('pinned'))):
            raise HTTPException(status_code=404, detail='Сообщение удалено')
        session_manager.save_session(session_id, messages, metadata, branch=branch)
    return {'success': True}


@app.post("/v1/sessions/{session_id}/messages/move-selection")
async def move_selected_messages_api(session_id: str, request: Request):
    from core.message_transfer import move_selection, StaleConversation
    body = await request.json()
    target = str(body.get('destination_session') or '')
    if not target:
        raise HTTPException(status_code=400, detail='Укажите целевую сессию')
    async with _sessions_catalog_lock:
        async with AsyncExitStack() as stack:
            for identity in sorted({session_id, target}):
                lock = _session_lock(identity)
                if lock.locked():
                    raise HTTPException(status_code=409, detail='Сессия выполняет запрос. Дождитесь его завершения.')
                await stack.enter_async_context(lock)
            try:
                return await asyncio.to_thread(move_selection, session_manager, session_id, **body)
            except StaleConversation as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/sessions/{session_id}/branches")
async def list_branches_api(session_id: str):
    async with _session_lock(session_id):
        return {"branches": session_manager.list_branches(session_id)}


@app.post('/v1/sessions/{session_id}/messages/forward')
async def forward_messages_api(session_id: str, request: Request):
    from core.message_transfer import forward_messages, StaleConversation
    body = await request.json()
    target = str(body.get('destination_session') or '')
    if not target:
        raise HTTPException(status_code=400, detail='Укажите целевой чат')
    async with _sessions_catalog_lock:
        async with AsyncExitStack() as stack:
            for identity in sorted({session_id, target}):
                lock = _session_lock(identity)
                if lock.locked():
                    raise HTTPException(status_code=409, detail='Чат выполняет запрос. Дождитесь его завершения.')
                await stack.enter_async_context(lock)
            try:
                return await asyncio.to_thread(forward_messages, session_manager, session_id, **body)
            except StaleConversation as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (ValueError, FileNotFoundError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/v1/sessions/{session_id}/branches/create")
async def create_branch_api(session_id: str, request: Request):
    body = await request.json()
    async with _session_lock(session_id):
        return session_manager.create_branch(
            session_id,
            body.get("branch_name", ""),
            source_branch=body.get("source_branch", "main"),
            fork_index=body.get("fork_index"),
        )


@app.post("/v1/sessions/{session_id}/branches/checkout")
async def checkout_branch_api(session_id: str, request: Request):
    body = await request.json()
    async with _session_lock(session_id):
        return session_manager.checkout_branch(session_id, body.get("branch_name", "main"))


@app.post("/v1/sessions/{session_id}/branches/delete")
async def delete_branch_api(session_id: str, request: Request):
    body = await request.json()
    async with _session_lock(session_id):
        return await asyncio.to_thread(session_manager.delete_branch, session_id, body.get("branch_name", ""))


@app.patch("/v1/sessions/{session_id}/branches/{branch_name}")
async def rename_branch_api(session_id: str, branch_name: str, request: Request):
    body = await request.json()
    async with _session_lock(session_id):
        from core.session_titles import manual_title
        try:
            return manual_title(session_manager, session_id, body.get("new_name", ""), branch_name)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))


def _incoming_messages(messages_raw: List[Dict[str, Any]]) -> List[BaseMessage]:
    messages: List[BaseMessage] = []
    for item in messages_raw:
        role, content = item.get("role"), item.get("content") or ""
        if role == "user":
            messages.append(HumanMessage(content=content, additional_kwargs=item.get("additional_kwargs", {})))
        elif role == "assistant":
            messages.append(AIMessage(content=content, additional_kwargs=item.get("additional_kwargs", {})))
    return messages


def _prepare_chat(body: Dict[str, Any]) -> Tuple[List[BaseMessage], Dict[str, Any], str, ExecutionContext]:
    messages_raw = body.get("messages", []) or []
    session_id = body.get("session_id")
    requested_branch = body.get("branch")
    from core.memory_preference import last_memory_mode
    incoming_meta = {
        "temp_memory_mode": body.get("temp_memory_mode", last_memory_mode()),
        "limit_ranking": body.get("limit_ranking", True),
        "workspace_root": body.get("workspace_root", default_workspace),
        "include_thoughts_in_context": body.get("include_thoughts_in_context", True),
        "include_replied_thoughts_in_context": body.get("include_replied_thoughts_in_context", False),
    }

    if session_id:
        stored_messages, metadata, active_branch = session_manager.load_session(
            session_id,
            default_meta=incoming_meta,
            branch=requested_branch,
        )
        # This is a session-wide policy edited through Projects. A cached
        # ChatStreamWorker may still send the old value; it must not overwrite
        # the saved setting. The body remains a fallback for legacy/new sessions.
        metadata.setdefault('include_thoughts_in_context', incoming_meta['include_thoughts_in_context'])
        metadata.setdefault('include_replied_thoughts_in_context', incoming_meta['include_replied_thoughts_in_context'])
        for key in ("workspace_root", "temp_memory_mode", "limit_ranking", "model_profile_id"):
            if key in body:
                metadata[key] = body[key]

        if body.get("use_provided_history", False):
            # Regeneration deliberately supplies an exact historical prefix.
            # Never merge the later saved conversation into that request, or
            # the regenerated answer would see messages from its own future.
            messages = _incoming_messages(messages_raw)
            from core.chat_references import CHAT_ID, REQUEST_MEMORY
            saved_users = {m.additional_kwargs.get(CHAT_ID): m for m in stored_messages if isinstance(m, HumanMessage)}
            for message in messages:
                original = saved_users.get(message.additional_kwargs.get(CHAT_ID))
                if isinstance(message, HumanMessage) and original is not None and original.content == message.content:
                    if isinstance(original.additional_kwargs.get('request_clock'), dict):
                        message.additional_kwargs['request_clock'] = dict(original.additional_kwargs['request_clock'])
                    cached = original.additional_kwargs.get(REQUEST_MEMORY)
                    if isinstance(cached, dict) and cached.get('source_content') == str(message.content or ''):
                        message.additional_kwargs.setdefault(REQUEST_MEMORY, dict(cached))
        else:
            messages = stored_messages
            last_user = next((item for item in reversed(messages_raw) if item.get("role") == "user"), None)
            if last_user:
                content = last_user.get("content") or ""
                additional_kwargs = last_user.get("additional_kwargs", {})
                saved_kwargs = dict(getattr(messages[-1], 'additional_kwargs', {}) or {}) if messages else {}
                if 'request_clock' not in additional_kwargs:
                    saved_kwargs.pop('request_clock', None)
                if 'chat_id' not in additional_kwargs:
                    for key in ('chat_id', 'chat_pinned'):
                        saved_kwargs.pop(key, None)
                if not (
                    messages
                    and isinstance(messages[-1], HumanMessage)
                    and messages[-1].content == content
                    and saved_kwargs == dict(additional_kwargs or {})
                ):
                    messages.append(HumanMessage(content=content, additional_kwargs=additional_kwargs))
    else:
        messages = _incoming_messages(messages_raw)
        metadata = incoming_meta
        active_branch = "main"

    from core.request_clock import stamp_latest_user
    if any(item.get('role') == 'user' for item in messages_raw) and not body.get('resume_run_id'):
        stamp_latest_user(messages)
    project_prompt = ""
    allowed_modules = None
    store_temp_memory_both = False
    workspace_root = metadata.get("workspace_root", default_workspace)
    if session_id:
        effective = session_manager.resolve_session_project_settings(
            session_id, metadata, branch=active_branch
        )
        project_prompt = effective.get("system_prompt", "")
        allowed_modules = effective.get("modules")
        store_temp_memory_both = bool(effective.get("store_temp_memory_both", False))
        workspace_root = effective.get("workspace_path") or default_workspace

    # Freeze the knowledge-base catalog once per saved session/branch. This
    # snapshot is intentionally NOT refreshed later: changing the live catalog
    # must not rewrite the session system prompt. The model can call
    # list_knowledge_bases for the current state.
    if "knowledge_base_snapshot" not in metadata:
        current_kbs = memory.list_knowledge_bases(
            allowed_modules,
            use_execution_context=False,
        )
        metadata["knowledge_base_snapshot"] = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "manifest": str(item.get("manifest") or ""),
                "owner": str(item.get("owner") or ""),
            }
            for item in current_kbs
        ]

    raw_kb_snapshot = metadata.get("knowledge_base_snapshot")
    if not isinstance(raw_kb_snapshot, list):
        raw_kb_snapshot = []
    kb_snapshot = [item for item in raw_kb_snapshot if isinstance(item, dict)]
    knowledge_base_snapshot_text = memory.format_knowledge_base_catalog(kb_snapshot)

    last_user_message = next((message for message in reversed(messages) if isinstance(message, HumanMessage)), None)
    if session_id:
        from core.interrupted_context import restore_interrupted_notes
        restore_interrupted_notes(messages, session_manager.storage(session_id))
    last_user_kwargs = dict(getattr(last_user_message, "additional_kwargs", {}) or {}) if last_user_message else {}
    task_status_check = bool(last_user_kwargs.get("task_status_check", False))
    if task_status_check and last_user_kwargs.get('task_status_refresh') and session_id:
        from core.task_review import build_task_status_prompt
        current_tasks = [t for t in session_manager.get_tasks_state(session_id, active_branch)['tasks'] if t['status'] == 'in_progress']
        last_user_message.content = build_task_status_prompt(current_tasks)
        last_user_message.additional_kwargs['task_status_task_ids'] = [t['id'] for t in current_tasks]
        last_user_kwargs = dict(last_user_message.additional_kwargs)
    raw_task_ids = last_user_kwargs.get("task_status_task_ids", []) if task_status_check else []
    if not isinstance(raw_task_ids, (list, tuple)):
        raw_task_ids = []
    task_status_expected_ids = tuple(dict.fromkeys(
        str(value).strip() for value in raw_task_ids if str(value).strip()
    ))

    from core.model_connections import load_settings
    profile_id = str(body.get('model_profile_id', metadata.get('model_profile_id', '')) or '')
    metadata['model_profile_id'] = profile_id
    # Resolve a default once, before the first model/tool step. It is also
    # journalled so changing the default cannot redirect a resumed operation.
    frozen_profile_id = profile_id or str(load_settings().get('selected') or '')
    metadata['model_profile_id'] = frozen_profile_id

    context = ExecutionContext.create(
        session_id=session_id,
        branch=active_branch,
        workspace_root=workspace_root,
        temp_memory_mode=(effective.get("memory_mode") if session_id else metadata.get("temp_memory_mode", "text")),
        store_temp_memory_both_formats=store_temp_memory_both,
        limit_ranking=metadata.get("limit_ranking", True),
        include_thoughts_in_context=metadata.get("include_thoughts_in_context", True),
        include_replied_thoughts_in_context=metadata.get("include_replied_thoughts_in_context", False),
        raw_request=bool(body.get('raw_request', False)),
        project_system_prompt=project_prompt,
        knowledge_base_snapshot_text=knowledge_base_snapshot_text,
        allowed_modules=tuple(allowed_modules) if allowed_modules is not None else None,
        selected_pipeline=body.get("pipeline", metadata.get("selected_pipeline", "")) if not task_status_check else "",
        model_profile_id=frozen_profile_id,
        task_status_check=task_status_check,
        full_web_pages=bool(last_user_kwargs.get('full_web_pages', False)) and not task_status_check,
        task_status_expected_ids=task_status_expected_ids,
    )
    effort = str(body.get('reasoning_effort', metadata.get('reasoning_effort', '')) or '')
    from core.reasoning_effort import valid_level
    if effort and not valid_level(effort):
        raise ValueError('Некорректное имя уровня reasoning effort.')
    metadata['reasoning_effort'] = effort
    context = replace(context, reasoning_effort=effort)
    if context.raw_request:
        # The raw route must not resolve or execute a selected pipeline.
        context = replace(context, selected_pipeline='')
    selected = context.selected_pipeline
    if selected:
        owner = selected.split("/", 1)[0]
        if owner not in registry.loaded_modules or (allowed_modules is not None and owner not in allowed_modules):
            selected = ""
            context = replace(context, selected_pipeline="")
    metadata["selected_pipeline"] = selected
    if body.get("run_id"):
        rid = str(body["run_id"])
        if not rid.isalnum():
            raise ValueError("Неверный ID запуска")
        context = replace(context, run_id=rid)
    if body.get('resume_run_id'):
        from core.chat_runs import load_checkpoint, resume_protocol
        resume_id = str(body['resume_run_id'])
        if not session_id:
            raise ValueError('Для продолжения требуется сохранённая сессия.')
        previous = load_checkpoint(context.storage_dir, resume_id)
        run = messages[-1].additional_kwargs.get('run_state', {}) if messages else {}
        if (previous.get('session_id') != session_id or previous.get('branch') != active_branch
                or run.get('run_id') != resume_id or previous.get('status') == 'completed'):
            raise ValueError('Можно продолжить только последний прерванный запуск текущей ветки.')
        mode = body.get('resume_mode', 'native')
        if mode not in {'native', 'logical'}:
            raise ValueError('Неверный способ продолжения.')
        messages = resume_protocol(previous)
        context = replace(context, run_id=resume_id, resume_run_id=resume_id, resume_mode=mode,
                          selected_pipeline=previous.get('selected_pipeline', ''),
                          model_profile_id=previous.get('model_profile_id', frozen_profile_id),
                          reasoning_effort=previous.get('reasoning_effort', ''),
                          full_web_pages=bool(previous.get('full_web_pages', False)))
    from core.chat_references import ensure_history_ids
    # A checkpoint is the existing model protocol, not a new UI history.
    # Adding IDs here inserts new user metadata before old assistant messages.
    if not body.get('resume_run_id'):
        ensure_history_ids(messages, session_id or context.run_id)
    return messages, metadata, active_branch, context



class _RunSegmentCollector:
    """Collects a lightweight UI transcript without changing model history."""

    def __init__(self):
        self.segments: List[Dict[str, Any]] = []
        self._assistant_index: Optional[int] = None
        self._phase_started_at: Optional[float] = None
        self._phase_marker_index: Optional[int] = None
        self._regenerating_answer = False
        self._pending_updates = []
        self._waiting_started_at: Optional[float] = time.perf_counter()

    def restore(self, segments, regenerating_answer=False):
        self._pending_updates = []
        self.segments = [dict(s) for s in segments]
        self._regenerating_answer = regenerating_answer
        if not self.segments:
            return
        last = self.segments[-1]
        if last.get('type') in {'assistant_stream', 'assistant_final'}:
            self._assistant_index = len(self.segments) - 1
        elif last.get('type') in {'reasoning', 'tool'}:
            self._phase_chat_id = last.get('chat_id')
            self._phase_marker_index = next((i for i, s in enumerate(self.segments)
                                            if s.get('chat_id') == self._phase_chat_id), len(self.segments) - 1)
            self._phase_started_at = time.perf_counter() - float(self.segments[self._phase_marker_index].get('phase_seconds', 0))

    def _flush_updates(self, through=None):
        if not self._pending_updates:
            return
        # При восстановлении применяемая правка уже может находиться в segments.
        # Не переносим вместе с ней более поздние, ещё не принятые моделью правки.
        if through is not None and not any(item.get('chat_id') == through for item in self._pending_updates):
            return
        self._close_phase()
        if self._assistant_index is not None:
            segment = self.segments[self._assistant_index]
            if segment.get('type') == 'assistant_stream':
                segment['type'] = 'assistant_interim'
        self._assistant_index = None
        while self._pending_updates:
            segment = self._pending_updates.pop(0)
            self.segments.append(segment)
            if segment.get('chat_id') == through:
                break

    def _ensure_phase(self) -> None:
        if self._phase_started_at is None:
            from core.chat_references import new_chat_id
            self._phase_chat_id = new_chat_id()
            self._phase_started_at = self._waiting_started_at if self._waiting_started_at is not None else time.perf_counter()
            self._waiting_started_at = None
            self._phase_marker_index = len(self.segments)

    def _close_phase(self) -> None:
        if self._phase_started_at is None:
            return
        elapsed = max(0.0, time.perf_counter() - self._phase_started_at)
        marker = self._phase_marker_index
        if marker is not None and 0 <= marker < len(self.segments):
            self.segments[marker]["phase_seconds"] = round(elapsed, 3)
        self._phase_started_at = None
        self._phase_marker_index = None
        self._waiting_started_at = None

    @staticmethod
    def _clip_text(value: Any, limit: int = 4000) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[:limit] + "\n… [truncated in session transcript]"

    def consume(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == 'pipeline_notification':
            if not any(s.get('type') == event_type and s.get('id') == event.get('id') for s in self.segments):
                self.segments.append({key: event[key] for key in ('type', 'id', 'text', 'color') if key in event})
            return
        if event_type == '_model_start':
            if self._phase_started_at is None and self._waiting_started_at is None:
                self._waiting_started_at = time.perf_counter()
            return
        if event_type == 'user_update':
            # Принятие правки ещё не означает, что хвост старого потока исчерпан.
            self._pending_updates.append({'type': 'user_update', 'message': event['message'],
                                          'chat_id': event['id'], 'run_id': event.get('run_id', '')})
            return
        if event_type == 'steering_applied':
            self._flush_updates(event['id'])
            for segment in self.segments:
                if segment.get('type') == 'user_update' and segment.get('chat_id') == event['id']:
                    segment['applied'] = True
            return
        if event_type == 'answer_continue':
            self._regenerating_answer = False
            if self._assistant_index is not None:
                self.segments[self._assistant_index]['type'] = 'assistant_stream'
            return
        if event_type == 'answer_restart':
            if self._assistant_index is not None:
                self.segments[self._assistant_index].update(text='', type='assistant_stream')
            return

        if event_type in {'direct_output', 'subagent_direct'}:
            self._close_phase()
            self._assistant_index = None
            for segment in self.segments:
                if segment.get('type') in {'assistant_stream','assistant_interim','assistant_final'}:
                    segment['type'] = 'reasoning'
            return

        if event_type == 'subagent':
            self._assistant_index = None
            from core.chat_references import new_chat_id
            segment = next((s for s in self.segments if s.get('type') == 'subagents'), None)
            if segment is None:
                segment = {'type': 'subagents', 'chat_id': new_chat_id(), 'results': []}
                self.segments.append(segment)
            event['chat_id'] = segment['chat_id']
            entry = {key:value for key,value in event.items() if key not in {'type','chat_id','streaming'}}
            previous = next((r for r in segment['results'] if r.get('id') == entry['id']), None)
            if previous is None:segment['results'].append(entry)
            else:previous.update(entry)
            return

        if event_type == "thought":
            delta = event.get("delta", "")
            if not delta:
                return
            if self._regenerating_answer and self._assistant_index is not None:
                index = next((i for i in range(self._assistant_index - 1, -1, -1)
                              if self.segments[i].get('type') == 'reasoning'), None)
                if index is None:
                    from core.chat_references import new_chat_id
                    index = self._assistant_index
                    self.segments.insert(index, {'type': 'reasoning', 'text': '', 'chat_id': new_chat_id()})
                    self._assistant_index += 1
                segment = self.segments[index]
                segment['text'] += delta
                self._phase_chat_id = segment['chat_id']
                if self._phase_started_at is None:
                    self._phase_marker_index = next(i for i, s in enumerate(self.segments) if s.get('chat_id') == self._phase_chat_id)
                    started = self._waiting_started_at if self._waiting_started_at is not None else time.perf_counter()
                    self._phase_started_at = started - float(self.segments[self._phase_marker_index].get('phase_seconds', 0))
                    self._waiting_started_at = None
                return
            self._ensure_phase()
            self._assistant_index = None
            if self.segments and self.segments[-1].get("type") == "reasoning":
                self.segments[-1]["text"] = self.segments[-1].get("text", "") + delta
            else:
                self.segments.append({"type": "reasoning", "text": delta, "chat_id": self._phase_chat_id})
            return

        if event_type == "content":
            delta = event.get("delta", "")
            if not delta:
                return
            if self._regenerating_answer:
                self._close_phase()
                self._regenerating_answer = False
            if self._assistant_index is None:
                self._close_phase()
                from core.chat_references import new_chat_id
                self.segments.append({"type": "assistant_stream", "text": "", "chat_id": new_chat_id()})
                self._assistant_index = len(self.segments) - 1
            self.segments[self._assistant_index]["text"] += delta
            return

        if event_type == "content_interim":
            if self._assistant_index is not None:
                segment = self.segments[self._assistant_index]
                if segment.get("type") == "assistant_stream":
                    segment["type"] = "assistant_interim"
                self._assistant_index = None
            return

        if event_type == "content_reset":
            if self._assistant_index is not None:
                if 0 <= self._assistant_index < len(self.segments):
                    self.segments.pop(self._assistant_index)
                self._assistant_index = None
            self._ensure_phase()
            return

        if event_type == "tool_start":
            self._ensure_phase()
            self._assistant_index = None
            self.segments.append({
                "type": "tool",
                "chat_id": self._phase_chat_id,
                "id": str(event.get("id") or ""),
                "name": str(event.get("name") or "tool"),
                "args": event.get("args") or {},
                "output": "",
            })
            return

        if event_type == "tool_result":
            tool_id = str(event.get("id") or "")
            tool_name = str(event.get("name") or "tool")
            output = str(event.get("output", ""))
            for segment in reversed(self.segments):
                if segment.get("type") != "tool":
                    continue
                if tool_id and str(segment.get("id") or "") == tool_id:
                    segment["output"] = output
                    segment["status"] = event.get("status") or ("failed" if event.get("error") else "completed")
                    segment["ui_sources"] = event.get("sources") or []
                    return
                if not tool_id and str(segment.get("name") or "") == tool_name and not segment.get("output"):
                    segment["output"] = output
                    segment["status"] = event.get("status") or ("failed" if event.get("error") else "completed")
                    segment["ui_sources"] = event.get("sources") or []
                    return
            self._ensure_phase()
            self.segments.append({
                "type": "tool",
                "id": tool_id,
                "chat_id": self._phase_chat_id if self._phase_started_at is not None else None,
                "name": tool_name,
                "args": {},
                "output": output,
                "status": event.get("status") or ("failed" if event.get("error") else "completed"),
                "ui_sources": event.get("sources") or [],
            })

    def finish(self) -> List[Dict[str, Any]]:
        self._flush_updates()
        self._close_phase()
        if self._assistant_index is not None:
            segment = self.segments[self._assistant_index]
            if segment.get("type") == "assistant_stream":
                segment["type"] = "assistant_final"
            self._assistant_index = None

        result: List[Dict[str, Any]] = []
        for segment in self.segments:
            item = dict(segment)
            if item.get("type") in {"reasoning", "assistant_interim", "assistant_final"}:
                if not str(item.get("text") or "").strip():
                    continue
            result.append(item)
        return result

def _last_assistant_message(
    final_state: Dict[str, Any],
    *,
    thinking_seconds: Optional[float] = None,
    thinking_had_activity: bool = False,
    thinking_tools_count: int = 0,
    run_segments: Optional[List[Dict[str, Any]]] = None,
) -> AIMessage:
    for message in reversed(final_state.get("messages", [])):
        if isinstance(message, AIMessage):
            kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
            if thinking_seconds is not None:
                kwargs["thinking_seconds"] = round(max(0.0, float(thinking_seconds)), 3)
                kwargs["thinking_had_activity"] = bool(thinking_had_activity)
                kwargs["thinking_tools_count"] = max(0, int(thinking_tools_count))
            if run_segments:
                kwargs["run_segments"] = [dict(segment) for segment in run_segments]
            return AIMessage(content=message.content or "", additional_kwargs=kwargs)
    kwargs = {}
    if thinking_seconds is not None:
        kwargs["thinking_seconds"] = round(max(0.0, float(thinking_seconds)), 3)
        kwargs["thinking_had_activity"] = bool(thinking_had_activity)
        kwargs["thinking_tools_count"] = max(0, int(thinking_tools_count))
    if run_segments:
        kwargs["run_segments"] = [dict(segment) for segment in run_segments]
    return AIMessage(content="", additional_kwargs=kwargs)


async def _save_visible_result(
    session_id: Optional[str],
    branch: str,
    metadata: Dict[str, Any],
    input_messages: List[BaseMessage],
    final_state: Dict[str, Any],
    *,
    thinking_seconds: Optional[float] = None,
    thinking_had_activity: bool = False,
    thinking_tools_count: int = 0,
    run_segments: Optional[List[Dict[str, Any]]] = None,
) -> None:
    if not session_id:
        return
    from core.chat_runs import visible_run_message
    projected = visible_run_message({
        'run_id': next((m.additional_kwargs.get('execution_run_id') for m in reversed(final_state['messages'])
                        if isinstance(m, AIMessage)), '') or '',
        'status': 'completed', 'segments': run_segments or [], 'partial': {}, 'tools': {},
        'protocol_messages': [message_to_dict(m) for m in final_state['messages']],
        'input_messages': [message_to_dict(m) for m in input_messages],
    }) if run_segments else _last_assistant_message(final_state)
    projected.additional_kwargs.update(thinking_seconds=round(max(0.0, float(thinking_seconds or 0)), 3),
                                       thinking_had_activity=thinking_had_activity, thinking_tools_count=thinking_tools_count)
    session_manager.save_session(
        session_id,
        input_messages + [projected],
        metadata,
        branch=branch,
    )
    last_user = next((message for message in reversed(input_messages) if isinstance(message, HumanMessage)), None)
    last_kwargs = dict(getattr(last_user, "additional_kwargs", {}) or {}) if last_user is not None else {}
    if not bool(last_kwargs.get("task_status_check", False)):
        session_manager.mark_task_activity(session_id, branch, last_user)


async def event_stream_generator(body: Dict[str, Any]):
    from core.chat_runs import ChatRunJournal, load_checkpoint, visible_run_message
    from core.llm_client import ContinuationUnsupported
    session_id = body.get('session_id')
    run_id = str(body.get('run_id') or '').strip()
    owner_task = asyncio.current_task()
    collector = _RunSegmentCollector()
    journal = None
    messages = []
    metadata = {}
    active_branch = body.get('branch') or 'main'
    if run_id and owner_task is not None:
        async with _active_chat_runs_lock:
            previous_task = _active_chat_runs.get(run_id)
            if previous_task and previous_task is not owner_task and not previous_task.done():
                yield 'data: ' + json.dumps({'type': 'error', 'message': 'Этот запуск уже выполняется.'}, ensure_ascii=False) + '\n\n'
                return
            _active_chat_runs[run_id] = owner_task
    try:
        async with _chat_guard(session_id):
            messages, metadata, active_branch, context = _prepare_chat(body)
            if session_id:
                ordinary = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
                if context.task_status_check:
                    session_manager.begin_task_review(session_id, active_branch)
                elif ordinary:
                    session_manager.mark_task_activity(session_id, active_branch, ordinary)
            run_id = context.run_id
            previous = None
            resume = None
            if context.resume_run_id:
                previous = load_checkpoint(context.storage_dir, context.resume_run_id)
                projected = visible_run_message(previous)
                collector.restore(projected.additional_kwargs.get('run_segments', []),
                                  regenerating_answer=projected.additional_kwargs['run_state']['stage'] == 'answer')
                resume = {**previous, 'stage': projected.additional_kwargs['run_state']['stage']}
                if resume['stage'] == 'answer' and not resume.get('partial'):
                    protocol = messages_from_dict(previous['protocol_messages'])
                    if protocol and isinstance(protocol[-1], AIMessage) and not protocol[-1].tool_calls:
                        notes = ''.join(re.findall(r'<think>[\s\S]*?</think>', protocol[-1].content or ''))
                        if notes:
                            resume['partial'] = {'content': notes, 'reasoning': '', 'tool_calls': {}, 'provider_deltas': []}
            journal = ChatRunJournal(context, messages, collector, previous=previous)
            # A stable pointer allows reconstruction from the checkpoint after
            # process exit, without rewriting the full session for each token.
            input_messages = messages_from_dict(journal.data['input_messages'])
            if session_id and body.get('persist_result', True):
                session_manager.save_session(session_id, input_messages + [journal.visible_message()], metadata, branch=active_branch)
            from modules.pipelines.storage import save_layout
            # The body must exist before any pipeline can persist a human
            # pause, including a crash before its SSE event reaches the GUI.
            # Finished runs remove this pointer instead of leaving a marker.
            save_layout(agent_core.pipeline_service.root / f'{run_id}_request.json', {'body': body, 'status': 'pending'})
            try:
                async with aclosing(agent_core.run_events(messages, context, journal=journal, resume=resume)) as events:
                    async for event in events:
                        kind = event.get('type')
                        if kind == 'done':
                            final_messages = event['state']['messages']
                            if final_messages and getattr(final_messages[-1], 'tool_calls', None):
                                raise RuntimeError('Достигнут лимит шагов. Запуск сохранён; его можно продолжить.')
                            from core.chat_runs import fold_completed_segments
                            collector.segments = fold_completed_segments(collector.finish())
                            journal.data['protocol_messages'] = [message_to_dict(m) for m in event['state']['messages']]
                            journal.data['partial'] = {}
                            journal.finish('completed')
                            input_messages = messages_from_dict(journal.data['input_messages'])
                            if session_id and body.get('persist_result', True):
                                session_manager.save_session(session_id, input_messages + [journal.visible_message()], metadata, branch=active_branch)
                                last_user = next((m for m in reversed(input_messages) if isinstance(m, HumanMessage)), None)
                                if last_user and not last_user.additional_kwargs.get('task_status_check'):
                                    session_manager.mark_task_activity(session_id, active_branch, last_user)
                            (agent_core.pipeline_service.root / f'{run_id}_request.json').unlink(missing_ok=True)
                            if session_id and body.get('persist_result', True):
                                from core.session_titles import schedule_title
                                schedule_title(client, session_manager, context, input_messages)
                            yield 'data: ' + json.dumps({'type': 'run_projection', 'segments': collector.segments}, ensure_ascii=False) + '\n\n'
                            yield 'data: ' + json.dumps({'type': 'run_status', **journal.visible_message().additional_kwargs['run_state']}, ensure_ascii=False) + '\n\n'
                            break
                        identity = None
                        if kind == 'content' and collector._assistant_index is not None:
                            identity = collector.segments[collector._assistant_index].get('chat_id')
                        elif kind in {'thought', 'tool_start', 'tool_result'}:
                            identity = getattr(collector, '_phase_chat_id', None)
                        if identity:
                            event = {**event, 'chat_id': identity}
                        yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
            except BaseException as error:
                status = 'stopped' if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else 'unavailable' if isinstance(error, ContinuationUnsupported) else 'interrupted'
                # A rejected prefix must not replace the original checkpoint
                # with an empty attempt or discard incomplete tool fragments.
                if isinstance(error, ContinuationUnsupported) and previous:
                    journal.data = previous
                    collector.restore(previous.get('segments', []))
                collector.finish()
                journal.finish(status, '' if status == 'stopped' else str(error))
                input_messages = messages_from_dict(journal.data['input_messages'])
                if session_id and body.get('persist_result', True):
                    session_manager.save_session(session_id, input_messages + [journal.visible_message()], metadata, branch=active_branch)
                # Stop is normal. Prevent generic pipeline recovery from
                # mistaking a saved chat checkpoint for a pending user pipeline.
                if not any(p.get('run_id') == run_id for p in agent_core.pipeline_service.broker.pending()):
                    (agent_core.pipeline_service.root / f'{run_id}_request.json').unlink(missing_ok=True)
                if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                    return
                yield 'data: ' + json.dumps({'type': 'run_status', **journal.visible_message().additional_kwargs['run_state']}, ensure_ascii=False) + '\n\n'
                code = 'continuation_unsupported' if isinstance(error, ContinuationUnsupported) else 'interrupted'
                yield 'data: ' + json.dumps({'type': 'error', 'code': code, 'message': str(error)}, ensure_ascii=False) + '\n\n'
    except asyncio.CancelledError:
        return
    except Exception as error:
        core_logger.error('STREAM_ERROR', str(error))
        yield 'data: ' + json.dumps({'type': 'error', 'message': str(error)}, ensure_ascii=False) + '\n\n'
    finally:
        if run_id and owner_task is not None:
            async with _active_chat_runs_lock:
                if _active_chat_runs.get(run_id) is owner_task:
                    _active_chat_runs.pop(run_id, None)
    yield 'data: [DONE]\n\n'


@app.post('/v1/model/credentials')
async def model_credentials(request: Request):
    # Protected by the same core-token middleware as every other /v1 route.
    # Never pass this body to graph, logging, or request journals.
    from core.model_connections import load_settings, remember_runtime_key
    body = await request.json()
    known = {item['id'] for item in load_settings()['connections']}
    keys = body.get('keys') or {}
    if not isinstance(keys, dict) or len(keys) > 100:
        raise HTTPException(status_code=400, detail='Некорректные ключи.')
    for identity, key in keys.items():
        if identity in known and isinstance(key, str) and len(key) < 16384:
            remember_runtime_key(identity, key)
    for identity in body.get('clear') or []:
        remember_runtime_key(identity, '')
    return {'ok': True}


@app.post('/v1/model/check')
async def model_check(request: Request):
    import httpx
    from core.model_connections import snapshot
    from core.model_transports import safe_error
    body = await request.json()
    operation = body.get('operation')
    if operation not in {'models', 'reasoning'}:
        raise HTTPException(status_code=400, detail='Неизвестная операция.')
    try:
        connection = await asyncio.to_thread(snapshot, llm_url, model_name, allow_missing_model=operation == 'models',
                                            connection_id=body.get('connection_id'))
        if operation == 'reasoning':
            from core.reasoning_effort import capabilities, supports_non_reasoning
            props = None
            if connection.mode == 'local':
                async with httpx.AsyncClient(timeout=3, headers=connection.headers(),
                        trust_env=connection.options.get('trust_env', True)) as http:
                    response = await http.get(connection.base_url.removesuffix('/v1') + '/props')
                    if response.status_code == 200:
                        props = response.json()
            levels = capabilities(connection, props)
            return {'levels': levels, 'non_reasoning': supports_non_reasoning(connection, props),
                    'model': connection.model,
                    'hint': '' if levels else 'Не удалось определить уровни reasoning effort для выбранного подключения. Проверьте модель и адрес в профиле. После обновления Toshy перезапустите её ядро.'}
        if operation == 'models':
            from core.model_providers import list_models as plugin_models
            catalog = await plugin_models(connection)
            if catalog is not None:
                return {'models': [str(item['id']) for item in catalog], 'details': catalog}
            async with httpx.AsyncClient(timeout=30, headers=connection.headers(),
                                         trust_env=connection.options.get('trust_env', True)) as http:
                response = await http.get(connection.base_url + '/models')
                if response.is_error:
                    raise safe_error(response, response.text, connection)
                data = response.json()
                return {'models': sorted({str(item.get('id') or item.get('name', '').removeprefix('models/'))
                    for item in data.get('data', data.get('models', [])) if item.get('id') or item.get('name')})}
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post('/v1/plugins/{service}/call')
async def plugin_service_call(service: str, request: Request):
    # Existing loopback authentication middleware also protects this route.
    if len(await request.body()) > 1024 * 1024:
        raise HTTPException(status_code=413, detail='Plugin request is too large.')
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Plugin request must be an object.')
    from core.plugin_services import call
    try:
        return await call(registry.plugins, service, payload)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    if body.get("stream", True):
        return StreamingResponse(event_stream_generator(body), media_type="text/event-stream")

    session_id = body.get("session_id")
    async with _chat_guard(session_id):
        try:
            messages, metadata, active_branch, context = _prepare_chat(body)
            if session_id:
                ordinary = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
                if context.task_status_check:
                    session_manager.begin_task_review(session_id, active_branch)
                elif ordinary:
                    session_manager.mark_task_activity(session_id, active_branch, ordinary)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        if context.task_status_check and not context.task_status_expected_ids:
            return {'choices':[{'message':{'role':'assistant','content':'Незавершённых задач нет.'}}]}
        final_state = None
        visible_content: List[str] = []
        run_started_at = time.perf_counter()
        final_content_started_at: Optional[float] = None
        thinking_had_activity = False
        thinking_tools_count = 0
        segment_collector = _RunSegmentCollector()
        async with aclosing(agent_core.run_events(messages, context)) as events:
            async for event in events:
                event_type = event.get("type")
                segment_collector.consume(event)
                if event_type == "thought":
                    thinking_had_activity = True
                elif event_type == "tool_start":
                    thinking_had_activity = True
                    thinking_tools_count += 1
                if event_type == "content":
                    if final_content_started_at is None:
                        final_content_started_at = time.perf_counter()
                    visible_content.append(event.get("delta", ""))
                elif event.get("type") == "content_interim":
                    # The non-streaming API returns only the final assistant segment.
                    visible_content.clear()
                    final_content_started_at = None
                elif event.get("type") == "content_reset":
                    visible_content.clear()
                    final_content_started_at = None
                elif event.get("type") == "done":
                    final_state = event["state"]
        if final_state is None:
            raise HTTPException(status_code=500, detail="Граф завершился без результата.")
        stop_at = final_content_started_at if final_content_started_at is not None else time.perf_counter()
        from core.chat_runs import fold_completed_segments
        if body.get("persist_result", True):
            await _save_visible_result(
                session_id,
                active_branch,
                metadata,
                messages,
                final_state,
                thinking_seconds=stop_at - run_started_at,
                thinking_had_activity=thinking_had_activity,
                thinking_tools_count=thinking_tools_count,
                run_segments=fold_completed_segments(segment_collector.finish()),
            )
            from core.session_titles import schedule_title
            schedule_title(client, session_manager, context, messages)

    return JSONResponse({
        "choices": [{"message": {"role": "assistant", "content": "".join(visible_content)}, "finish_reason": "stop", "index": 0}],
        "model": final_state.get('model_name', model_name),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5000)
