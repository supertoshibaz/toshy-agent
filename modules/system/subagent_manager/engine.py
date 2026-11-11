"""Parallel, request-scoped subagent graphs using the parent's permitted tools."""

from __future__ import annotations
import asyncio
import copy
import json
import time
from dataclasses import replace
from contextlib import ExitStack, aclosing
from typing import Annotated, TypedDict

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
)
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from core.context import get_execution_context, bind_execution_context
from core.llm_client import LlmClient
from core.reasoning_control import finish_requested, finalizable_stream, FinalizationRequested

from core.logger import core_logger
from core.registry import BaseModule, BaseTool
from core.sqlite_storage import SQLiteStore
from modules.system.subagent_manager.definitions import (
    SubagentStore,
    SubagentResult,
    delegation_catalog,
    save_definition,
    delegation_frame,
    delegation_scope,
    track_subagent,
)
from modules.system.subagent_manager.history import (
    retained_messages,
    request_context,
    subagent_system_prompt,
    subagent_connection,
)
from .pipeline_extension import make_result, incoming_result, result_message, sequential_requests


def _finished_output(journal,text=''):
    text=text or 'Субагент завершён по запросу пользователя. Оставшиеся действия не выполнялись.'
    known='\n\n'.join(str(item.get('name') or '')+':\n'+str(item.get('output') or '')
        for item in (journal.get('tools') or {}).values() if item.get('status')=='completed')[-64000:]
    return text+('\n\nПолученные результаты инструментов:\n'+known if known else '')


class SubAgentState(TypedDict):
    messages: Annotated[list, add_messages]
    steps_count: int


