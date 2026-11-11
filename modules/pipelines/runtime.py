"""Single sequential, resumable execution engine for all pipeline invocation modes."""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from .contracts import *
from .storage import save_layout, load_layout
from .execution import NOTIFICATION, PARALLEL, TRANSFORM, HANDOFF, policy, result_error
from . import history

MISSING = object()

class PipelineShutdown(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

class PipelineValidationError(ValueError):
    pass

def typed(value, kind):
    if kind == 'binary':
        kind = 'boolean'
    if kind == 'boolean':
        if isinstance(value, bool):
            return value
        if value in (1, 'true', 'True'):
            return True
        if value in (0, 'false', 'False'):
            return False
        raise ValueError('Ожидается Boolean')
    if kind == 'integer':
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError('Ожидается Integer')
        return int(value)
    if kind in {'float', 'number'}:
        if isinstance(value, bool):
            raise ValueError('Ожидается Float')
        return float(str(value).replace(',', '.'))
    if kind == 'array':
        if not isinstance(value, list):
            raise ValueError('Ожидается Array')
        return value
    if kind == 'object':
        if not isinstance(value, dict): raise ValueError('Ожидается Object')
        return value
    if kind == 'string':
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return value

async def execute_python(code, inputs, outputs, *, cwd=None, timeout=60):
    from core.process_owner import run_json_worker
    try:
        returncode, stdout, stderr = await run_json_worker(Path(__file__).with_name('python_worker.py'),
            {'code': code, 'inputs': inputs, 'outputs': outputs}, cwd=cwd, timeout=timeout)
        if returncode:
            return {'values': {}, 'error': stderr.decode(errors='replace')[-16000:] or f'Python exited {returncode}', 'stdout': '', 'stderr': ''}
        return json.loads(stdout)
    except asyncio.TimeoutError:
        return {'values': {}, 'error': 'Python execution timed out.', 'stdout': '', 'stderr': ''}
    except (ValueError, OSError) as error:
        return {'values': {}, 'error': str(error), 'stdout': '', 'stderr': ''}

@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    current_node: str = ''
    state: str = 'RUNNING'
    values: dict = field(default_factory=dict)
    node_results: dict = field(default_factory=dict)
    node_inputs: dict = field(default_factory=dict)
    last_result: Any = None
    last_error: Any = None
    iterations: dict = field(default_factory=dict)
    visits: dict = field(default_factory=dict)
    steps: int = 0
    pending: dict = field(default_factory=dict)
    result: Any = None
    incoming_edge: dict = field(default_factory=dict)
    journal: list = field(default_factory=list)
    loop_scopes: dict = field(default_factory=dict)
    parallel: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)

