"""Skill service dispatched by the owning custom module."""

from dataclasses import replace
from fastapi import HTTPException
from core.context import ExecutionContext, bind_execution_context


async def handle_request(runtime, request, path, sessions):
    if path.strip("/") != "skills":
        raise HTTPException(status_code=404, detail="Ресурс модуля не найден.")
    if request.method == "GET":
        return {"skills": runtime.catalog()}
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Метод недоступен.")
    body = await request.json()
    if body.get("action") not in {"read", "copy"}:
        raise HTTPException(
            status_code=400, detail="Эта UI-операция скилла недоступна."
        )
    # This authenticated UI surface authors/inspects the selected module,
    # independently of which chat happens to be selected in the sidebar.
    context = replace(ExecutionContext.create(), run_id="ui-resources-" + runtime.owner)
    try:
        with bind_execution_context(context):
            return await runtime.action(
                body.get("action"),
                body.get("skill", ""),
                path=body.get("path", "SKILL.md"),
                content=body.get("content", ""),
                name=body.get("name", ""),
                preview=body.get("action") == "read",
            )
    except (ValueError, OSError, KeyError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
