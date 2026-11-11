"""Module-owned management endpoints, independent of execution availability."""
import asyncio
from typing import Optional
from fastapi import HTTPException, Request
from core.system_prompt import render_system_prompt_preview_markdown

def install(app, registry, session_manager, _prepare_chat):
    @app.get("/v1/subagents")
    async def get_subagents(session_id: Optional[str] = None, branch: str = "main"):
        from modules.system.subagent_manager.definitions import SubagentStore

        store = SubagentStore()
        pending = getattr(registry, "_subagent_drafts", {})
        pending = {
            key: value
            for key, value in pending.items()
            if "subagent:" + key in registry._pending_changes
        }
        configs = await asyncio.to_thread(store.read)
        stats = {}
        if session_id:
            from modules.system.subagent_manager.history import context_statistics
            from core.token_usage import session_connection

            storage = session_manager.storage(session_id)
            if storage.exists():
                parent_connection = await asyncio.to_thread(
                    session_connection, storage, branch
                )
                stats = await asyncio.to_thread(
                    context_statistics, storage, branch, configs, parent_connection
                )
        return {
            "subagents": configs,
            "settings": store.settings(),
            "context_stats": stats,
            "context_scope": {"session_id": session_id, "branch": branch},
            "module_enabled": "subagent_manager" in registry.loaded_modules,
            "modules": registry.get_modules_status(),
            "pending": pending,
            "settings_pending": "subagent-settings" in registry._pending_changes,
        }


    @app.post('/v1/subagents')
    async def create_subagent_api(request: Request):
        from modules.system.subagent_manager.definitions import SubagentStore
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict) or not isinstance(body.get('name', ''), str):
            raise HTTPException(status_code=400, detail='Expected an optional template name.')
        return {'success': True, 'subagent': await asyncio.to_thread(SubagentStore().create, body.get('name') or 'New subagent')}


    @app.put('/v1/subagents/settings')
    async def subagent_settings_api(request: Request):
        from modules.system.subagent_manager.definitions import SubagentStore
        data = await request.json()
        try:
            value = {'max_parallel': max(1, min(32, int(data.get('max_parallel', 4))))}
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail='Неверный лимит параллельных запусков')
        async def save():await asyncio.to_thread(SubagentStore().write_settings, value)
        return await registry.apply_when_idle('subagent-settings', save)


    @app.put('/v1/subagents/{identity}')
    async def update_subagent_api(identity: str, request: Request):
        from modules.system.subagent_manager.definitions import save_definition
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail='Ожидается объект настроек')
        try:
            saved = await save_definition(registry, body, identity=identity)
        except KeyError:
            raise HTTPException(status_code=404, detail='Субагент не найден')
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {'success': True, 'pending': False, 'subagent': saved}


    @app.post("/v1/subagents/{identity}/prompt-preview")
    async def subagent_prompt_preview_api(identity: str, request: Request):
        from dataclasses import replace
        from modules.system.subagent_manager.definitions import SubagentStore
        from modules.system.subagent_manager.history import subagent_system_prompt

        body = await request.json()
        config = SubagentStore().read().get(identity)
        if config is None:
            raise HTTPException(status_code=404, detail="Субагент не найден")
        try:
            config = SubagentStore.validate({**config, **(body.get("config") or {})})
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=str(error))
        _, _, branch, parent = _prepare_chat(
            {
                "session_id": body.get("session_id"),
                "branch": body.get("branch") or "main",
                "messages": [],
            }
        )
        allowed = (set(config["allowed_modules"]) & set(registry.loaded_modules)) - {
            "subagent_manager"
        }
        if parent.allowed_modules is not None:
            allowed &= set(parent.allowed_modules)
        context = replace(
            parent.child(), subagent_id=identity, allowed_modules=tuple(sorted(allowed))
        )
        async with registry.execution_snapshot(
            context.allowed_modules, context=context
        ) as (tools, addons, sections):
            markdown = render_system_prompt_preview_markdown(
                session_id=context.session_id,
                branch=branch,
                base_system_prompt=subagent_system_prompt(config, context),
                core_tools=[
                    {"name": name, "description": tools[name].description}
                    for name in registry.core_tool_names
                    if name in tools and tools[name].model_visible
                ],
                module_sections=sections if config.get('use_system_prompt', True) else [],
                module_separator="",
                hidden_history=(
                    [{"type": "request_guidance", "content": addons}] if addons and config.get('use_system_prompt', True) else []
                ),
            )
        return {"markdown": markdown, "name": config["name"], "id": identity}


    @app.delete('/v1/subagents/{identity}')
    async def delete_subagent_api(identity: str):
        from modules.system.subagent_manager.definitions import SubagentStore
        store = SubagentStore()
        try:store.path(identity)
        except ValueError as error:raise HTTPException(status_code=400, detail=str(error))
        async def remove():
            await asyncio.to_thread(store.delete, identity)
            getattr(registry, '_subagent_drafts', {}).pop(identity, None)
        return await registry.apply_when_idle('subagent:'+identity, remove)


