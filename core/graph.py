import asyncio
from core.resume_request import (is_native_resume, can_continue_partial, request_snapshot,
    restored_request, partial_message, visible_prefix, complete_provider_data)
import json
import re
import time
from contextlib import nullcontext
from typing import Annotated, Any, AsyncGenerator, Dict, List, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, message_to_dict, messages_from_dict
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from core.context import ExecutionContext, bind_execution_context
from core.steering import steering_scope, inbox_for, has_updates, steerable_stream, SteeringRequested
from core.chat_references import (CHAT_ID, REPLY, REQUEST_MEMORY, REQUEST_GUIDANCE, service_only_output, model_assistant_parts,
                                  model_message_metadata, model_content as reply_model_content)
from core.llm_client import LlmClient
from core.logger import core_logger
from core.memory.manager import MemoryManager
from core.embeddings import EmbeddingError, MEMORY_QUERY_TASK
from core.registry import BaseTool, ModuleRegistry
from core.memory.module import CoreMemoryModule
from core.system_prompt import build_core_system_prompt
from core.system_tools import ReportTaskStatusesTool
from core.reasoning_control import (finish_requested, finish_owner, finalizable_stream,
    FinalizationRequested, BLOCKED_TOOL, interruptible, contains_native_tools)


class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    steps_count: int
    max_steps: int
    context: ExecutionContext
    event_queue: asyncio.Queue
    tool_snapshot: Dict[str, BaseTool]
    prompt_addons: str
    module_sections: List[Dict[str, Any]]
    retry_empty_response: bool
    empty_response_retries: int
    journal: Any
    resume: Any
    tool_source_calls: dict
    log_timers: dict
    direct_output_done: bool
    compressor: Any
    final_answer: bool