class SubagentEngine:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.registry = None
            cls._instance._limit = None
        return cls._instance

    def set_registry(self, registry):
        self.registry = registry

    def get_all_subagent_configs(self):
        return SubagentStore().read()

    def save_subagent_config(self, name, data):
        return SubagentStore().write(name, data)

    def delete_subagent_config(self, name):
        SubagentStore().delete(name)
        return True

    async def execute_subagent(self, agent_name, task, direct_output=None, *, previous_result=None):
        parent = get_execution_context()
        frame = delegation_frame()
        if parent.subagent_id:
            return make_result(
                "Ошибка: вложенное делегирование отключено; последовательности собираются в редакторе пайплайнов."
            )
        catalog = delegation_catalog()
        identity = (
            agent_name
            if agent_name in catalog
            else next(
                (key for key, cfg in catalog.items() if cfg["name"] == agent_name), ""
            )
        )
        if not identity or self.registry is None:
            return make_result(
                "Ошибка: субагент отключён или недоступен для текущего запроса."
            )
        cfg = copy.deepcopy(catalog[identity])
        child = replace(parent.child(), subagent_id=identity)

        with track_subagent(child):
            return await self._run_subagent(
                parent, child, frame, cfg, identity, task, direct_output, previous_result
            )

    async def _run_subagent(
        self, parent, child, frame, cfg, identity, task, direct_output, previous_result=None
    ):
        async def discard_event(event):
            pass

        emit = (
            frame["emit"] if frame and frame.get("emit") is not None else discard_event
        )
        group = frame.get("call_id", "") if frame else ""
        record = dict(
            id=child.run_id,
            agent_id=identity,
            name=cfg["name"],
            task=str(task),
            status="queued",
            output="",
            thoughts="",
            error="",
            direct=(
                bool(cfg.get("direct_output"))
                if direct_output is None
                else bool(direct_output)
            ),
            group=group,
        )
        store = SQLiteStore(child.storage_dir / "session.sqlite")
        journal = dict(
            run_id=child.run_id,
            parent_run_id=parent.run_id,
            branch=child.branch,
            status="queued",
            subagent_id=identity,
            protocol_messages=[],
            subagent_context_version=1,
            started_at=time.time(),
        )

        async def persist():
            write = asyncio.create_task(
                asyncio.to_thread(store.write_run, copy.deepcopy(journal))
            )
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                # A late running checkpoint must not overwrite the stopped one.
                await write
                raise

        async def publish(status, output=None):
            record["status"] = status
            if output is not None:
                record["output"] = str(output)
            journal.update(status=status, result=dict(record), updated_at=time.time())
            await persist()
            if emit:
                await emit({"type": "subagent", **record})

        limit = SubagentStore().settings()["max_parallel"]
        if self._limit != limit:
            self._limit, self._slots = limit, asyncio.Semaphore(limit)
        try:
            await publish("queued")
            async with self._slots:
                if finish_requested(child):
                    await publish('stopped',_finished_output(journal))
                    return make_result(record['output'],record)
                await publish("running")
                allowed = (
                    set(cfg.get("allowed_modules") or [])
                    & set(self.registry.loaded_modules)
                ) - {"subagent_manager"}
                if parent.allowed_modules is not None:
                    allowed &= set(parent.allowed_modules)
                child = replace(child, allowed_modules=tuple(sorted(allowed)))
                async with self.registry.execution_snapshot(
                    child.allowed_modules, context=child
                ) as (tools, addons, sections):
                    if frame:
                        tools = {
                            name: frame["tools"][name]
                            for name in tools
                            if name in frame["tools"]
                        }
                    tools = {
                        name: tool
                        for name, tool in tools.items()
                        if name
                        not in {
                            "delegate_to_subagent",
                            "delegate_to_subagents",
                            "create_or_update_subagent",
                        }
                    }
                    client = LlmClient()
                    if cfg.get("model_profile_id"):
                        connection = await asyncio.to_thread(
                            client.model_connection, cfg["model_profile_id"]
                        )
                    else:
                        connection = subagent_connection(cfg, client.model_connection())
                    child = replace(
                        child,
                        model_profile_id=str(
                            cfg.get("model_profile_id") or
                            ('' if (cfg.get('llm') or {}).get('api_url') else parent.model_profile_id)
                        ),
                    )
                    journal["model_fingerprint"] = connection.fingerprint
                    system = subagent_system_prompt(cfg, child)
                    guidance = (
                        [addons] if addons else []
                    ) + self.registry.get_request_messages(child, tools)
                    if not cfg.get('use_system_prompt', True):
                        guidance = []
                    messages = []
                    if cfg.get("context_mode") == "conversation" and frame:
                        for message in frame["messages"]:
                            if (
                                isinstance(message, HumanMessage)
                                or isinstance(message, AIMessage)
                                and not message.tool_calls
                            ):
                                messages.append(message.model_copy(deep=True))
                            elif isinstance(message, ToolMessage):
                                messages.append(
                                    HumanMessage(
                                        content="Saved result of tool "
                                        + str(message.name or message.tool_call_id)
                                        + ":\n"
                                        + str(message.content)
                                    )
                                )
                    parent_runs = None
                    if frame is not None:
                        parent_runs = {parent.run_id, parent.resume_run_id}
                        parent_runs.update(
                            message.additional_kwargs.get("run_state", {}).get("run_id")
                            for message in frame["messages"]
                        )
                        parent_runs.discard(None)
                        parent_runs.discard("")
                    history = await asyncio.to_thread(
                        retained_messages,
                        store,
                        child.branch,
                        identity,
                        connection.fingerprint,
                        exclude_run=child.run_id,
                        parent_runs=parent_runs,
                    )
                    messages.extend(history)
                    journal["interaction_start"] = len(messages)
                    if previous_result is not None:
                        messages.append(result_message(previous_result))
                    messages = [
                        *messages,
                        HumanMessage(
                            content=str(task)
                            + (
                                "\n\nInstructions for this task:\n"
                                + "\n".join(guidance)
                                if guidance
                                else ""
                            )
                        ),
                    ]
                    schemas = self.registry.get_openai_tool_schemas(tools)
                    from core.context_compression import ContextCompressor
                    from core.token_usage import usage_scope
                    compressor = ContextCompressor(client, child, emit)

                    async def agent_node(state):
                        from core.graph import _ThinkStreamFilter
                        from core.token_usage import discover_limit

                        if finish_requested(child):
                            answer=AIMessage(content=_finished_output(journal))
                            record['output']=answer.content
                            journal['protocol_messages']=[message_to_dict(m) for m in [*state['messages'],answer]]
                            journal['partial']={};await persist()
                            return {'messages':[answer],'steps_count':state['steps_count']}

                        parser = _ThinkStreamFilter()
                        visible, reasoning, call_parts = [], [], {}
                        provider_data = {}
                        preceding_thoughts = record["thoughts"]
                        last_emit = last_save = 0.0

                        async def update_stream(force=False):
                            nonlocal last_emit, last_save
                            record["output"] = "".join(visible)
                            record["thoughts"] = preceding_thoughts + "".join(reasoning)
                            now = time.monotonic()
                            if force or now - last_emit >= 0.05:
                                await emit(
                                    {"type": "subagent", **record, "streaming": True}
                                )
                                last_emit = now
                            if now - last_save >= 0.25:
                                journal.update(
                                    result=dict(record),
                                    partial={
                                        "content": record["output"],
                                        "reasoning": record["thoughts"],
                                    },
                                    updated_at=time.time(),
                                )
                                await persist()
                                last_save = now

                        def display(event):
                            (
                                reasoning if event["type"] == "thought" else visible
                            ).append(event["delta"])

                        journal["protocol_messages"] = [
                            message_to_dict(m) for m in state["messages"]
                        ]
                        await persist()
                        try:
                            use_system = cfg.get('use_system_prompt', True)
                            projected = (await compressor.apply(state['messages'], system, schemas)
                                         if use_system else state['messages'])
                            stream = client.chat_completion_stream(
                                ([SystemMessage(content=system)] if use_system else []) + list(projected),
                                tools=schemas or None,
                                session_id=child.session_id,
                                system_prompt_enabled=use_system,
                            )
                            stream = finalizable_stream(stream,child)
                            async with aclosing(stream):
                                async for delta in stream:
                                    provider_data = (
                                        delta.get("_provider_data") or provider_data
                                    )
                                    thought = (
                                        delta.get("reasoning_content")
                                        or delta.get("reasoning")
                                        or ""
                                    )
                                    if thought:
                                        for buffered in parser.flush():
                                            display(buffered)
                                        parser.in_thought = False
                                        reasoning.append(thought)
                                    if delta.get("content"):
                                        for event in parser.feed(delta["content"]):
                                            display(event)
                                    for fragment in delta.get("tool_calls") or []:
                                        index = fragment.get("index", 0)
                                        entry = call_parts.setdefault(
                                            index,
                                            {
                                                "id": child.run_id
                                                + "_"
                                                + str(state["steps_count"])
                                                + "_"
                                                + str(index),
                                                "name": "",
                                                "arguments": "",
                                            },
                                        )
                                        if fragment.get("id"):
                                            entry["id"] = fragment["id"]
                                        function = fragment.get("function") or {}
                                        if function.get("name"):
                                            entry["name"] = function["name"]
                                        if function.get("arguments"):
                                            entry["arguments"] += function["arguments"]
                                    await update_stream()
                        except FinalizationRequested:
                            pass
                        finally:
                            for event in parser.flush():
                                display(event)
                            await update_stream(force=True)
                            snapshot = await asyncio.to_thread(
                                request_context, store, child.run_id
                            )
                            if snapshot["limit"] is None:
                                snapshot["limit"] = (journal.get("context") or {}).get(
                                    "limit"
                                )
                            journal["context"] = snapshot
                        calls = []
                        for entry in ([] if finish_requested(child) else call_parts.values()):
                            args = json.loads(entry["arguments"] or "{}")
                            if not isinstance(args, dict):
                                raise ValueError("Некорректные аргументы инструмента")
                            calls.append(
                                dict(
                                    name=entry["name"],
                                    args=args,
                                    id=entry["id"],
                                    type="tool_call",
                                )
                            )
                        text, notes = "".join(visible), "".join(reasoning)
                        if finish_requested(child):
                            provider_data={}
                            text=_finished_output(journal,text)
                            record['output']=text
                        answer = AIMessage(
                            content=("<think>" + notes + "</think>\n" if notes else "")
                            + text,
                            tool_calls=calls,
                            additional_kwargs={"provider_data": provider_data},
                        )
                        journal["protocol_messages"] = [
                            message_to_dict(m) for m in [*state["messages"], answer]
                        ]
                        journal["partial"] = {}
                        if snapshot["limit"] is None:
                            snapshot["limit"] = await discover_limit(connection)
                        journal["context"] = snapshot
                        if calls:
                            if text:
                                record["thoughts"] += "\n\n" + text
                            record["thoughts"] += "\n\n"
                            record["output"] = ""
                            await emit(
                                {"type": "subagent", **record, "streaming": True}
                            )
                        await persist()
                        return {
                            "messages": [answer],
                            "steps_count": state["steps_count"] + 1,
                        }

                    async def tool_node(state):
                        results = []
                        for call in state["messages"][-1].tool_calls:
                            journal.setdefault("tools", {})[call["id"]] = {
                                "name": call["name"],
                                "args": call["args"],
                                "status": "running",
                            }
                            await persist()
                            with ExitStack() as scopes:
                                service = getattr(
                                    self.registry, "pipeline_service", None
                                )
                                if service is not None:
                                    scopes.enter_context(
                                        service.frame(
                                            state["messages"],
                                            child,
                                            tools,
                                            emit,
                                            call_id=call["id"],
                                        )
                                    )
                                scopes.enter_context(
                                    delegation_scope(
                                        child,
                                        tools,
                                        state["messages"],
                                        emit,
                                        call_id=call["id"],
                                    )
                                )
                                output = await self.registry.execute_tool(
                                    call["name"],
                                    call["args"],
                                    context=child,
                                    tools=tools,
                                )
                            journal["tools"][call["id"]].update(
                                status="completed", output=str(output)
                            )
                            results.append(
                                ToolMessage(
                                    content=str(output), tool_call_id=call["id"]
                                )
                            )
                            journal["protocol_messages"] = [
                                message_to_dict(m)
                                for m in [*state["messages"], *results]
                            ]
                            await persist()
                        return {"messages": results}

                    def route(state):
                        last = state["messages"][-1]
                        if not last.tool_calls:
                            return "end"
                        if state["steps_count"] >= int(cfg.get("max_steps", 10)):
                            raise RuntimeError(
                                "Достигнут лимит шагов субагента; задача не завершена."
                            )
                        return "tools"

                    graph = StateGraph(SubAgentState)
                    graph.add_node("agent", agent_node)
                    graph.add_node("tools", tool_node)
                    graph.add_edge(START, "agent")
                    graph.add_conditional_edges(
                        "agent", route, {"end": END, "tools": "tools"}
                    )
                    graph.add_edge("tools", "agent")
                    with bind_execution_context(child), client.bind_connection(
                        connection
                    ), usage_scope(observer=compressor.observe):
                        final = await graph.compile().ainvoke(
                            {"messages": messages, "steps_count": 0},
                            config={
                                "recursion_limit": max(
                                    25, int(cfg.get("max_steps", 10)) * 2 + 5
                                )
                            },
                        )
                        if not finish_requested(child) and cfg.get('use_system_prompt', True):await compressor.finish(final['messages'], system, schemas)
                        if compressor.settings and compressor.settings['micro_compact']:
                            compacted_context = await asyncio.to_thread(request_context, store, child.run_id)
                            compacted_context['limit'] = compacted_context['limit'] or (journal.get('context') or {}).get('limit')
                            journal['context'] = compacted_context
                    answer = record["output"].strip()
                    if not answer:
                        raise RuntimeError("Субагент завершился без ответа.")
                    await publish("stopped" if finish_requested(child) else "completed", answer)
                    return make_result(
                        f"Результат субагента {cfg['name']} ({identity}):\n\n{answer}",
                        record,
                    )
        except asyncio.CancelledError:
            record["error"] = (
                "Выполнение остановлено. Действия автоматически не повторяются."
            )
            await publish("stopped")
            raise
        except Exception as error:
            record["error"] = str(error)
            await publish("error")
            core_logger.error("SUBAGENT_ERROR", f"{identity}: {error}")
            return make_result(f"Ошибка субагента {cfg['name']}: {error}", record)