class HumanBroker:
    """Persisted requests and responses. Waiting has no timeout and releases no graph branch."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.waiters = {}

    def pending(self, session_id=None):
        result = []
        for path in self.root.glob('*.json'):
            data = load_layout(path)
            if data.get('status') == 'pending' and (session_id is None or data.get('session_id') == session_id):
                result.append(data)
        return result

    def respond(self, request_id, response):
        if not request_id.isalnum():
            raise ValueError('Неверный ID запроса')
        path = self.root / f'{request_id}.json'
        data = load_layout(path)
        if data.get('status') != 'pending':
            raise ValueError('Этот запрос уже завершён или не существует')
        if not isinstance(response, dict) or not isinstance(response.get('values', {}), dict):
            raise ValueError('Ожидается объект ответа и объект values')
        response = dict(response)
        action = response.get('action', 'continue')
        if action not in data.get('actions', ['continue']):
            raise ValueError('Неизвестное действие')
        response['action'] = action
        values = dict(response.get('values', {}))
        for field in data.get('fields', []):
            name = field['name']
            value = values.get(name)
            if value is not None and (not isinstance(value, str) or value.strip()):
                values[name] = typed(value, field['type'])
        response['values'] = values
        data.update(status='answered', response=response)
        save_layout(path, data)
        from core.logger import core_logger
        core_logger.event('pipelines', 'PIPELINE_RESUME', f'User decision: {action}',
            context={'session_id':data.get('session_id', ''), 'branch':data.get('branch', ''), 'run_id':data.get('run_id', '')},
            details={'request_id':request_id, 'response':response})
        future = self.waiters.get(request_id)
        if future and not future.done():
            future.set_result(response)
        return data

    async def ask(self, payload, emit):
        request_id = payload.get('request_id') or uuid.uuid4().hex
        path = self.root / f'{request_id}.json'
        old = load_layout(path)
        if old.get('status') == 'answered':
            return old['response']
        data = dict(payload, request_id=request_id, status='pending')
        save_layout(path, data)
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = future
        await emit(dict(data, type='pipeline_pause'))
        try:
            return await future
        finally:
            self.waiters.pop(request_id, None)

    def abandon(self, request_id):
        path = self.root / f'{request_id}.json'
        data = load_layout(path)
        if data.get('status') == 'pending':
            data['status'] = 'cancelled'
            save_layout(path, data)

class PipelineRunner:
    def __init__(self, pipeline, *, tools=None, enabled=None, model=None, invoke_tool=None,
                 broker=None, emit=None, checkpoint=None, execution_context=None, resolve_call=None, depth=0):
        self.pipeline = pipeline
        self.nodes = {n.node_id: n for n in pipeline.nodes}
        self.tools = tools or {}
        self.enabled = enabled
        self.model = model
        self.invoke_tool = invoke_tool
        self.broker = broker
        self.emit_callback = emit
        self.checkpoint = Path(checkpoint) if checkpoint else None
        self.execution_context = execution_context
        self.resolve_call = resolve_call
        self.depth = depth
        self.context_text = ''
        self.inputs = {}
        self.state = RunState()
        self.loop_bodies = history.loop_bodies(pipeline)
        self.tool_overrides = {}

    async def emit(self, event):
        if self.emit_callback:
            await self.emit_callback(event)

    def save(self):
        if self.checkpoint:
            save_layout(self.checkpoint, dict(asdict(self.state), context_text=self.context_text, inputs=self.inputs))

    async def ask(self, kind, message, **kwargs):
        if not self.broker:
            raise RuntimeError('Human In Loop недоступен в этом запуске')
        saved = self.state.pending
        payload = saved if saved else dict(kind=kind, message=message, request_id=uuid.uuid4().hex,
            run_id=self.state.run_id, node_id=self.state.current_node,
            session_id=getattr(self.execution_context, 'session_id', None), branch=getattr(self.execution_context, 'branch', 'main'), **kwargs)
        self.state.pending = payload
        self.state.state = 'SUSPENDED'
        self.save()
        from core.reasoning_control import interruptible, FinalizationRequested
        try:response = await interruptible(self.broker.ask(payload,self.emit),self.execution_context)
        except FinalizationRequested:
            self.broker.abandon(payload['request_id']);raise
        self.state.pending = {}
        self.state.state = 'RUNNING'
        self.save()
        return response

    async def fill(self, schema, values, instruction=''):
        missing = [name for name in schema.get('required', []) if name not in values]
        if not missing:
            from jsonschema import validate as validate_json
            validate_json(values, schema)
            return values
        if not self.model:
            raise ValueError('Missing pipeline inputs: ' + ', '.join(missing))
        requested = {'type': 'object', 'properties': {n: schema['properties'][n] for n in missing}, 'required': missing, 'additionalProperties': False}
        for key in ('$defs', 'definitions'):
            if key in schema:
                requested[key] = schema[key]
        generated = await self.model(requested, instruction, values, self)
        result = dict(values)
        for name in missing:
            if name not in generated:
                raise ValueError('Model did not provide pipeline input: ' + name)
            spec = requested['properties'][name]
            result[name] = typed(generated[name], spec.get('type', ''))
            if 'enum' in spec and result[name] not in spec['enum']:
                raise ValueError('Invalid pipeline input: ' + name)
        from jsonschema import validate as validate_json
        validate_json(result, schema)
        return result

    def incoming(self, node, port, seen=None):
        edge = next((e for e in self.pipeline.edges if e.target_id == node.node_id and e.target_port == port), None)
        if not edge:
            return MISSING
        return self.value(edge.source_id, edge.source_port, seen)

    def value(self, nid, port, seen=None):
        node = self.nodes[nid]
        if not is_data(node):
            return self.state.values.get(nid, {}).get(port, MISSING)
        seen = set(seen or ())
        if nid in seen:
            raise ValueError('Цикл Arg: ' + nid)
        seen.add(nid)
        if node.type_id == 'content.mcp_prompt':
            raw = self.incoming(node, 'arg:messages', seen)
            if raw is MISSING:
                raise ValueError('Сообщения MCP ещё не получены')
            if isinstance(raw, str):
                raw = json.loads(raw)
            messages = raw.get('messages', []) if isinstance(raw, dict) else raw
            if not isinstance(messages, list):
                raise ValueError('Ожидался результат MCP get_prompt с messages')
            parts = []
            for message in messages:
                content = message.get('content', {})
                for block in content if isinstance(content, list) else [content]:
                    if block.get('type') != 'text':
                        raise ValueError('Преобразование в Prompt поддерживает только текстовые сообщения MCP')
                    parts.append(f"[{message.get('role', 'user')}]\n{block.get('text', '')}")
            return '\n\n'.join(parts)
        if node.type_id == 'content.prompt':
            parts = [str(node.settings.get('text') or '')]
            edges = sorted((e for e in self.pipeline.edges if e.target_id == nid), key=lambda e: (0 if e.target_port == 'arg:in' else int(e.target_port.rsplit(':', 1)[1])))
            for e in edges:
                v = self.value(e.source_id, e.source_port, seen)
                if v is MISSING:
                    raise ValueError(f'Источник Arg {e.source_id} ещё не исполнялся')
                if v is not None:
                    parts.append(str(v))
            return '\n'.join(x for x in parts if x)
        kind = SCALARS[node.type_id]
        local = node.settings.get('value')
        if kind == 'array':
            result = list(local or [])
            for e in sorted((e for e in self.pipeline.edges if e.target_id == nid), key=lambda e: int(e.target_port.split(':')[1])):
                v = self.value(e.source_id, e.source_port, seen)
                if v is MISSING:
                    raise ValueError(f'Источник Arg {e.source_id} ещё не исполнялся')
                result.append(typed(v, node.settings.get('item_type', 'string')))
            return result
        if local is None or (kind == 'string' and local == ''):
            local = self.incoming(node, 'arg:in', seen)
        if local is MISSING or local is None:
            return MISSING
        return typed(local, kind)

    def dynamic_inputs(self, node, prefix, section='inputs'):
        values = {}
        for x in definitions(node, section):
            v = self.incoming(node, f"arg:{prefix}:in:{x['id']}")
            if v is not MISSING:
                values[x['name']] = typed(v, x['type'])
        return values

    def put_outputs(self, node, prefix, values):
        self.state.values[node.node_id] = {f"arg:{prefix}:out:{x['id']}": typed(values[x['name']], x['type'])
            for x in definitions(node, 'outputs') if x['name'] in values}

    async def run(self, inputs=None, context_text='', resume=False, state=None):
        errors, warnings, _ = validate(self.pipeline, tools=self.tools, enabled=self.enabled,
            resolve_call=self.resolve_call)
        if errors:
            raise PipelineValidationError('\n'.join(errors))
        if self.depth > 16:
            raise PipelineValidationError('Превышена глубина вызовов pipeline')
        if resume and self.checkpoint and self.checkpoint.exists():
            stored = load_layout(self.checkpoint)
            self.state = RunState(**{k: stored[k] for k in RunState.__dataclass_fields__ if k in stored})
            self.context_text = stored.get('context_text', context_text)
            self.inputs = stored.get('inputs', inputs or {})
        else:
            self.state = state or RunState()
            self.context_text = context_text
            self.inputs = dict(inputs or {})
            self.state.current_node = next(n.node_id for n in self.pipeline.nodes if n.type_id == GRAPH_START_TYPE)
        if self.state.state == 'COMPLETED':
            return self.state.result
        try:
            while self.state.current_node:
                from core.reasoning_control import ensure_work_allowed
                ensure_work_allowed(self.execution_context)
                if self.state.steps >= 10000:
                    raise RuntimeError('Превышен лимит 10000 шагов pipeline')
                node = self.nodes[self.state.current_node]
                started = time.perf_counter()
                await self.emit({'type': 'tool_start', 'name': node.type_id, 'id': f'{self.state.run_id}:{self.state.steps}', 'args': {}})
                ensure_work_allowed(self.execution_context)
                history.enter(self, node)
                try:
                    output, result = await self.execute_checked(node)
                except BaseException as exc:
                    from core.reasoning_control import FinalizationRequested
                    await self.emit({'type': 'tool_result', 'name': node.type_id, 'id': f'{self.state.run_id}:{self.state.steps}',
                                     'output': str(exc), 'error': str(exc), 'status': 'stopped' if isinstance(exc, (PipelineShutdown, FinalizationRequested, asyncio.CancelledError)) else 'failed'})
                    raise
                if node.type_id == PYTHON_CODE_TYPE and isinstance(result, dict) and not result.get('error'):
                    outputs = result.get('values', {})
                    self.state.last_result = outputs['result'] if set(outputs) == {'result'} else outputs
                else:
                    self.state.last_result = result
                self.state.node_results[node.node_id] = {'result': result, 'exec': output, 'duration': time.perf_counter() - started,
                    'error': self.state.last_error}
                history.append(self, dict(kind='node', **self.state.node_results[node.node_id]))
                history.leave(self, node, output)
                trace = dict(self.state.node_results[node.node_id])
                if trace.get('error') is None:
                    trace.pop('error', None)
                if node.type_id == PYTHON_CODE_TYPE and isinstance(result, dict) and result.get('error') is None:
                    trace['result'] = {k: v for k, v in result.items() if k != 'error'}
                await self.emit({'type': 'tool_result', 'id': f'{self.state.run_id}:{self.state.steps}', 'name': node.type_id,
                    'output': json.dumps(trace, ensure_ascii=False), **({'error': str(self.state.last_error)} if self.state.last_error else {})})
                self.state.steps += 1
                self.state.visits[node.node_id] = self.state.visits.get(node.node_id, 0) + 1
                if output is None:
                    self.state.result = result
                    self.state.current_node = ''
                else:
                    edge = next((e for e in self.pipeline.edges if e.source_id == node.node_id and e.source_port == output), None)
                    self.state.current_node = edge.target_id if edge else ''
                    self.state.incoming_edge = asdict(edge) if edge else {}
                    if not edge:
                        self.state.result = result
                self.save()
            self.state.state = 'COMPLETED'
            self.save()
            return self.state.result
        except PipelineShutdown:
            self.state.state = 'SHUTDOWN'; self.save(); raise
        except asyncio.CancelledError:
            self.state.state = 'SUSPENDED' if self.state.pending else 'CANCELLED'
            self.save(); raise
        except Exception as error:
            from core.reasoning_control import FinalizationRequested
            if isinstance(error,FinalizationRequested):
                self.state.state='COMPLETED';self.state.current_node='';self.state.pending={}
                self.state.result={'finished_by_user':True,'last_result':self.state.last_result}
                self.save();return self.state.result
            self.state.state = 'FAILED'; self.save(); raise

    def context_projection(self):
        return history.projection(self)

    async def execute_checked(self, node):
        """Normalize tool failures, preserving legacy semantics when no policy was saved."""
        from core.reasoning_control import ensure_work_allowed, FinalizationRequested, interruptible
        config = policy(node.settings)
        attempt_key = f'{node.node_id}:{self.state.steps}'
        attempt = self.state.attempts.get(attempt_key, 0)
        while True:
            ensure_work_allowed(self.execution_context)
            # Private extension markers include guards against replaying an uncertain side effect.
            private = {k:v for k,v in self.state.values.get(node.node_id, {}).items() if k.startswith('_')}
            self.state.values[node.node_id] = private  # Prior visit's public outputs are unavailable.
            # Routers and Loops deliberately inspect the preceding failure.
            if node.type_id not in {GRAPH_LOOP_TYPE, GRAPH_ROUTER_TYPE}:
                self.state.last_error = None
            try:
                output, result = await self.execute(node)
                error = None if node.type_id in {GRAPH_LOOP_TYPE, GRAPH_ROUTER_TYPE, GRAPH_END_TYPE} else self.state.last_error
                if not error or config is None:
                    self.state.attempts.pop(attempt_key, None)
                    return output, result
            except (PipelineShutdown, FinalizationRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                if config is None: raise
                error = str(exc)
                result = {'error': error}
            self.state.last_error = str(error)
            history.append(self, {'kind': 'error', 'attempt': attempt, 'error': str(error)})
            mode = config['mode']
            if mode == 'retry' and attempt < config['retries']:
                attempt += 1
                self.state.attempts[attempt_key] = attempt
                self.save()
                if config['delay']:
                    await interruptible(asyncio.sleep(config['delay']), self.execution_context)
                continue
            if mode == 'retry': mode = config['exhausted']
            self.state.attempts.pop(attempt_key, None)
            # Preserve completed parallel results even when the container fails.
            saved = self.state.values.get(node.node_id, {}) if node.type_id == PARALLEL else {}
            self.state.values[node.node_id] = dict(saved, **{'arg:error': str(error)})
            self.save()
            if mode == 'stop': raise RuntimeError(f'{node.node_id}: {error}')
            return ('exec:error' if mode == 'branch' else 'exec:out'), result

    async def execute(self, node):
        t, s = node.type_id, node.settings
        if t == NOTIFICATION:
            from string import Formatter
            text = str(s.get('message', ''))
            fields = [field for _, field, _, _ in Formatter().parse(text) if field is not None]
            if any(field != 'arg' for field in fields): raise ValueError('Notification supports only {arg}')
            value = self.incoming(node, 'arg:in')
            if fields and value is MISSING: raise ValueError('Notification requires Arg')
            rendered = text.format(arg=json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
            color = str(s.get('color', '#5b8def'))
            import re
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', color): raise ValueError('Invalid notification color')
            await self.emit({'type': 'pipeline_notification', 'id': f'{self.state.run_id}:{node.node_id}:{self.state.steps}',
                             'text': rendered, 'color': color})
            # The panel is UI-only, including its text; it never enters model history.
            return 'exec:out', {'notification_displayed': True}
        if t == TRANSFORM:
            from .transform import apply
            value = self.incoming(node, 'arg:in')
            if value is MISSING:
                if 'value' not in s: raise ValueError('Transform requires Arg')
                value = s['value']
            from core.reasoning_control import interruptible
            result = await interruptible(asyncio.to_thread(apply, value, s.get('operations', []), parse_json_input=bool(s.get('parse_json_input', True))), self.execution_context)
            self.state.values[node.node_id] = {'arg:result': result}
            return 'exec:out', result
        if t == PARALLEL:
            from .parallel import execute_parallel
            return await execute_parallel(self, node)
        if t == GRAPH_START_TYPE:
            self.state.values[node.node_id] = {}
            self.state.last_error = None
            return 'exec:out', {}
        if t in {GRAPH_END_TYPE, GRAPH_SHUTDOWN_TYPE}:
            if t == GRAPH_SHUTDOWN_TYPE:
                raise PipelineShutdown(str(s.get('message') or 'Генерация остановлена пайплайном.'))
            vals = self.dynamic_inputs(node, 'end')
            vals = await self.fill(schema_for(definitions(node, 'inputs')), vals)
            result = vals if definitions(node, 'inputs') else self.state.last_result
            if s.get('message'):
                result = {'result': result, 'message': s['message']}
            return None, result
        if t == PYTHON_CODE_TYPE:
            # Preserve inputs across an error pause, including values generated by the model.
            pending_error = self.state.pending.get('kind') == 'python_error'
            if pending_error and node.node_id in self.state.node_inputs:
                values = dict(self.state.node_inputs[node.node_id])
            else:
                values = self.dynamic_inputs(node, 'python')
                values = await self.fill(schema_for(definitions(node, 'inputs')), values)
                self.state.node_inputs[node.node_id] = dict(values)
                self.save()
            # A restored Python error must not rerun the failed script before the user's action.
            report = self.state.node_results.get(node.node_id, {}).get('python_error_report') if pending_error else None
            if pending_error and report is None:
                report = {'error': self.state.last_error, 'values': {}, 'stdout': '', 'stderr': ''}
            while True:
                if report is None:
                    report = await execute_python(s.get('code', ''), values, [x['name'] for x in definitions(node, 'outputs')],
                        cwd=getattr(self.execution_context, 'workspace_root', None), timeout=s.get('timeout', 60))
                self.state.last_error = report.get('error')
                if not report.get('error'):
                    missing = [x['name'] for x in definitions(node, 'outputs') if x['name'] not in report['values']]
                    if missing:
                        report['error'] = 'Python не присвоил выходы: ' + ', '.join(missing)
                        self.state.last_error = report['error']
                    else:
                        self.put_outputs(node, 'python', report['values'])
                        return 'exec:out', report
                if policy(s) is not None:
                    self.state.values[node.node_id] = {}
                    return 'exec:out', report
                self.state.node_results[node.node_id] = {'python_error_report': report}
                response = await self.ask('python_error', str(report['error']), actions=[x['value'] for x in PYTHON_CODE_ERROR_ACTIONS])
                action = response['action']
                if action == 'retry_script':
                    report = None; continue
                if action == 'shutdown_generation':
                    raise PipelineShutdown('Генерация остановлена пользователем.')
                if action == 'finish_graph':
                    return None, self.state.last_result
                self.state.values[node.node_id] = {}
                return 'exec:out', report
        if t == GRAPH_HUMAN_IN_LOOP_TYPE:
            items = definitions(node, 'outputs') if s.get('arguments_enabled') else []
            response = await self.ask('human', s.get('message') or 'Продолжить выполнение?', fields=items, actions=['continue'])
            values = {k: v for k, v in response.get('values', {}).items() if v is not None and (not isinstance(v, str) or v.strip())}
            values = {x['name']: typed(values[x['name']], x['type']) for x in items if x['name'] in values}
            values = await self.fill(schema_for(items), values, 'Fill only empty fields. Do not change user-provided values.')
            self.put_outputs(node, 'human', values)
            self.state.last_error = None
            return 'exec:out', values
        if t in {GRAPH_LOOP_TYPE, GRAPH_ROUTER_TYPE}:
            return await self.control(node)
        if t == 'pipeline.call':
            child = self.resolve_call(str(s.get('pipeline') or ''))
            items, outputs = interface(child)
            vals = {}
            for x in items:
                v = self.incoming(node, f"arg:call:in:{x['id']}")
                if v is not MISSING:
                    vals[x['name']] = v
            child_runner = PipelineRunner(child, tools=self.tools, enabled=self.enabled, model=self.model,
                invoke_tool=self.invoke_tool, broker=self.broker, emit=self.emit, execution_context=self.execution_context,
                resolve_call=self.resolve_call, depth=self.depth+1,
                checkpoint=self.checkpoint.with_name(self.checkpoint.stem + '_' + node.node_id + '_' + str(self.state.steps) + '.json') if self.checkpoint else None)
            result = await child_runner.run(vals, self.context_text, resume=True, state=RunState(run_id=self.state.run_id))
            data = result.get('result', result) if isinstance(result, dict) else {}
            self.state.values[node.node_id] = {'arg:result': result, **{f"arg:call:out:{x['id']}": data[x['name']] for x in outputs if isinstance(data, dict) and x['name'] in data}}
            # The parent step index gives every visit its own durable child checkpoint.
            self.state.last_error = None
            return 'exec:out', result
        module, tool_name = identity(node)
        outcome = None
        prompts = []
        for e in sorted((e for e in self.pipeline.edges if e.target_id == node.node_id and stream(e.target_port) == 'prompt'), key=lambda e: int(e.target_port.split(':')[1])):
            v = self.value(e.source_id, e.source_port)
            if v is not MISSING:
                prompts.append(str(v))
        instruction = '\n'.join(prompts)
        if t == 'advanced.agent_decision':
            if not self.model:
                raise ValueError('Самостоятельное решение агента требует модель')
            result = await self.model(None, instruction, {'autonomous_once': True}, self)
        elif tool_name:
            tool = self.tools[tool_name]
            vals = dict(s.get('arguments') or {})
            for name, prop in tool.args_schema.get('properties', {}).items():
                v = self.incoming(node, 'arg:' + name)
                if v is not MISSING:
                    vals[name] = typed(v, prop.get('type', ''))
                elif 'default' in prop:
                    vals.setdefault(name, prop['default'])
            if tool_name == 'search_knowledge_base' and s.get('databases'):
                vals['databases'] = s['databases']
            if node.node_id in self.tool_overrides:
                vals = dict(self.tool_overrides[node.node_id])
            cached = self.state.node_inputs.get(node.node_id, {})
            if cached.get('_visit_step') == self.state.steps and isinstance(cached.get('arguments'), dict):
                vals = dict(cached['arguments'])
            else:
                vals = await self.fill(tool.args_schema, vals, instruction)
                self.state.node_inputs[node.node_id] = {'_visit_step': self.state.steps, 'arguments': vals}
                self.save()
            call_id = f'{self.state.run_id}:{self.state.steps}:tool:{self.state.attempts.get(f"{node.node_id}:{self.state.steps}", 0)}'
            await self.emit({'type': 'tool_start', 'id': call_id, 'name': tool_name, 'args': vals})
            from .extensions import execute_tool_node
            try:
                outcome = await execute_tool_node(node, self, vals)
                result = outcome.result if outcome is not None else await self.invoke_tool(tool_name, vals)
            except BaseException as exc:
                from core.reasoning_control import FinalizationRequested
                await self.emit({'type': 'tool_result', 'id': call_id, 'name': tool_name,
                                 'output': str(exc), 'error': str(exc), 'status': 'stopped' if isinstance(exc, (PipelineShutdown, FinalizationRequested, asyncio.CancelledError)) else 'failed'})
                raise
            error = outcome.error if outcome is not None else result_error(result, tool)
            await self.emit({'type': 'tool_result', 'id': call_id, 'name': tool_name, 'output': str(result),
                             **({'error': str(error), 'status': 'failed'} if error else {})})
        else:
            if not self.model:
                raise ValueError('Нода модуля требует модель')
            result = await self.model(None, instruction, {'module': module}, self)
        self.state.last_error = outcome.error if outcome is not None else result_error(result, self.tools.get(tool_name))
        self.state.values[node.node_id] = {'arg:result': result, **(outcome.values if outcome is not None else {})}
        if outcome is not None and outcome.halt and policy(s) is None:
            raise RuntimeError(str(outcome.error))
        return outcome.output if outcome is not None else 'exec:out', result

    async def control(self, node):
        loop = node.type_id == GRAPH_LOOP_TYPE
        s = node.settings
        count = self.state.iterations.get(node.node_id, 0)
        if loop and not maximum_allows_repeat(count, int(s.get('max_repeats', 10))):
            return 'exec:continue', self.state.last_result
        mechanisms = normalize_loop_conditions(s.get('conditions')) if loop else normalize_router_mechanisms(s.get('mechanisms'))
        route_count = int(s.get('route_count', 1))
        for item in mechanisms:
            if not item['enabled']:
                continue
            kind = item['type']
            value = MISSING
            if kind == 'error':
                if not self.state.last_error:
                    continue
                value = True if loop else item['route']
            elif kind == 'argument':
                value = self.incoming(node, 'arg:repeat' if loop else 'arg:route')
            elif kind == 'counter':
                value = counter_requests_repeat(count, item['repeats'])
            elif kind == 'always':
                value = item['route']
            elif kind == 'python':
                if self.state.pending:
                    value = MISSING
                else:
                    report = await execute_python(item.get('code', ''), {'result': self.state.last_result, 'error': self.state.last_error,
                        'iteration': count}, ['result', 'decision', 'route'], cwd=getattr(self.execution_context, 'workspace_root', None))
                    value = report['values'].get('decision', report['values'].get('route', report['values'].get('result', MISSING))) if not report['error'] else MISSING
            elif kind in {'agent', 'subagent'}:
                schema = {'type': 'object', 'properties': {'decision': {'type': 'boolean' if loop else 'integer'}}, 'required': ['decision']}
                try:
                    vals = await self.model(schema, item.get('prompt', ''), {'decision_kind': kind, 'result': self.state.last_result,
                        'error': self.state.last_error, 'iteration': count, 'route_count': route_count,
                        'route_descriptions': s.get('route_descriptions', {})}, self)
                    value = vals.get('decision', MISSING)
                except Exception as exc:
                    from core.reasoning_control import FinalizationRequested
                    if isinstance(exc, (FinalizationRequested, PipelineShutdown)) or policy(node.settings) is not None: raise
                    value = MISSING
                    await self.emit({'type': 'thought', 'delta': f'Решение {kind} недоступно: {exc}. Ожидается решение пользователя.\n'})
            elif kind == 'user':
                value = MISSING
            valid = isinstance(value, bool) if loop else normalize_router_route(value, route_count) is not None
            if not valid:
                response = await self.ask('loop' if loop else 'router', item.get('message') or ('Повторить виток?' if loop else 'Выберите маршрут'),
                    actions=['repeat', 'continue'] if loop else [str(i) for i in range(route_count)],
                    route_descriptions=s.get('route_descriptions', {}))
                value = response['action'] == 'repeat' if loop else int(response['action'])
            if loop:
                if value:
                    self.state.iterations[node.node_id] = count + 1
                    return 'exec:repeat', self.state.last_result
                # Explicit Python False/User Continue is a decision, unlike an unmet error/counter.
                if kind in {'python', 'user', 'agent', 'subagent'}:
                    break
            else:
                return f'exec:route:{value}', self.state.last_result
        if loop:
            return 'exec:continue', self.state.last_result
        response = await self.ask('router', 'Ни один механизм не выбрал маршрут.', actions=[str(i) for i in range(route_count)])
        return f"exec:route:{response['action']}", self.state.last_result
