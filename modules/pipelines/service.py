"""LLM/tool adapter and durable chat continuation for PipelineRunner."""
from __future__ import annotations
from core.logger import log_operation
import asyncio
import json
import re
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from core.context import bind_execution_context
from core.paths import APP_PATHS
from core.system_prompt import build_core_system_prompt
from .packages import PackageStore
from .storage import load_pipeline_script, save_layout, load_layout, save_pipeline_script
from .runtime import PipelineRunner, HumanBroker, PipelineShutdown, RunState

_frame = ContextVar('pipeline_frame', default=None)

class PipelineService:
    def __init__(self, registry, client):
        self.registry, self.client = registry, client
        self.store = PackageStore()
        self.root = APP_PATHS.runtime / 'pipelines'
        self.root.mkdir(parents=True, exist_ok=True)
        # Older versions left a tiny terminal status file for every chat run.
        # Keep pending recovery records and all execution checkpoints intact.
        for path in self.root.glob('*_request.json'):
            try:
                if path.stat().st_size > 256:continue
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict) and set(data) == {'status'} and data['status'] in {'completed', 'cancelled'}:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        self.broker = HumanBroker(self.root / 'human')

    @contextmanager
    def frame(self, messages, context, tools, emit, call_id='user'):
        frame = {'messages': list(messages), 'context': context, 'tools': tools,
            'enabled': set(self.registry.loaded_modules) if context.allowed_modules is None else set(context.allowed_modules) & set(self.registry.loaded_modules),
            'emit': emit, 'call_id': call_id, 'depth': 0}
        token = _frame.set(frame)
        try:
            yield frame
        finally:
            _frame.reset(token)

    def catalog(self, allowed=None):
        enabled = set(self.registry.loaded_modules)
        if allowed is not None:
            enabled &= set(allowed)
        return self.store.catalog(enabled, self.registry.tools)

    @log_operation('pipelines', 'PIPELINE')
    async def run(self, module, pid, inputs=None, *, definition_override=None, resolve_override=None):
        frame = _frame.get()
        if not frame:
            raise RuntimeError('Pipeline требует контекст запуска агента')
        if module not in frame['enabled']:
            raise ValueError(f'Модуль {module} отключён или недоступен в этой сессии')
        if frame['depth'] > 16:
            raise ValueError('Превышена глубина вызовов pipeline')
        entry = self.store.read(module)['pipelines'][pid]
        definition = definition_override if definition_override is not None else load_pipeline_script(self.store.resolve(module, pid) / 'pipeline.py')
        context = frame['context']
        call_id = re.sub('[^a-zA-Z0-9_]', '_', frame['call_id'])
        checkpoint = self.root / f'{context.run_id}_{call_id}_{module}_{pid}.json'
        definition_path = checkpoint.with_name(checkpoint.stem + '_definition.py')
        if checkpoint.exists() and definition_path.exists():
            definition = load_pipeline_script(definition_path)
        else:
            save_pipeline_script(definition_path, definition)
        async def invoke(name, args):
            with bind_execution_context(context):
                return await self.registry.execute_tool(name, args, context=context, tools=frame['tools'])
        runner = PipelineRunner(definition, tools=frame['tools'], enabled=frame['enabled'], model=self.model,
            invoke_tool=invoke, broker=self.broker, emit=frame['emit'], checkpoint=checkpoint, execution_context=context,
            resolve_call=resolve_override or (lambda ref: self.store.internal_definition(module, ref)), depth=frame['depth'])
        token = _frame.set(dict(frame, depth=frame['depth'] + 1))
        try:
            return await runner.run(inputs, context_text='\n'.join(str(m.content) for m in frame['messages'] if isinstance(m, HumanMessage)), resume=checkpoint.exists(), state=RunState(run_id=context.run_id))
        finally:
            _frame.reset(token)

    async def model(self, schema, instruction, fixed, runner):
        frame = _frame.get()
        context = frame['context']
        from core.reasoning_control import interruptible, ensure_work_allowed
        ensure_work_allowed(context)
        async def call_model(*args,**kwargs):
            return await interruptible(self.client.chat_completion(*args,**kwargs),context)
        system = build_core_system_prompt(context.workspace_root)
        system += '\n' + context.project_system_prompt
        if context.knowledge_base_snapshot_text:
            system += '\nKnowledge bases at session start:\n' + context.knowledge_base_snapshot_text
        # Never send an unfinished outer tool_call to another LLM request.
        history = [m for m in frame['messages'] if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)]
        request_tools = dict(frame['tools'])
        target_module = fixed.get('module') if not schema and not fixed.get('autonomous_once') else None
        if target_module:
            for name, tool in list(request_tools.items()):
                scope = getattr(tool, 'scope_to_module', None)
                if callable(scope):
                    scoped = scope(target_module)
                    if scoped is None:
                        request_tools.pop(name)
                    else:
                        request_tools[name] = scoped
        if not schema:
            guidance = self.registry.get_combined_prompt_addons(context.allowed_modules)
            requests = ([guidance] if guidance else []) + self.registry.get_request_messages(context, request_tools)
            history.extend(HumanMessage(content=text) for text in requests if text)
        if not context.include_thoughts_in_context:
            history = [AIMessage(content=re.sub(r'<think>[\s\S]*?</think>', '', str(m.content)).strip()) if isinstance(m, AIMessage) else m for m in history]
        payload = {'pipeline': runner.pipeline.name, 'node': runner.state.current_node, 'instruction': instruction,
            'fixed_values': fixed, 'previous_result': runner.state.last_result, 'previous_error': runner.state.last_error,
            'execution_history': runner.context_projection()}
        if schema:
            task = ('Generate input values for this pipeline node to carry out the latest user request. '
                    'This is argument generation, not tool execution or a final assistant response. '
                    'Return a JSON object containing every required key from the schema. '
                    'Use the instruction, conversation, execution history and fixed_values as context. '
                    'Do not repeat fixed_values or invent a wrapper such as arguments.\n')
            task += json.dumps({'schema': schema, 'context': payload}, ensure_ascii=False)
        else:
            task = 'Complete the module step using only the provided tools.\n' + json.dumps(payload, ensure_ascii=False)
        if schema and fixed.get('decision_kind') == 'subagent':
            # The saved editor contract has no subagent selector; the node chooses one
            # through the configured delegation tool, without access to topology.
            configs_tool = frame['tools'].get('list_subagents')
            catalog = await self.registry.execute_tool('list_subagents',{},context=context,tools=frame['tools']) if configs_tool else ''
            resp = await call_model([SystemMessage(content=system), *history,
                HumanMessage(content='Choose a suitable subagent for this decision. Return JSON {"agent_name": "..."}.\n' + str(catalog) + '\n' + task)],
                response_format={'type': 'json_object'}, session_id=context.session_id)
            agent_name = self.parse_json(resp['choices'][0]['message'].get('content', ''))['agent_name']
            delegate = frame['tools'].get('delegate_to_subagents')
            if delegate is None:
                raise ValueError('Инструмент субагента недоступен')
            with bind_execution_context(context):
                text = await self.registry.execute_tool('delegate_to_subagents',{'tasks':[{'agent_name':agent_name,'task':task}]},context=context,tools=frame['tools'])
            return self.parse_json(text)
        messages = [SystemMessage(content=system), *history, HumanMessage(content=task)]
        if not schema and fixed.get('autonomous_once'):
            messages.append(HumanMessage(content='Choose at most one suitable tool and call it once. '
                'If no tool is needed, answer with text. Tool chains and parallel calls are forbidden.'))
            available = {name: tool for name, tool in frame['tools'].items() if getattr(tool, 'model_visible', True)}
            response = await call_model(messages,
                tools=self.registry.get_openai_tool_schemas(available), session_id=context.session_id)
            choice = response['choices'][0]['message']
            from .history import append as record_history
            record_history(runner, {'kind': 'model', 'message': choice})
            runner.save()
            reasoning = choice.get('reasoning_content') or choice.get('reasoning') or ''
            if reasoning:
                await frame['emit']({'type': 'thought', 'delta': reasoning})
            calls = choice.get('tool_calls') or []
            if not calls:
                return re.sub(r'<think>[\s\S]*?</think>', '', choice.get('content') or '').strip()
            if len(calls) != 1:
                raise ValueError('Нода решения допускает только один вызов инструмента')
            call = calls[0]
            name = call['function']['name']
            if name not in available:
                raise ValueError('Инструмент недоступен в проекте: ' + name)
            arguments = call['function'].get('arguments') or '{}'
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            from jsonschema import validate
            validate(arguments, available[name].args_schema)
            call_id = call.get('id') or f'{runner.state.run_id}:{runner.state.steps}:decision'
            await frame['emit']({'type': 'tool_start', 'name': name, 'id': call_id, 'args': arguments})
            token = _frame.set(dict(frame, call_id=call_id))
            try:
                result = await runner.invoke_tool(name, arguments)
            finally:
                _frame.reset(token)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            await frame['emit']({'type': 'tool_result', 'name': name, 'id': call_id, 'output': text})
            return result
        if schema:
            from jsonschema import validate as validate_json, ValidationError
            requested = dict(schema, additionalProperties=False)
            last_error = ''
            for attempt in range(3):
                response = await call_model(messages, response_format={
                    'type': 'json_schema', 'json_schema': {
                        'name': 'pipeline_inputs', 'strict': False, 'schema': requested}},
                    session_id=context.session_id)
                choice = response['choices'][0]['message']
                from .history import append as record_history
                record_history(runner, {'kind': 'model', 'message': choice})
                runner.save()
                reasoning = choice.get('reasoning_content') or choice.get('reasoning') or ''
                if reasoning:
                    await frame['emit']({'type': 'thought', 'delta': reasoning})
                content = re.sub(r'<think>[\s\S]*?</think>', '', choice.get('content') or '').strip()
                try:
                    generated = self.parse_json(content)
                    validate_json(generated, requested)
                    return generated
                except (ValueError, ValidationError) as error:
                    last_error = (error.message if isinstance(error, ValidationError) else str(error))[:500]
                    if attempt < 2:
                        messages.append(AIMessage(content=content or '{}'))
                        messages.append(HumanMessage(content='Your JSON did not satisfy the input schema: '
                            + last_error + '. Return the complete corrected JSON object, including every required key.'))
            raise ValueError('Pipeline input generation failed after 3 attempts: ' + last_error)
        module = fixed.get('module') if not schema else None
        names = ['remember_this', 'forget_this', 'search_memory', 'list_memories', 'search_temp_memory', 'list_knowledge_bases', 'search_knowledge_base'] if module == 'memory' else self.registry.module_tools_map.get(module, [])
        available = {n: frame['tools'][n] for n in names if n in frame['tools']}
        for name, tool in request_tools.items():
            if module in getattr(tool, 'owner_modules', ()):
                available[name] = tool
        # Pipeline tools can be nested, but only with an explicit depth guard.
        for step in range(20):
            response = await call_model(messages, tools=self.registry.get_openai_tool_schemas(available) if not schema else None,
                response_format={'type': 'json_object'} if schema else None, session_id=context.session_id)
            choice = response['choices'][0]['message']
            from .history import append as record_history
            record_history(runner, {'kind': 'model', 'message': choice})
            runner.save()
            content = re.sub(r'<think>[\s\S]*?</think>', '', choice.get('content') or '').strip()
            reasoning = choice.get('reasoning_content') or choice.get('reasoning') or ''
            if reasoning:
                await frame['emit']({'type': 'thought', 'delta': reasoning})
            calls = choice.get('tool_calls') or []
            if not calls:
                return self.parse_json(content) if schema else content
            parsed = [{'name': c['function']['name'], 'args': json.loads(c['function']['arguments']), 'id': c['id'], 'type': 'tool_call'} for c in calls]
            messages.append(AIMessage(content=content, tool_calls=parsed,
                                      additional_kwargs={'provider_data': choice.get('_provider_data') or {}}))
            for call in parsed:
                if call['name'] not in available:
                    raise ValueError('Модель выбрала инструмент вне текущего модуля: ' + call['name'])
                await frame['emit']({'type': 'tool_start', 'name': call['name'], 'id': call['id'], 'args': call['args']})
                token = _frame.set(dict(frame, call_id=call['id']))
                try:
                    with bind_execution_context(context):
                        result = await self.registry.execute_tool(call['name'],call['args'],context=context,tools=available)
                finally:
                    _frame.reset(token)
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                await frame['emit']({'type': 'tool_result', 'name': call['name'], 'id': call['id'], 'output': str(text),
                                     **({'document_images':result.model_images} if getattr(result,'model_images',None) else {})})
                messages.append(ToolMessage(content=str(text), tool_call_id=call['id'], additional_kwargs=(
                    {'document_images':result.model_images} if getattr(result,'model_images',None) else {})))
                record_history(runner, {'kind': 'tool', 'name': call['name'], 'args': call['args'], 'result': text})
                runner.save()
        raise RuntimeError('Модуль не завершил шаг за 20 обращений к модели')

    @staticmethod
    def parse_json(text):
        text = re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
        # Strip fenced JSON and the existing subagent tool's fixed prose prefix.
        decoder = json.JSONDecoder()
        for pos, char in enumerate(text):
            if char == '{':
                try:
                    value, end = decoder.raw_decode(text[pos:])
                    if isinstance(value, dict):
                        return value
                except ValueError:
                    pass
        raise ValueError('Модель должна вернуть JSON-объект')