class DelegateToSubagentTool(BaseTool):
    """Hidden compatibility endpoint for saved graphs and interrupted calls."""
    model_visible = False
    catalog_hidden = True
    pipeline_hidden = True
    pipeline_title = "Делегировать субагентам"
    parallel_group = "subagents"
    repeat_after_interruption = False
    result_metadata = {"subagent_result": True}
    def __init__(self, engine):
        super().__init__(
            name="delegate_to_subagent",
            description="Legacy single-subagent endpoint. Use delegate_to_subagents for new calls.",
            args_schema={
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "task": {"type": "string"},
                    "direct_output": {"type": "boolean"},
                },
                "required": ["agent_name", "task"],
            },
        )
        self.engine = engine

    async def execute(self, agent_name, task, direct_output=None, **kwargs):
        return await self.engine.execute_subagent(
            agent_name, task, direct_output, previous_result=incoming_result()
        )


class DelegateToSubagentsTool(BaseTool):
    pipeline_title = "Делегировать субагентам"
    parallel_group = "subagents"
    repeat_after_interruption = False
    result_metadata = {"subagent_result": True}
    def __init__(self, engine):
        super().__init__(
            name="delegate_to_subagents",
            description="Delegate one task (a one-item tasks array) or 2-32 independent tasks to enabled subagents concurrently. A sequential pipeline block runs these jobs one at a time. The same agent may receive multiple independent jobs. A pipeline handoff supplies the previous node result to each job. Results are collected after all jobs complete; use direct_output only for results ready to show without rewriting.",
            args_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_name": {"type": "string"},
                                "task": {"type": "string"},
                                "direct_output": {"type": "boolean"},
                            },
                            "required": ["agent_name", "task"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        )
        self.engine = engine

    async def execute(self, tasks, **kwargs):
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
            raise ValueError('Укажите от 1 до 32 заданий')
        for task in tasks:
            if (not isinstance(task, dict) or not isinstance(task.get('agent_name'), str)
                    or not task['agent_name'].strip() or not isinstance(task.get('task'), str)
                    or not task['task'].strip()):
                raise ValueError('Каждое задание должно содержать непустые agent_name и task')
            if task.get('direct_output') is not None and not isinstance(task['direct_output'], bool):
                raise ValueError('direct_output должен быть логическим значением')
        previous = incoming_result()
        async def run(task):
            try:
                return await self.engine.execute_subagent(task['agent_name'], task['task'],
                    task.get('direct_output'), previous_result=previous)
            except (asyncio.CancelledError, FinalizationRequested):
                raise
            except Exception as error:
                from modules.pipelines.runtime import PipelineShutdown
                if isinstance(error, PipelineShutdown): raise
                return make_result(f"Ошибка субагента {task['agent_name']}: {error}")
        if sequential_requests():
            results = [await run(task) for task in tasks]
        else:
            jobs = [asyncio.create_task(run(task)) for task in tasks]
            try:
                results = await asyncio.gather(*jobs)
            finally:
                for job in jobs:
                    if not job.done(): job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
        if len(results) == 1: return results[0]
        payloads = [dict(getattr(r, 'pipeline_result', None) or {
            'status':'error', 'error':str(r), 'output':''}) for r in results]
        failed = any(p.get('status') != 'completed' for p in payloads)
        error = '\n'.join(str(p.get('name') or tasks[i]['agent_name']) + ': '
            + str(p.get('error') or p.get('status') or 'Unknown result')
            for i,p in enumerate(payloads) if p.get('status') != 'completed')
        text = '\n\n'.join(str(r) for r in results)
        result = SubagentResult(text, direct=not failed and all(getattr(r, 'direct', False) for r in results))
        result.pipeline_result = {'status':'error' if failed else 'completed', 'output':text,
            'error':error, 'name':f'Batch of {len(results)} subagents', 'agent_id':'', 'id':'', 'results':payloads}
        return result