class _ThinkStreamFilter:
    """Разделяет content и <think> даже когда теги разбиты между чанками."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self):
        self.buffer = ""
        self.in_thought = False

    def feed(self, chunk: str) -> List[Dict[str, str]]:
        self.buffer += chunk
        events: List[Dict[str, str]] = []
        while self.buffer:
            marker = self.CLOSE if self.in_thought else self.OPEN
            marker_pos = self.buffer.find(marker)
            if marker_pos >= 0:
                prefix = self.buffer[:marker_pos]
                if prefix:
                    events.append({"type": "thought" if self.in_thought else "content", "delta": prefix})
                self.buffer = self.buffer[marker_pos + len(marker):]
                self.in_thought = not self.in_thought
                continue

            keep = max((length for length in range(1, len(marker))
                        if self.buffer.endswith(marker[:length])), default=0)
            emit_len = len(self.buffer) - keep
            if emit_len <= 0:
                break
            events.append({
                "type": "thought" if self.in_thought else "content",
                "delta": self.buffer[:emit_len],
            })
            self.buffer = self.buffer[emit_len:]
        return events

    def flush(self) -> List[Dict[str, str]]:
        if not self.buffer:
            return []
        event = {"type": "thought" if self.in_thought else "content", "delta": self.buffer}
        self.buffer = ""
        return [event]


class AgentCore:
    """Единый граф исполнения для потоковых и обычных запросов."""

    def __init__(self, client: LlmClient, memory: MemoryManager, registry: ModuleRegistry):
        self.client = client
        self.memory = memory
        self.registry = registry
        registry.client = client
        from core.timeout_settings import load_settings
        self.max_steps = load_settings()['max_steps']

        self.registry.register_core_tool(ReportTaskStatusesTool())
        from core.context_tools import ContextHistoryTool
        self.registry.register_core_tool(ContextHistoryTool())
        self.registry.register_core_tool(ContextHistoryTool(read=True))
        self.memory_module = CoreMemoryModule(self.memory, self.client)
        for tool in self.memory_module.get_tools():
            self.registry.register_core_tool(tool)
        from modules.pipelines.service import PipelineService
        self.pipeline_service = PipelineService(registry, client)
        registry.pipeline_service = self.pipeline_service
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("agent", self._agent_node)
        builder.add_node("tools", self._tool_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent",
            self._should_continue,
            {"continue": "tools", "retry": "agent", "end": END},
        )
        builder.add_edge("tools", "agent")
        return builder.compile()

    async def _emit(self, state: AgentState, event: Dict[str, Any]) -> None:
        timers = state.setdefault('log_timers', {})
        kind = event.get('type')
        identity = event.get('id', '')
        context = state.get('context')
        if kind in {'tool_start', 'tool_result'}:
            category = 'pipelines' if ':' in identity and not identity.endswith(':tool') else 'tools'
            if kind == 'tool_start':
                timers[identity] = time.perf_counter()
                core_logger.event(category, 'TOOL_START', f"Started: {event.get('name')}", context=context,
                    details={'id': identity, 'args': event.get('args', {})})
            else:
                elapsed = time.perf_counter()-timers.pop(identity, time.perf_counter())
                output = str(event.get('output', ''))
                failed = output.startswith(('System Error', 'Error:', 'System: Error')) or bool(event.get('error'))
                core_logger.event(category, 'TOOL_ERROR' if failed else 'TOOL_DONE',
                    f"{event.get('name')} · {max(0, elapsed):.3f} s · {'error' if failed else 'completed'}", context=context,
                    level='WARN' if failed else 'INFO', details={'id': identity, 'result': output[:16000], 'truncated':len(output)>16000})
        elif kind == '_model_start':
            core_logger.event('context', 'CONTEXT_READY', f"Context: {len(event.get('messages', []))} messages; step {event.get('steps_count', 0)+1}", context=context,
                details={'thoughts_in_context':getattr(context, 'include_thoughts_in_context', True),
                    'tools':list(state.get('tool_snapshot', {})), 'modules':[s.get('name') for s in state.get('module_sections', [])],
                    'attachments':sum(len(getattr(m, 'additional_kwargs', {}).get('attachments', [])) for m in state.get('messages', []))})
        elif kind == 'pipeline_pause':
            core_logger.event('pipelines', 'PIPELINE_PAUSE', str(event.get('message') or 'Waiting for user input'), context=context,
                details={k:v for k,v in event.items() if k not in {'type', 'messages'}})
        calls = state.setdefault('tool_source_calls', {})
        if event.get('type') == 'tool_start':
            calls[event.get('id', '')] = dict(event.get('args') or {})
        elif event.get('type') == 'tool_result':
            args = calls.pop(event.get('id', ''), {})
            if 'sources' not in event and state.get('context'):
                tool = state.get('tool_snapshot', {}).get(event.get('name'))
                if tool is not None:
                    event = {**event, 'sources':tool.get_sources(args, event.get('output'), state['context'].workspace_root)}
        journal = state.get('journal')
        if journal is not None:
            journal.consume(event)
        plugins = getattr(self.registry, 'plugins', None)
        if plugins is not None and kind not in ('content', 'thought'):
            await plugins.emit_async('core.run_events', {'event': dict(event), 'context': context})
        await state["event_queue"].put(event)

    def _extract_fallback_tool_calls(self, content: str, tools: Dict[str, BaseTool]) -> List[Dict[str, Any]]:
        tools = {name: tool for name, tool in tools.items() if tool.model_visible}
        if not self.registry.config.get("allow_text_tool_fallback", False) or not content:
            return []
        match = re.fullmatch(r"\s*```(?:json)?\s*(\{[\s\S]*\})\s*```\s*", content)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
            name = data.get("name") or data.get("tool") or data.get("tool_name")
            args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
            if name and name in tools and isinstance(args, dict):
                core_logger.warn("FALLBACK_PARSER", f"Captured an explicit JSON tool call: {name}")
                return [{"name": name, "args": args, "id": f"call_fallback_{int(time.time() * 1000)}", "type": "tool_call"}]
        except Exception:
            pass
        return []

    def _processed_history(self, messages: List[BaseMessage], include_thoughts: bool, resume_run_id: str = '', *, include_replied_thoughts: bool = False) -> List[BaseMessage]:
        from core.chat_references import replied_thought_target, assistant_message_ids
        reply_target = replied_thought_target(messages, include_replied_thoughts and not include_thoughts)
        processed: List[BaseMessage] = []
        for message in messages:
            if isinstance(message, AIMessage):
                trace = (message.additional_kwargs.get('run_state') or {}).get('steered_protocol')
                if trace and (message.additional_kwargs.get('run_state') or {}).get('status') == 'completed':
                    expanded = messages_from_dict(trace)
                    updates = {segment.get('message', {}).get('data', {}).get('additional_kwargs', {}).get('chat_id')
                               for segment in message.additional_kwargs.get('run_segments', [])
                               if segment.get('type') == 'user_update'}
                    for original in expanded:
                        if isinstance(original, HumanMessage) and original.additional_kwargs.get('chat_id') and original.additional_kwargs['chat_id'] in updates:
                            original.additional_kwargs['steering_update'] = True
                    saved_state = message.additional_kwargs.get('run_state') or {}
                    current_final = re.sub(r'<think>[\s\S]*?</think>', '', str(message.content or '')).strip()
                    if (expanded and isinstance(expanded[-1], AIMessage) and not expanded[-1].tool_calls
                            and current_final != str(saved_state.get('projection_final_text', current_final)).strip()):
                        thinking = ''.join(re.findall(r'<think>[\s\S]*?</think>', str(expanded[-1].content or '')))
                        expanded[-1].content = thinking + ('\n' if thinking else '') + current_final
                        expanded[-1].additional_kwargs.pop('provider_data', None)
                    processed.extend(self._processed_history(expanded, include_thoughts, resume_run_id,
                        include_replied_thoughts=include_replied_thoughts))
                    continue
                content = message.content or ""
                model_kwargs = dict(message.additional_kwargs or {})
                from core.interrupted_context import RETAINED_NOTES
                include = (include_thoughts or bool(resume_run_id and model_kwargs.get('execution_run_id') == resume_run_id)
                           or bool(reply_target and reply_target in assistant_message_ids(message))
                           or bool(model_kwargs.get(RETAINED_NOTES)))
                model_kwargs['_include_model_thoughts'] = include
                if not include and 'model_segments' not in (model_kwargs.get('run_state') or {}):
                    content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
                processed.append(AIMessage(content=content, tool_calls=message.tool_calls,
                                           additional_kwargs=model_kwargs))
            elif isinstance(message, HumanMessage):
                content = message.content or ""
                tags = list(message.additional_kwargs.get("tags", []) or [])

                # One-time compatibility normalization for older saved sessions. Historical
                # tags are never hidden anymore: legacy suffixes/comments are converted to a
                # stable, explicitly request-scoped instruction on every model call. Because
                # the normalization is deterministic, the provider sees the same prefix on
                # subsequent turns and can keep its prompt cache.
                legacy_comment = re.search(r"(?:\n\n)?<!--tags:(.*?)-->\s*$", content)
                if not tags and legacy_comment:
                    tags = [tag.strip() for tag in legacy_comment.group(1).split("|") if tag.strip()]
                if tags:
                    instruction = f"Instructions for this request only: {', '.join(str(tag).strip() for tag in tags if str(tag).strip())}"
                    if not content.rstrip().endswith(instruction):
                        base = content.rstrip()
                        if legacy_comment:
                            base = base[:legacy_comment.start()].rstrip()
                        else:
                            old_instruction = "Конкретно только для этого запроса: " + ', '.join(str(tag).strip() for tag in tags if str(tag).strip())
                            if base.endswith(old_instruction):
                                base = base[:-len(old_instruction)].rstrip()
                            legacy_words = " ".join(
                                str(tag).strip().lower() + ("" if str(tag).strip().endswith(".") else ".")
                                for tag in tags if str(tag).strip()
                            )
                            if legacy_words and base.endswith(legacy_words):
                                base = base[:-len(legacy_words)].rstrip()
                        content = f"{base}\n\n{instruction}"

                model_kwargs = dict(message.additional_kwargs or {})
                # Application-only task-check metadata must remain in the saved chat
                # for retries/dirty tracking, but it is not part of the model input.
                model_kwargs.pop("task_status_check", None)
                model_kwargs.pop("task_status_task_ids", None)
                processed.append(HumanMessage(content=content, additional_kwargs=model_kwargs))
            else:
                processed.append(message)
        return processed

    async def _build_system_prompt(self, state: AgentState) -> str:
        context = state["context"]
        prompt = build_core_system_prompt(context.workspace_root)
        if state["steps_count"] == 0 and not finish_requested(context):
            users = [message for message in state["messages"] if isinstance(message, HumanMessage)
                     and not message.additional_kwargs.get('module_guidance')]
            # Attachment-only turns have no useful textual query for automatic
            # background memory retrieval. Explicit memory tools still work.
            memory_query_text = str(users[-1].content or "").strip() if users else ""
            if users:
                last_kwargs = dict(getattr(users[-1], "additional_kwargs", {}) or {})
                attachments = list(last_kwargs.get("attachments") or [])
                tags = [str(tag).strip() for tag in (last_kwargs.get("tags") or []) if str(tag).strip()]
                if attachments and tags:
                    tag_only = f"Instructions for this request only: {', '.join(tags)}"
                    if memory_query_text == tag_only:
                        memory_query_text = ""
            # A changing system prefix invalidates the server's KV cache.
            # Freeze retrieval on this user message, at the end of model input.
            cached_memory = (users[-1].additional_kwargs.get(REQUEST_MEMORY) if users else None)
            if memory_query_text and (not isinstance(cached_memory, dict) or cached_memory.get('source_content') != str(users[-1].content or '')):
                users[-1].additional_kwargs[REQUEST_MEMORY] = {'source_content': str(users[-1].content or ''), 'text': ''}
                try:
                    session = self.client.embedding_snapshot()
                    # Validate the stored vector space before inference. If the
                    # user selected a different (or offline) embedding backend,
                    # the UI should report the actual model mismatch first.
                    self.memory.validate_permanent_embedding(session.identity)
                    query_vector = await session.embed(
                        memory_query_text, purpose="query", task=MEMORY_QUERY_TASK
                    )
                    matches = self.memory.search_permanent_memory(
                        query_vector,
                        limit=3,
                        embedding_model=session.identity,
                        embedding_dimension=session.dimension,
                    )
                    core_logger.event('memory', 'MEMORY_RETRIEVAL', f'Background retrieval found {len(matches)} chunks', context=context)
                    if matches:
                        chunks = [f"- {match['text']} (схожесть: {match['score']:.2f})" for match in matches]
                        users[-1].additional_kwargs[REQUEST_MEMORY]['text'] = '\n'.join(chunks)
                    # Фоновая проверка/автоподмешивание памяти не должна показывать
                    # большой error banner. Она только сообщает GUI, что текущая
                    # embedding-конфигурация снова здорова.
                    await self._emit(state, {"type": "embedding_status", "active": False, "message": ""})
                except EmbeddingError as error:
                    core_logger.warn("MEMORY_ERROR", f"Memory retrieval failed: {error}")
                    # Это фоновая попытка автоматически подмешать память в prompt.
                    # Показываем лишь постоянный индикатор возле поля ввода.
                    # Большой banner появится только если реальный tool-вызов
                    # пользователя/модели упрётся в ту же ошибку.
                    await self._emit(
                        state,
                        {"type": "embedding_status", "active": True, "message": str(error)},
                    )
                except Exception as error:
                    core_logger.warn("MEMORY_ERROR", f"Memory retrieval failed: {error}")
        project_prompt = str(getattr(context, "project_system_prompt", "") or "").strip()
        if project_prompt:
            prompt += "\n\n=== PROJECT / SESSION INSTRUCTIONS ===\n" + project_prompt

        kb_snapshot = str(getattr(context, "knowledge_base_snapshot_text", "") or "").strip()
        if kb_snapshot:
            prompt += (
                "\n\n=== KNOWLEDGE BASES AT SESSION START ===\n"
                "This is a fixed snapshot captured when the session began. It is not updated during "
                "the session. If databases/modules may have changed, call 'list_knowledge_bases' for "
                "the current catalog.\n"
                + kb_snapshot
            )
        return prompt

    @staticmethod
    def _hidden_protocol_history(
        source_messages: List[BaseMessage],
        processed_messages: List[BaseMessage],
    ) -> List[Dict[str, Any]]:
        """Return saved-history pieces that reach the model outside normal chat prose."""
        hidden: List[Dict[str, Any]] = []
        for index, processed in enumerate(processed_messages):
            source = source_messages[index] if index < len(source_messages) else None

            parts = model_assistant_parts(processed) if isinstance(processed, AIMessage) else []
            identities = ([('user', processed.additional_kwargs.get(CHAT_ID))]
                          if isinstance(processed, HumanMessage)
                          else [('assistant', identity) for identity, _ in parts])
            for role, identity in identities:
                metadata = model_message_metadata(role, identity)
                if metadata:
                    hidden.append({'type': 'application_message_metadata', 'message_index': index,
                                   'content': metadata['content']})
            if isinstance(processed, HumanMessage) and isinstance(processed.additional_kwargs.get(REPLY), dict):
                reply_kwargs = dict(processed.additional_kwargs)
                reply_kwargs.pop(REQUEST_MEMORY, None)
                header = reply_model_content(processed.model_copy(update={'content': '', 'additional_kwargs': reply_kwargs}))
                if header:
                    hidden.append({'type': 'user_reply_context', 'message_index': index, 'content': header})
            if isinstance(processed, HumanMessage):
                guidance = processed.additional_kwargs.get(REQUEST_GUIDANCE)
                if isinstance(guidance, dict) and guidance.get('messages'):
                    hidden.append({'type': 'user_request_context_addition', 'message_index': index,
                                   'content': '\n\n'.join(guidance['messages'])})
                memory = processed.additional_kwargs.get(REQUEST_MEMORY)
                if isinstance(memory, dict) and memory.get('text'):
                    hidden.append({'type': 'user_request_context_addition', 'message_index': index,
                                   'content': str(memory['text'])})

            if isinstance(processed, HumanMessage) and isinstance(source, HumanMessage):
                source_content = str(source.content or "")
                model_content = str(processed.content or "")
                if model_content != source_content:
                    if model_content.startswith(source_content):
                        addition = model_content[len(source_content):].lstrip("\n")
                        hidden.append({
                            "type": "user_request_context_addition",
                            "message_index": index,
                            "content": addition,
                        })
                    else:
                        hidden.append({
                            "type": "user_request_context_transform",
                            "message_index": index,
                            "model_content": model_content,
                        })

            if isinstance(processed, AIMessage):
                if processed.tool_calls:
                    hidden.append({
                        "type": "assistant_tool_calls",
                        "message_index": index,
                        "tool_calls": list(processed.tool_calls),
                    })
                reasoning_parts = re.findall(
                    r"<think>([\s\S]*?)</think>", '\n'.join(str(body) for _, body in parts)
                )
                if reasoning_parts:
                    hidden.append({
                        "type": "assistant_reasoning_in_context",
                        "message_index": index,
                        "content": "\n\n".join(reasoning_parts),
                    })

            if isinstance(processed, ToolMessage):
                hidden.append({
                    "type": "tool_result",
                    "message_index": index,
                    "tool_call_id": processed.tool_call_id,
                    "content": str(processed.content),
                })
        return hidden

    def _bind_request_guidance(self, messages, context, tools, prompt_addons, restore_notice=""):
        user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if user is None:
            return
        frozen = user.additional_kwargs.get(REQUEST_GUIDANCE)
        if isinstance(frozen, dict) and frozen.get('source_content') == str(user.content or ''):
            self.registry.refresh_request_guidance(frozen, context, tools)
            old_notice = frozen.get('file_restore_notice')
            if restore_notice:
                frozen['messages'] = [text for text in frozen['messages'] if text != old_notice]
                frozen['messages'].append(restore_notice)
                frozen['file_restore_notice'] = restore_notice
            return
        guidance = ([prompt_addons] if prompt_addons else []) + self.registry.get_request_messages(context, tools)
        if restore_notice:
            guidance.append(restore_notice)
        user.additional_kwargs[REQUEST_GUIDANCE] = {'source_content': str(user.content or ''),
                                                  'messages': [text for text in guidance if text]}
        if restore_notice:
            user.additional_kwargs[REQUEST_GUIDANCE]['file_restore_notice'] = restore_notice

    async def preview_hidden_model_context(
        self, messages: List[BaseMessage], context: ExecutionContext
    ) -> Dict[str, Any]:
        """Build the same hidden context used by the first agent model call.

        This intentionally reuses execution_snapshot(), _build_system_prompt(),
        and _processed_history() so the inspector cannot silently drift away from
        the real runtime payload. Tool schemas are represented in the preview only
        by their runtime name and description.
        """
        async with self.registry.execution_snapshot(context.allowed_modules) as (tools, prompt_addons, module_sections):
            request_messages = [message.model_copy(deep=True) if isinstance(message, HumanMessage) else message
                                for message in messages]
            from core.file_history import get_history
            restore_notice = await asyncio.to_thread(get_history().last_restore_notice, context.workspace_root)
            self._bind_request_guidance(request_messages, context, tools, prompt_addons, restore_notice)
            state: AgentState = {
                "messages": request_messages,
                "steps_count": 0,
                "context": context,
                "event_queue": asyncio.Queue(),
                "tool_snapshot": tools,
                "prompt_addons": prompt_addons,
                "module_sections": module_sections,
                "retry_empty_response": False,
                "log_timers": {},
                "empty_response_retries": 0,
            }
            with bind_execution_context(context):
                system_prompt = await self._build_system_prompt(state)
            from core.context_compression import ContextCompressor
            projected = await ContextCompressor(self.client, context).preview(request_messages)
            processed_history = self._processed_history(projected, context.include_thoughts_in_context,
                include_replied_thoughts=context.include_replied_thoughts_in_context)
            module_context = await self.registry.model_context(context, request_messages, tools, preview=True)
            base_system_prompt = system_prompt
            if prompt_addons and system_prompt.endswith(prompt_addons):
                base_system_prompt = system_prompt[:-len(prompt_addons)].rstrip()

            core_tools: List[Dict[str, str]] = []
            for tool_name in self.registry.core_tool_names:
                tool = tools.get(tool_name)
                if tool is None:
                    continue
                core_tools.append({
                    "name": tool_name,
                    "description": str(getattr(tool, "description", "") or ""),
                })

            return {
                "base_system_prompt": base_system_prompt,
                "core_tools": core_tools,
                "module_separator": "=== ИНСТРУКЦИИ МОДУЛЕЙ ===" if prompt_addons else "",
                "module_sections": module_sections,
                "hidden_history": self._hidden_protocol_history(
                    projected, processed_history
                ) + [{'type': 'module_context', 'content': text} for text in module_context] + [{'type':'context_compaction' if m.additional_kwargs.get('context_summary') else 'restored_reply_target',
                      'content': str(m.content)} for m in projected
                     if m.additional_kwargs.get('context_summary') or m.additional_kwargs.get('context_reply_target')],
            }

    async def _agent_node(self, state: AgentState) -> Dict[str, Any]:
        resume = state.get('resume')
        native_mode = is_native_resume(self.client, state['context'], resume)
        native_prefix = native_mode and can_continue_partial(resume)
        if resume and not native_mode and not resume.get('_notes_retained'):
            # Закрепляем весь видимый ход рассуждений обычным текстом. API и
            # шаблоны, отбрасывающие reasoning_content, тоже обязаны его увидеть.
            import hashlib
            from core.interrupted_context import RETAINED_NOTES
            notes = '\n\n'.join(str(segment.get('text') or '') for segment in resume.get('segments', [])
                                if segment.get('type') == 'reasoning')
            if not notes:
                partial = resume.get('partial') or {}
                notes = str(partial.get('reasoning') or '') + '\n'.join(re.findall(
                    r'<think>([\s\S]*?)(?:</think>|$)', str(partial.get('content') or '')))
            new_notes = notes
            for message in reversed(state['messages']):
                saved = message.additional_kwargs.get('interrupted_reasoning_snapshot') or {}
                length = saved.get('length', 0)
                if length and hashlib.sha256(notes[:length].encode()).hexdigest() == saved.get('sha256'):
                    new_notes = notes[length:].lstrip('\n')
                    break
            retained = []
            if new_notes:
                text = 'Saved working notes from the interrupted operation. Continue from this work; do not repeat completed actions.\n\n' + new_notes
                retained.append(AIMessage(content=text, additional_kwargs={
                    'working_notes': True, 'execution_run_id': state['context'].run_id, RETAINED_NOTES: text,
                    'interrupted_reasoning_snapshot': {'length': len(notes), 'sha256': hashlib.sha256(notes.encode()).hexdigest()}}))
            updated = dict(state, messages=[*state['messages'], *retained],
                           resume={**resume, '_notes_retained': True})
            result = await self._agent_node(updated)
            return {**result, 'messages': [*retained, *result['messages']]}
        inbox = inbox_for(state['context'].run_id)
        if inbox and inbox.pending:
            updates = inbox.take()
            from dataclasses import replace
            updated_context = replace(state['context'], full_web_pages=state['context'].full_web_pages
                                      or any(m.additional_kwargs.get('full_web_pages') is True for m in updates))
            if state.get('journal'):
                state['journal'].data['full_web_pages'] = updated_context.full_web_pages
            prior = []
            partial = (state.get('resume') or {}).get('partial') or {}
            if partial and not (state.get('resume') or {}).get('_notes_retained'):
                from core.interrupted_context import RETAINED_NOTES
                saved = str(partial.get('reasoning') or '') + str(partial.get('content') or '')
                if saved:
                    prior.append(AIMessage(content='Saved work before interruption:\n' + saved,
                        additional_kwargs={'working_notes': True, 'execution_run_id': state['context'].run_id,
                                           RETAINED_NOTES: 'Saved work before interruption:\n' + saved}))
            self._bind_request_guidance(updates, updated_context, state['tool_snapshot'], state['prompt_addons'])
            for update in updates:
                await self._emit(state, {'type': 'steering_applied', 'id': update.additional_kwargs['chat_id']})
            updated = dict(state, messages=[*state['messages'], *prior, *updates], context=updated_context, resume=None,
                           retry_empty_response=False, direct_output_done=False)
            result = await self._agent_node(updated)
            return {**result, 'context': updated_context, 'messages': [*prior, *updates, *result['messages']]}
        final_answer=bool(state.get('final_answer') or finish_requested(state['context']))
        restarting_final=bool(final_answer and not native_prefix and (state.get('resume') or {}).get('stage')=='answer')
        if final_answer and state.get('resume') and not native_prefix:
            partial=state['resume'].get('partial') or {}
            saved=str(partial.get('reasoning') or '')+str(partial.get('content') or '')
            state['resume']=None
            if saved:
                notes=AIMessage(content='Saved working notes from the interrupted final answer:\n'+saved,
                    additional_kwargs={'execution_run_id':state['context'].run_id,'working_notes':True})
                state=dict(state,messages=[*state['messages'],notes],final_answer=True,
                           restarting_final=restarting_final)
                result=await self._agent_node(state)
                return {**result,'messages':[notes,*result['messages']]}
        restarting_final|=bool(state.get('restarting_final'))
        if final_answer and getattr(state['context'],'parent_run_id',''):
            # The parent will summarize its completed child operations. Do not
            # start another child model pass after the parent requested finish.
            known='\n\n'.join(str(m.content) for m in state['messages'] if isinstance(m,ToolMessage))[-64000:]
            answer=AIMessage(content='The subagent was stopped at the user\'s request. No new actions were performed.'+
                ('\n\nKnown tool results:\n'+known if known else ''),
                additional_kwargs={'final_answer':True,'execution_run_id':state['context'].run_id})
            await self._emit(state,{'type':'_model_complete','messages':[message_to_dict(m) for m in [*state['messages'],answer]]})
            return {'messages':[answer],'steps_count':state['steps_count'],'retry_empty_response':False}
        if final_answer:
            control=finish_owner(state['context'])
            if control is not None:control.final_answer_started=True
            if not native_prefix:
                state['resume']=None
                await self._emit(state,{'type':'content_interim'})
        if state.get('direct_output_done') and not final_answer:
            await self._emit(state, {'type':'direct_output'})
            return {'messages': [AIMessage(content='', additional_kwargs={'direct_output': True})],
                    'steps_count': state['steps_count'], 'retry_empty_response': False}
        step = state["steps_count"] + 1
        context = state["context"]
        core_logger.info("GRAPH_NODE", f"Run {context.run_id}: step #{step}")
        frozen_request = (restored_request(self.client, resume, context)
                          if native_mode and (native_prefix or resume.get('model_inflight')) else None)
        system_prompt = frozen_request[0][0].content if frozen_request else await self._build_system_prompt(state)
        if finish_requested(context) and not final_answer:
            return await self._agent_node(dict(state,final_answer=True))
        runtime_instructions = ([] if final_answer or frozen_request else
            await self.registry.model_context(context, state["messages"], state["tool_snapshot"]))
        if final_answer and not frozen_request:
            runtime_instructions.append('The user requested that this run finish now. Provide exactly one final answer using the work already saved and completed tool results. Do not call tools, delegate, start pipelines, or propose further execution as if it were completed. Clearly distinguish completed work from work not performed. Proceed to the user-facing answer now.')
        if state.get("retry_empty_response") and not final_answer and not frozen_request:
            runtime_instructions.append(
                "\n\n=== RUNTIME COMPLETION GUARD ===\n"
                "The previous model pass ended after internal reasoning without either a user-visible "
                "assistant response or a tool call. Continue the same turn now. Either call the "
                "appropriate tool or provide the user-visible response. Do not stop after reasoning alone."
            )
        schemas = frozen_request[1] if frozen_request else ([] if final_answer else self.registry.get_openai_tool_schemas(state["tool_snapshot"]))
        compressor = None if frozen_request else state.get('compressor')
        try:
            projected = (await compressor.preview(state['messages']) if final_answer and compressor is not None else
                await interruptible(compressor.apply(state['messages'], system_prompt, schemas),context)
                if compressor is not None else state['messages'])
        except FinalizationRequested:
            return await self._agent_node(dict(state,final_answer=True))
        history = frozen_request[0][1:] if frozen_request else self._processed_history(projected, context.include_thoughts_in_context,
                                          context.resume_run_id or context.run_id,
                                          include_replied_thoughts=context.include_replied_thoughts_in_context)
        resume = state.get('resume')
        state['resume'] = None
        partial = (resume or {}).get('partial') or {}
        resume_answer = bool(resume and resume.get('stage') == 'answer')
        if resume_answer and not native_prefix:
            schemas = []
            runtime_instructions.append(
                '\nRuntime: the analysis and actions for this operation are already complete. '
                'Use the saved working notes and tool results to write the final answer in full. '
                'Proceed directly to the user-facing answer; do not restart analysis or repeat tools.'
            )
        continuation = None
        prefix_content = ''
        prefix_reasoning = ''
        if native_prefix:
            prefix_content = str(partial.get('content') or '')
            prefix_reasoning = str(partial.get('reasoning') or '')
            history.append(partial_message(self.client, partial))
            continuation = 'native'
        elif resume and not native_mode:
            if partial:
                raw_content = str(partial.get('content') or '')
                inline_notes = raw_content.split('<think>', 1)[1].split('</think>', 1)[0] if '<think>' in raw_content else ''
                notes = inline_notes + str(partial.get('reasoning') or '')
                # Retained notes already contain this partial; don't append it twice.
                if notes and not resume.get('_notes_retained'):
                    history.append(AIMessage(content='Saved working notes from this interrupted operation:\n' + notes))
            history.append(HumanMessage(content=(
                'Runtime continuation: regenerate the final answer in full using the saved work. Do not repeat tools.'
                if resume_answer else
                'Runtime continuation: continue the interrupted operation using the saved thoughts and tool results. '
                'Do not repeat completed actions or restate already displayed reasoning.')))

        if runtime_instructions:
            instruction = HumanMessage(content='\n\n'.join(runtime_instructions),
                                       additional_kwargs={'runtime_instruction': True})
            index = len(history) - 1 if history and isinstance(history[-1], AIMessage) and history[-1].additional_kwargs.get('continuation_prefix') else len(history)
            history.insert(index, instruction)

        model_base = history[:-1] if native_prefix else history
        model_request = request_snapshot(self.client, [SystemMessage(content=system_prompt), *model_base], schemas, context)
        await self._emit(state, {'type': '_model_start', 'model_request': model_request, 'messages': [message_to_dict(m) for m in state['messages']],
                                 'steps_count': step - 1,
                                 'seed_partial': {'content': prefix_content, 'reasoning': prefix_reasoning,
                                                  'tool_calls': {}, 'provider_deltas': partial.get('provider_deltas', []),
                                                  'parsed_reasoning': bool(partial.get('parsed_reasoning') or partial.get('reasoning'))}})

        if native_prefix and resume_answer:
            await self._emit(state, {'type': 'answer_continue'})

        accumulated_content = prefix_content
        accumulated_reasoning = prefix_reasoning
        visible_response = visible_prefix(partial) if native_prefix else ''
        reasoning_response = prefix_reasoning
        if not reasoning_response and '<think>' in prefix_content:
            reasoning_response = prefix_content.split('<think>', 1)[1].split('</think>', 1)[0]
        accumulated_tool_calls: Dict[int, Dict[str, str]] = {}
        think_filter = _ThinkStreamFilter()
        if continuation and prefix_content and not partial.get('parsed_reasoning'):
            think_filter.in_thought = prefix_content.rfind('<think>') > prefix_content.rfind('</think>')
        restarted_answer = bool(native_prefix and resume_answer)
        provider_data = {}
        visible_phase = bool(visible_response)

        # Ordinary assistant text is streamed immediately. If the same model pass
        # later ends with native tool calls, that text becomes an interim assistant
        # segment instead of being deleted. Text-only fallback tool syntax is still
        # retracted because it is machine control data, not a user-facing message.
        visible_content_streamed = False
        visible_content_retracted = False

        async def emit_visible(delta: str) -> None:
            nonlocal visible_content_streamed, restarted_answer, visible_response, visible_phase
            if not delta:
                return
            visible_content_streamed = True
            visible_phase = True
            visible_response += delta
            if (resume_answer or restarting_final) and not restarted_answer:
                restarted_answer = True
                await self._emit(state, {'type': 'answer_continue' if native_prefix else 'answer_restart'})
            await self._emit(state, {"type": "content", "delta": delta})

        async def retract_visible_if_needed() -> None:
            nonlocal visible_content_retracted
            if visible_content_streamed and not visible_content_retracted:
                visible_content_retracted = True
                await self._emit(state, {"type": "content_reset"})

        stream_kwargs = {'session_id': context.session_id, 'control_reasoning': True}
        if resume_answer and not native_prefix:
            stream_kwargs['answer_only'] = True
        if continuation:
            stream_kwargs['continuation'] = continuation
        steered = False
        try:
            source=self.client.chat_completion_stream(
                [SystemMessage(content=system_prompt)] + history,tools=schemas or None,**stream_kwargs)
            from contextlib import aclosing
            source = source if final_answer else finalizable_stream(source, context)
            async with aclosing(steerable_stream(source, context)) as stream:
                async for delta in stream:
                    if '_reasoning_control' in delta:
                        await self._emit(state, {'type': 'reasoning_control', **delta['_reasoning_control']})
                        continue
                    await self._emit(state, {'type': '_model_delta', 'delta': delta})
                    provider_data = delta.get('_provider_data') or provider_data
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if reasoning:
                        for buffered in think_filter.flush():
                            if buffered['type'] == 'content':
                                await emit_visible(buffered['delta'])
                            else:
                                reasoning_response += buffered['delta']
                                await self._emit(state, buffered)
                        if visible_phase:
                            await self._emit(state, {'type': 'content_interim'})
                            visible_phase = False
                        accumulated_reasoning += reasoning
                        reasoning_response += reasoning
                                        # Parsed reasoning streams omit the closing </think> marker.
                                        # Their content field is already the user-facing answer.
                        think_filter.in_thought = False
                        await self._emit(state, {"type": "thought", "delta": reasoning})

                    content_chunk = delta.get("content") or ""
                    if content_chunk:
                        accumulated_content += content_chunk
                        for event in think_filter.feed(content_chunk):
                            if event["type"] == "thought":
                                if visible_phase:
                                    await self._emit(state, {'type': 'content_interim'})
                                    visible_phase = False
                                reasoning_response += event['delta']
                                await self._emit(state, event)
                            else:
                                await emit_visible(event["delta"])

                    tool_call_deltas = delta.get("tool_calls", [])
                    if tool_call_deltas:
                        for tool_call in tool_call_deltas:
                            idx = tool_call.get("index", 0)
                            entry = accumulated_tool_calls.setdefault(idx, {
                            "id": tool_call.get("id") or f"call_{int(time.time() * 1000)}_{idx}",
                            "name": "",
                            "arguments": "",
                        })
                            function = tool_call.get("function", {})
                            if function.get("name"):
                                entry["name"] = function["name"]
                            if function.get("arguments"):
                                entry["arguments"] += function["arguments"]
        except SteeringRequested:
            steered = True
        except FinalizationRequested:
            pass
        except BaseException:
            # The filter retains a short suffix in case a <think> tag is split.
            # Flush it even on cancellation; otherwise the last characters vanish.
            for event in think_filter.flush():
                await self._emit(state, event)
            raise

        for event in think_filter.flush():
            if event["type"] == "thought":
                reasoning_response += event['delta']
                await self._emit(state, event)
            else:
                await emit_visible(event["delta"])

        if steered:
            from core.interrupted_context import RETAINED_NOTES
            stored = ('<think>' + reasoning_response + '</think>\n' if reasoning_response else '') + visible_response
            retained = 'Saved work before the user updated the current task.\n' + reasoning_response
            if visible_response:
                retained += '\n\nPartial answer before the update:\n' + visible_response
            if accumulated_tool_calls:
                # Намерения сохраняются текстом; старые незапущенные вызовы не исполняются.
                retained += ('\n\nUnexecuted tool requests from the interrupted pass. These actions have NOT run; '
                             'reconsider them after applying the correction:\n'
                             + json.dumps(list(accumulated_tool_calls.values()), ensure_ascii=False))
            notes = AIMessage(content=stored, additional_kwargs={
                'execution_run_id': context.run_id, 'working_notes': True, RETAINED_NOTES: retained})
            await self._emit(state, {'type': '_model_complete',
                'messages': [message_to_dict(m) for m in [*state['messages'], notes]]})
            await self._emit(state, {'type': 'content_interim'})
            return {'messages': [notes], 'steps_count': step, 'resume': None,
                    'retry_empty_response': False, 'empty_response_retries': 0}

        if finish_requested(context) and not final_answer:
            # An interrupted provider block is not an authenticated native
            # message. Preserve its visible working notes as ordinary text.
            stored=('<think>'+reasoning_response+'</think>\n' if reasoning_response else '')+visible_response
            notes=AIMessage(content=stored,additional_kwargs={'execution_run_id':context.run_id,'working_notes':True})
            await self._emit(state,{'type':'_model_complete','messages':[message_to_dict(m) for m in [*state['messages'],notes]]})
            final_state=dict(state,messages=[*state['messages'],notes],steps_count=step,final_answer=True,retry_empty_response=False)
            result=await self._agent_node(final_state)
            return {**result,'messages':[notes,*result['messages']]}

        if native_prefix:
            provider_data = complete_provider_data(provider_data, partial)
        parsed_tool_calls = []
        for tool_call in ([] if final_answer else accumulated_tool_calls.values()):
            try:
                args = json.loads(tool_call["arguments"]) if tool_call["arguments"] else {}
            except Exception as error:
                core_logger.error("TOOL_PARSE_ERROR", f"Invalid arguments for {tool_call['name']}: {error}")
                raise RuntimeError(f"Незавершённые аргументы инструмента {tool_call['name']}; вызов не выполнен.") from error
            if not isinstance(args, dict) or not tool_call['name']:
                raise RuntimeError('Некорректный вызов инструмента; вызов не выполнен.')
            parsed_tool_calls.append({"name": tool_call["name"], "args": args, "id": tool_call["id"], "type": "tool_call"})

        native_tool_calls = bool(parsed_tool_calls)
        if resume_answer and not native_prefix and parsed_tool_calls:
            raise RuntimeError('Модель запросила инструмент при перегенерации итогового ответа; действие не выполнено.')
        if not parsed_tool_calls and not final_answer:
            parsed_tool_calls = self._extract_fallback_tool_calls(accumulated_content, state["tool_snapshot"])
            if parsed_tool_calls:
                # Fallback syntax is encoded in ordinary text and must never remain
                # visible as a conversational assistant segment.
                await retract_visible_if_needed()

        if service_only_output(accumulated_content):
            await retract_visible_if_needed()
            if not parsed_tool_calls:
                raise RuntimeError(
                    'Модель выдала только служебные ссылки вместо ответа. '
                    'Этот результат не сохранён как успешная генерация.'
                )
            accumulated_content = ''

        if native_tool_calls and visible_content_streamed and not visible_content_retracted:
            # The streamed text remains visible, but the UI closes that assistant
            # segment before tool execution and starts a fresh thinking phase.
            await self._emit(state, {"type": "content_interim"})

        # A model pass containing only hidden reasoning is not a valid terminal
        # assistant turn. Some reasoning-capable/OpenAI-compatible models can
        # occasionally stop after saying what they intend to do without emitting
        # either the promised tool call or any user-visible content. Previously
        # that silently satisfied _should_continue() and ended the graph, leaving
        # the UI with a finished thinking card and no answer.
        visible_terminal_text = visible_response.strip()
        if final_answer and not visible_terminal_text:
            raise RuntimeError('Модель не выдала итоговый текстовый ответ. Инструменты не выполнялись; можно продолжить получение итогового ответа.')
        if not parsed_tool_calls and not visible_terminal_text:
            if hasattr(self.client, 'model_connection') and self.client.model_connection().mode == 'api':
                # A second completed cloud pass may be billed. Keep its thoughts
                # in the checkpoint and let the user explicitly continue.
                raise RuntimeError('Модель завершила запрос только мыслями, без ответа или вызова инструмента. Мысли сохранены; можно нажать «Продолжить».')
            retries = int(state.get("empty_response_retries", 0))
            if retries >= 1:
                raise RuntimeError(
                    "Модель дважды завершила шаг только размышлениями: "
                    "не получен ни видимый ответ, ни вызов инструмента."
                )
            core_logger.warn(
                "EMPTY_AGENT_RESPONSE",
                f"Run {context.run_id}: reasoning-only response at step #{step}; retrying the agent step.",
            )
            notes = AIMessage(content='<think>' + reasoning_response + '</think>', additional_kwargs={
                'execution_run_id': context.resume_run_id or context.run_id, 'working_notes': True, 'provider_data': provider_data})
            await self._emit(state, {'type': '_model_complete',
                                    'messages': [message_to_dict(m) for m in [*state['messages'], notes]]})
            return {
                "messages": [notes],
                'resume': None,
                "steps_count": step,
                "retry_empty_response": True,
                "empty_response_retries": retries + 1,
            }

        stored_content = visible_response
        if reasoning_response:
            stored_content = f"<think>{reasoning_response}</think>\n{stored_content}"
        if final_answer and (accumulated_tool_calls or contains_native_tools(provider_data)):provider_data={}
        answer = AIMessage(content=stored_content, tool_calls=parsed_tool_calls,
                           additional_kwargs={'execution_run_id': context.resume_run_id or context.run_id,
                                              'provider_data': provider_data,'final_answer':final_answer})
        await self._emit(state, {'type': '_model_complete',
                                'messages': [message_to_dict(m) for m in [*state['messages'], answer]]})
        return {
            "messages": [answer],
            'resume': None,
            "steps_count": step,
            "retry_empty_response": False,
            "empty_response_retries": 0,
        }

    async def _tool_node(self, state: AgentState) -> Dict[str, Any]:
        from modules.pipelines.storage import save_layout

        last = next(
            m
            for m in reversed(state["messages"])
            if isinstance(m, AIMessage) and m.tool_calls
        )
        resolved = {
            m.tool_call_id for m in state["messages"] if isinstance(m, ToolMessage)
        }
        calls = [call for call in last.tool_calls if call["id"] not in resolved]
        tool_messages, direct_results = [], []
        delegates = {name for name, tool in state["tool_snapshot"].items() if getattr(tool, "parallel_group", "")}

        async def execute(call):
            name, args, call_id = call["name"], call["args"], call["id"]
            if finish_requested(state['context']):
                return ToolMessage(content=BLOCKED_TOOL,tool_call_id=call_id,name=name),False
            journal = state.get("journal")
            if (
                not getattr(state["tool_snapshot"].get(name), "repeat_after_interruption", False)
                and journal
                and state.get("resume")
                and journal.data.get("tools", {}).get(call_id, {}).get("status")
                in {"running", "unknown"}
            ):
                output = ("System: This tool was interrupted after it started. Its outcome is unknown. "
                          "It has NOT been executed again. Inspect the saved outputs, files or running jobs "
                          "before deciding whether any further action is needed; do not assume failure or repeat side effects.")
                await self._emit(state, {'type': 'tool_result', 'id': call_id, 'name': name,
                                         'output': output, 'status': 'unknown'})
                return ToolMessage(content=output, tool_call_id=call_id, name=name), False
            if has_updates(state['context']):
                return ToolMessage(content='System: The user updated the current task before this action started. '
                    'This action was not executed. Reconsider it using the new instructions.',
                    tool_call_id=call_id, name=name), False
            await self._emit(
                state, {"type": "tool_start", "name": name, "args": args, "id": call_id}
            )
            try:
                with self.pipeline_service.frame(
                    state["messages"],
                    state["context"],
                    state["tool_snapshot"],
                    lambda event: self._emit(state, event),
                    call_id=call_id,
                ), self.registry.dispatch_scope(
                    state["context"],
                    state["tool_snapshot"],
                    state["messages"],
                    lambda event: self._emit(state, event),
                    call_id=call_id,
                ):
                    result = await self.registry.execute_tool(
                        name,
                        args,
                        context=state["context"],
                        tools=state["tool_snapshot"],
                    )
            except EmbeddingError as error:
                await self._emit(
                    state, {"type": "embedding_error", "message": str(error)}
                )
                result = (
                    "System: The embedding subsystem failed. The application has already shown "
                    f"the user the technical error. Details: {error}"
                )
            except Exception as error:
                from modules.pipelines.runtime import PipelineShutdown
                from core.reasoning_control import FinalizationRequested
                if isinstance(error, (PipelineShutdown, FinalizationRequested)):
                    raise
                result = f"System: Tool '{name}' failed: {error}"
            await self._emit(
                state,
                {
                    "type": "tool_result",
                    "id": call_id,
                    "name": name,
                    "output": str(result),
                    **({"document_images": result.model_images} if getattr(result, "model_images", None) else {}),
                    **getattr(state["tool_snapshot"].get(name), "result_metadata", {}),
                },
            )
            return ToolMessage(
                content=str(result),
                tool_call_id=call_id,
                name=name,
                additional_kwargs=(
                    {**getattr(state["tool_snapshot"].get(name), "result_metadata", {}),
                     **({"document_images": result.model_images} if getattr(result, "model_images", None) else {}),
                     **({"direct_output":True} if getattr(result, "direct", False) else {})}
                ),
            ), bool(getattr(result, "direct", False))

        index = 0
        while index < len(calls):
            call = calls[index]
            if call["name"] in delegates:
                end = index + 1
                while end < len(calls) and calls[end]["name"] in delegates:
                    end += 1
                jobs = [asyncio.create_task(execute(item)) for item in calls[index:end]]
                try:
                    results = await asyncio.gather(*jobs)
                finally:
                    for job in jobs:
                        if not job.done():
                            job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
                index = end
            else:
                if call["name"].startswith("pipeline_"):
                    save_layout(
                        self.pipeline_service.root
                        / f'{state["context"].run_id}_outer.json',
                        {
                            "messages": [
                                message_to_dict(m)
                                for m in [*state["messages"], *tool_messages]
                            ],
                            "steps_count": state["steps_count"],
                            "status": "pending",
                        },
                    )
                results = [await execute(call)]
                index += 1
            for message, direct in results:
                tool_messages.append(message)
                direct_results.append(direct)
            if call["name"].startswith("pipeline_"):
                save_layout(
                    self.pipeline_service.root
                    / f'{state["context"].run_id}_outer.json',
                    {
                        "messages": [
                            message_to_dict(m)
                            for m in [*state["messages"], *tool_messages]
                        ],
                        "steps_count": state["steps_count"],
                        "status": "pending",
                    },
                )
        return {
            "messages": tool_messages,
            "direct_output_done": bool(direct_results) and all(direct_results),
        }

    def _should_continue(self, state: AgentState) -> str:
        if has_updates(state['context']):
            return 'continue' if getattr(state['messages'][-1], 'tool_calls', None) else 'retry'
        if finish_requested(state['context']):
            last=state['messages'][-1]
            if last.additional_kwargs.get('final_answer'):return 'end'
            return 'continue' if getattr(last,'tool_calls',None) else 'retry'
        limit = state.get('max_steps', self.max_steps)
        if state["steps_count"] >= limit:
            core_logger.warn("GUARD", f"Step limit ({limit}) reached.")
            return "end"
        if state.get("retry_empty_response"):
            return "retry"
        return "continue" if getattr(state["messages"][-1], "tool_calls", None) else "end"

    async def run_events(self, messages: List[BaseMessage], context: ExecutionContext, *, journal=None, resume=None) -> AsyncGenerator[Dict[str, Any], None]:
        from core.file_history import history_run
        from contextlib import aclosing
        completed = None
        async with history_run(context):
            async with aclosing(self._run_events_with_history(messages, context, journal=journal, resume=resume)) as events:
                async for event in events:
                    if event.get('type') == 'done':
                        completed = event
                        break
                    yield event
        if completed is not None:
            yield completed

    async def _run_events_with_history(self, messages, context, *, journal=None, resume=None):
        """Запускает единственный граф и транслирует события его узлов."""
        if context.raw_request:
            from core.raw_request import raw_events
            async for event in raw_events(self.client, messages, context, journal=journal):
                yield event
            return
        started = time.perf_counter()
        from core.context_compression import ContextCompressor
        from core.token_usage import usage_scope
        compressor = ContextCompressor(self.client, context)
        core_logger.event('generation', 'RUN_RESUME' if resume else 'RUN_START',
            'Generation resumed' if resume else 'Generation started', context=context,
            details={'resume_mode':context.resume_mode, 'selected_pipeline':context.selected_pipeline})
        async with self.registry.execution_snapshot(context.allowed_modules, context=context) as (tools, prompt_addons, module_sections):
            messages = list(messages)
            from core.file_history import get_history
            restore_notice = await asyncio.to_thread(get_history().last_restore_notice, context.workspace_root)
            # Freeze guidance on the current user request. It is sent as user
            # content, and replayed unchanged on later turns for prefix caching.
            if not resume or restore_notice:
                self._bind_request_guidance(messages, context, tools, prompt_addons, restore_notice)
            queue: asyncio.Queue = asyncio.Queue()
            from core.timeout_settings import load_settings
            limits = await asyncio.to_thread(load_settings)
            initial_state: AgentState = {
                "max_steps": limits['max_steps'],
                "messages": list(messages),
                "steps_count": 0,
                "context": context,
                "event_queue": queue,
                "tool_snapshot": tools,
                "prompt_addons": prompt_addons,
                "module_sections": module_sections,
                "retry_empty_response": False,
                "empty_response_retries": 0,
                'journal': journal,
                'resume': resume,
                'tool_source_calls': {},
                'compressor': compressor,
            }
            compressor.emit = lambda event: self._emit(initial_state, event)

            connection = None
            if hasattr(self.client, 'model_connection'):
                connection = await asyncio.to_thread(self.client.model_connection, context.model_profile_id) if context.model_profile_id else await asyncio.to_thread(self.client.model_connection)
                if resume and resume.get('model_fingerprint') and resume['model_fingerprint'] != connection.fingerprint:
                    raise ValueError('Профиль прерванного запуска был изменён. Восстановите его URL, модель и протокол либо перегенерируйте запрос.')
                if journal is not None:
                    journal.data['model_fingerprint'] = connection.fingerprint
                    journal.save(force=True)
            async def invoke_graph():
                try:
                    from core.reasoning_control import reasoning_scope
                    with steering_scope(context, journal, lambda event: self._emit(initial_state, event)) as steering, bind_execution_context(context), reasoning_scope(context,lambda:self._emit(initial_state,{'type':'run_finalizing'})) as finish_control, (self.client.bind_connection(connection) if connection else nullcontext()), usage_scope(observer=compressor.observe), self.registry.dispatch_scope(context, tools, messages, lambda event:self._emit(initial_state,event)):
                        if resume and resume.get('finalizing'):
                            finish_control.finalizing=True;finish_control.stopped=True;finish_control.interrupt.set()
                        await self._emit(initial_state,{'type':'reasoning_control',**finish_control.status()})
                        if context.selected_pipeline and finish_requested(context):
                            resolved={m.tool_call_id for m in initial_state['messages'] if isinstance(m,ToolMessage)}
                            if any(isinstance(m,AIMessage) and any(c['id'] not in resolved for c in m.tool_calls) for m in initial_state['messages']):
                                skipped=await self._tool_node(initial_state)
                                initial_state['messages']+=skipped['messages']
                            if resume and not resume.get('model_started'):
                                saved=[{'name':item.get('name'),'output':item.get('output')} for item in
                                       (resume.get('tools') or {}).values() if item.get('status')=='completed']
                                if saved:initial_state['messages'].append(HumanMessage(
                                    content='Saved results of completed pipeline operations:\n'+json.dumps(saved,ensure_ascii=False),
                                    additional_kwargs={'runtime_instruction':True}))
                            answer=await self._agent_node(dict(initial_state,final_answer=True))
                            final_state=dict(initial_state,messages=[*initial_state['messages'],*answer['messages']])
                        elif context.selected_pipeline and (not resume or not resume.get('model_started')):
                            module, pid = context.selected_pipeline.split("/", 1)
                            entry = self.pipeline_service.store.read(module)["pipelines"][pid]
                            if entry.get("mode") != "user":
                                raise ValueError("Выбранный пайплайн недоступен для назначения пользователем")
                            with self.pipeline_service.frame(messages, context, tools, lambda e: self._emit(initial_state, e)):
                                result = await self.pipeline_service.run(module, pid)
                            # The graph determines execution; the final model call may only phrase its result.
                            final_input = dict(initial_state, tool_snapshot={}, messages=[*messages,
                                HumanMessage(content="Give the final answer using the completed pipeline results. Do not start any additional actions.\n" + json.dumps(result, ensure_ascii=False), additional_kwargs={"runtime_instruction": True})])
                            answer = await self._agent_node(final_input)
                            final_state = dict(final_input, messages=[*final_input['messages'], *answer["messages"]])
                        else:
                            from modules.pipelines.storage import load_layout
                            outer = load_layout(self.pipeline_service.root / f'{context.run_id}_outer.json')
                            if outer.get("status") == "pending" and not resume:
                                initial_state["messages"] = messages_from_dict(outer["messages"])
                                initial_state["steps_count"] = outer["steps_count"]
                                continued = await self._tool_node(initial_state)
                                initial_state["messages"] += continued["messages"]
                                initial_state['direct_output_done'] = continued.get('direct_output_done', False)
                            if resume:
                                resolved = {m.tool_call_id for m in initial_state['messages'] if isinstance(m, ToolMessage)}
                                pending = any(isinstance(m, AIMessage) and any(c['id'] not in resolved for c in m.tool_calls)
                                              for m in initial_state['messages'])
                                if pending:
                                    continued = await self._tool_node(initial_state)
                                    initial_state['messages'] += continued['messages']
                                    initial_state['direct_output_done'] = continued.get('direct_output_done', False)
                            final_state = await self.graph.ainvoke(initial_state,
                                config={'recursion_limit':max(25,initial_state['max_steps']*2+5)})
                        while steering.pending:
                            continuation_state = dict(initial_state, **{key: value for key, value in final_state.items()
                                if key not in {'event_queue', 'journal'}})
                            final_state = await self.graph.ainvoke(continuation_state,
                                config={'recursion_limit': max(25, initial_state['max_steps'] * 2 + 5)})
                        steering.accepting = False
                        if not finish_requested(context):await compressor.finish(final_state['messages'])
                    from modules.pipelines.storage import save_layout
                    outer_path = self.pipeline_service.root / f'{context.run_id}_outer.json'
                    if outer_path.exists():
                        save_layout(outer_path, {"status": "completed"})
                    if connection is not None:
                        final_state['model_name'] = connection.model
                    await queue.put({"type": "_graph_complete", "state": final_state})
                except Exception as error:
                    from modules.pipelines.runtime import PipelineShutdown
                    if isinstance(error, PipelineShutdown):
                        await queue.put({"type": "content", "delta": error.message})
                        await queue.put({"type": "_graph_complete", "state": dict(initial_state,
                            messages=[*messages, AIMessage(content=error.message)])})
                    else:
                        await queue.put({"type": "_graph_error", "error": error})

            task = asyncio.create_task(invoke_graph())
            outcome = 'stopped'
            try:
                while True:
                    event = await queue.get()
                    if event["type"] == "_graph_complete":
                        outcome = 'completed'
                        yield {"type": "done", "state": event["state"]}
                        break
                    if event["type"] == "_graph_error":
                        outcome = 'error'
                        core_logger.event('generation', 'RUN_ERROR', str(event['error']), context=context, level='ERROR')
                        raise event["error"]
                    if event['type'].startswith('_model_'):
                        continue
                    yield event
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                from core.child_runs import cancel_child_runs
                await cancel_child_runs(context.run_id)
                core_logger.event('generation', 'RUN_END', f'{outcome} · {time.perf_counter()-started:.3f} s', context=context)