class ListSubagentsTool(BaseTool):
    def __init__(self, engine):
        super().__init__(
            name="list_subagents",
            description="List enabled subagents available for this request.",
            args_schema={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs):
        catalog = delegation_catalog()
        return json.dumps(
            [
                {
                    "id": key,
                    "name": v["name"],
                    "description": v.get("description", ""),
                    "direct_output": v.get("direct_output", False),
                }
                for key, v in catalog.items()
            ],
            ensure_ascii=False,
        )


class CreateOrUpdateSubagentTool(BaseTool):
    def __init__(self, engine):
        self.engine = engine
        super().__init__(
            name="create_or_update_subagent",
            description="Create or update a reusable subagent definition immediately. Match existing definitions by name. Omitted fields preserve existing settings, including the model connection. Set model_profile_id to an empty string explicitly to inherit the main model. New enabled definitions require a description. Running subagents keep their startup settings; updates affect subsequent launches.",
            args_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "allowed_modules": {"type": "array", "items": {"type": "string"}},
                    "max_steps": {"type": "integer"},
                    "model_profile_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "direct_output": {"type": "boolean"},
                },
                "required": ["name"],
            },
        )

    async def execute(
        self,
        name,
        description=None,
        system_prompt=None,
        allowed_modules=None,
        max_steps=None,
        model_profile_id=None,
        enabled=None,
        direct_output=None,
        **kwargs,
    ):
        data = {key: value for key, value in dict(
            name=name, description=description, system_prompt=system_prompt,
            allowed_modules=allowed_modules, max_steps=max_steps,
            model_profile_id=model_profile_id, enabled=enabled,
            direct_output=direct_output,
        ).items() if value is not None}
        if model_profile_id is not None:
            data['llm'] = None
        saved = await save_definition(self.engine.registry, data, defaults={'enabled': True})
        return json.dumps({
            'id': saved['id'], 'name': saved['name'], 'enabled': saved['enabled'],
            'pending': False,
            'message': ('Шаблон сохранён. Субагента можно вызвать прямо сейчас, в этом же запросе.'
                        if saved['enabled'] else 'Шаблон сохранён и отключён.'),
        }, ensure_ascii=False)


class Module(BaseModule):
    def dispatch_scope(self, context, tools, messages, emit, *, call_id=""):
        return delegation_scope(context, tools, messages, emit, call_id=call_id)

    def refresh_request_guidance(self, frozen, context, tools):
        frozen['messages'] = [text for text in frozen.get('messages', [])
                              if not text.startswith('Available subagents for this request')]
        frozen['messages'].extend(self.get_request_messages(context, tools))

    def __init__(self):
        self.engine = SubagentEngine()
        self.tools = [
            DelegateToSubagentTool(self.engine),
            DelegateToSubagentsTool(self.engine),
            ListSubagentsTool(self.engine),
            CreateOrUpdateSubagentTool(self.engine),
        ]

    def set_context(self, registry):
        self.engine.set_registry(registry)

    def get_tools(self):
        return self.tools

    def get_system_prompt_addon(self):
        return ""

    def get_request_messages(self, context, tools):
        if context.subagent_id or "delegate_to_subagents" not in tools:
            return []
        items = [
            {
                "id": key,
                "name": cfg["name"],
                "description": cfg.get("description", ""),
                "direct_output": cfg.get("direct_output", False),
            }
            for key, cfg in delegation_catalog().items()
        ]
        return [
            "Available subagents for this request (historical catalogs are not current permissions):\n"
            + json.dumps(items, ensure_ascii=False)
            + "\nUse delegate_to_subagents for one task (1-item tasks array) or multiple concurrent tasks (2-32 items). Use direct_output only when the subagent result can be shown without rewriting."
        ]
