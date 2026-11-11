"""Headless graph contracts shared by compiler, module packaging and runner."""
from __future__ import annotations
import keyword
from .control import *
from .execution import NOTIFICATION, PARALLEL, TRANSFORM, HANDOFF, policy, error_ports, parallel_owners, parallel_children, parallel_mode, is_delegation

SCALARS = {'argument.string': 'string', 'argument.binary': 'boolean',
           'argument.integer': 'integer', 'argument.float': 'float', 'argument.array': 'array'}
# Existing editor calls these nodes arg.*, retained alongside the readable aliases.
SCALARS.update({'content.' + k.split('.')[1]: v for k, v in list(SCALARS.items())})
SCALARS.update({'arg.' + k.split('.')[1]: v for k, v in list(SCALARS.items())})

def definitions(node, section):
    return normalize_argument_definitions(node.settings.get(section), with_description=True)

def interface(pipeline):
    inputs = []
    outputs = []
    for n in pipeline.nodes:
        if n.type_id == GRAPH_END_TYPE:
            for item in definitions(n, 'inputs'):
                if item['name'] not in {x['name'] for x in outputs}:
                    outputs.append(item)
    return inputs, outputs

def schema_for(items):
    return {'type': 'object', 'properties': {x['name']: {'type': 'number' if x['type'] == 'float' else x['type'],
            **({'items': {}} if x['type'] == 'array' else {}), 'description': x.get('description', '')} for x in items},
            'required': [x['name'] for x in items], 'additionalProperties': False}

def identity(node):
    parts = node.type_id.split('.')
    if parts[:2] == ['core', 'tool']:
        return 'memory', '.'.join(parts[2:])
    if len(parts) >= 3 and parts[0] in {'system', 'custom', 'core'}:
        return parts[2], '.'.join(parts[3:]) if parts[1] == 'tool' else None
    if node.type_id in SCALARS or node.type_id in {GRAPH_START_TYPE, GRAPH_END_TYPE, GRAPH_SHUTDOWN_TYPE, GRAPH_LOOP_TYPE, GRAPH_ROUTER_TYPE, GRAPH_HUMAN_IN_LOOP_TYPE, PYTHON_CODE_TYPE, NOTIFICATION, PARALLEL, TRANSFORM, 'content.prompt', 'content.mcp_prompt', 'advanced.agent_decision', 'pipeline.call'}:
        return None, None
    from .extensions import extension_identity
    return extension_identity(node)

def is_data(node):
    return node.type_id in SCALARS or node.type_id in {'content.prompt', 'content.mcp_prompt'}

def port_sets(node, tools=None, target_interface=None, *, contract=None):
    t, s = node.type_id, node.settings
    ins, outs = set(), set()
    if t in SCALARS:
        if SCALARS[t] == 'array':
            ins = {f'array:{i}' for i in range(1, max(1, int(s.get('_array_input_count', 1))) + 1)}
        elif s.get('value') is None or s.get('value') == '':
            ins = {'arg:in'}
        return ins, {'arg:out'}
    if t == 'content.mcp_prompt':
        return {'arg:messages'}, {'prompt:out'}
    if t == 'content.prompt':
        return {'arg:in', *{f'arg:in:{i}' for i in range(2, int(s.get('_prompt_arg_input_count', 1)) + 1)}}, {'prompt:out'}
    if t == GRAPH_START_TYPE:
        return set(), {'exec:out'}
    ins = {'exec:in'}
    outs = {'exec:out'}
    if t == NOTIFICATION:
        ins |= {'arg:in'}
    elif t == TRANSFORM:
        ins |= {'arg:in'}; outs |= {'arg:result'}
    elif t == PARALLEL:
        outs |= {'arg:result'}
    elif t == 'advanced.agent_decision':
        ins |= {f'prompt:{i}' for i in range(1, int(s.get('_prompt_input_count', 1)) + 1)}
    elif t in {GRAPH_END_TYPE, GRAPH_SHUTDOWN_TYPE}:
        outs = set()
        ins |= {f"arg:end:in:{x['id']}" for x in definitions(node, 'inputs')}
    elif t == PYTHON_CODE_TYPE:
        ins |= {f"arg:python:in:{x['id']}" for x in definitions(node, 'inputs')}
        outs |= {f"arg:python:out:{x['id']}" for x in definitions(node, 'outputs')}
    elif t == GRAPH_HUMAN_IN_LOOP_TYPE:
        if s.get('arguments_enabled'):
            outs |= {f"arg:human:out:{x['id']}" for x in definitions(node, 'outputs')}
    elif t == GRAPH_LOOP_TYPE:
        if any(x.get('type') == 'argument' and x.get('enabled') for x in s.get('conditions', [])):
            ins |= {'arg:repeat'}
        outs = {'exec:continue', 'exec:repeat'}
    elif t == GRAPH_ROUTER_TYPE:
        if any(x.get('type') == 'argument' and x.get('enabled') for x in s.get('mechanisms', [])):
            ins |= {'arg:route'}
        outs = {f'exec:route:{i}' for i in range(int(s.get('route_count', 1)))}
    elif t == 'pipeline.call':
        _, outputs = target_interface or ([], definitions(node, 'outputs'))
        inputs = []
        ins |= {f"arg:call:in:{x['id']}" for x in inputs}
        outs |= {f"arg:call:out:{x['id']}" for x in outputs}
        outs |= {'arg:result'}
    else:
        module, tool = identity(node)
        if module is None:
            raise ValueError(f'Неизвестный тип ноды: {t}')
        ins |= {f'prompt:{i}' for i in range(1, int(s.get('_prompt_input_count', 1)) + 1)}
        outs |= {'arg:result'}
        if tool and tools is not None and tool in tools:
            ins |= {f'arg:{name}' for name in tools[tool].args_schema.get('properties', {})}
    from .extensions import node_contract
    outs.update(port['key'] for port in (node_contract(t) if contract is None else contract).get('outputs', ()))
    if error_ports(s):
        outs |= {'exec:error', 'arg:error'}
    if s.get('_parallel_parent') or s.get('_parallel_chain'):
        ins.discard('exec:in')
        outs = {p for p in outs if not p.startswith('exec:') or p == HANDOFF}
        if s.get('_parallel_chain'):
            ins.add('exec:in')  # Only handoff edges are legal here.
    return ins, outs

def stream(port):
    return 'arg' if port.startswith('array:') else port.split(':')[0]

def reachable(pipeline, *, _index=None, _owners=None):
    from .topology import GraphIndex
    index = _index or GraphIndex(pipeline)
    nodes = index.nodes
    starts = [n.node_id for n in pipeline.nodes if n.type_id == GRAPH_START_TYPE]
    if len(starts) != 1:
        raise ValueError('В pipeline должна быть ровно одна Graph Start')
    owners = parallel_owners(pipeline) if _owners is None else _owners
    children = {}
    for child,owner in owners.items(): children.setdefault(owner, []).append(child)
    live, pending = set(), list(starts)
    while pending:
        key = pending.pop()
        if key in live: continue
        if key not in nodes: raise ValueError(f'Не существует нода {key}')
        live.add(key)
        pending.extend(children.get(key, ()))
        pending.extend(e.target_id for e in index.outgoing.get(key, ()) if stream(e.source_port) == 'exec')
    pending = list(live)
    while pending:
        key = pending.pop()
        for edge in index.incoming.get(key, ()):
            if stream(edge.source_port) == 'exec' or edge.source_id in live: continue
            source = nodes.get(edge.source_id)
            if source is None: raise ValueError(f'Не существует источник {edge.source_id}')
            if not is_data(source): raise ValueError(f'Arg {edge.source_id} поступает от недостижимой исполняемой ноды')
            live.add(edge.source_id); pending.append(edge.source_id)
    return live

def dependencies(pipeline):
    live = reachable(pipeline)
    required = {identity(n)[0] for n in pipeline.nodes if n.node_id in live
                and identity(n)[0] not in {None, 'memory'}}
    for n in pipeline.nodes:
        if n.node_id not in live:
            continue
        from .extensions import extension_dependencies
        required.update(extension_dependencies(n))
        for item in n.settings.get('conditions', n.settings.get('mechanisms', [])):
            if isinstance(item, dict) and item.get('type') == 'subagent' and item.get('enabled'):
                required.add('subagent_manager')
        for database in n.settings.get('databases', []):
            owner = str(database).split('::', 1)[0]
            if owner.startswith(('system.', 'custom.')):
                required.add(owner.split('.', 1)[1])
    return sorted(required)

def validate(pipeline, *, tools=None, enabled=None, resolve_call=None, stack=()):
    from .topology import GraphIndex, first_cycle, parallel_data_issues, unfinished_exec
    from .extensions import node_contract, connection_error
    index, contracts = GraphIndex(pipeline), {}
    for node in pipeline.nodes:
        if node.type_id not in contracts: contracts[node.type_id] = node_contract(node.type_id)
    errors, warnings, ports = [], [], {}
    try:
        owners = parallel_owners(pipeline, contracts=contracts)
        live = reachable(pipeline, _index=index, _owners=owners)
    except ValueError as exc:
        return [str(exc)], [], set()
    nodes = index.nodes
    for n in pipeline.nodes:
        if n.node_id not in live:
            continue
        try:
            child_iface = None
            if n.type_id == 'pipeline.call':
                reference = str(n.settings.get('pipeline') or '')
                if not resolve_call:
                    raise ValueError('Nested Pipeline не имеет контекста модуля')
                if reference in stack:
                    raise ValueError('Рекурсивный вызов pipeline: ' + reference)
                child = resolve_call(reference)
                ce, cw, _ = validate(child, tools=tools, enabled=enabled, resolve_call=resolve_call, stack=(*stack, reference))
                errors.extend(f'{n.node_id}: {e}' for e in ce)
                warnings.extend(cw)
                child_iface = interface(child)
            from dataclasses import replace
            effective = replace(n, settings=dict(n.settings, _parallel_chain=True)) if n.node_id in owners and not n.settings.get('_parallel_parent') else n
            ports[n.node_id] = port_sets(effective, tools, child_iface, contract=contracts[n.type_id])
            if n.node_id in owners:
                if identity(n)[1] is None:
                    errors.append(f'{n.node_id}: parallel children must be tool nodes')
                if error_ports(n.settings):
                    errors.append(f'{n.node_id}: route errors on the parallel container')
                if int(n.settings.get('_parallel_max_calls', 1)) < 0:
                    errors.append(f'{n.node_id}: invalid call limit')
            policy(n.settings)
            if n.type_id == PARALLEL:
                parallel_mode(n.settings)
                if sum(HANDOFF in contracts[c.type_id].get('output_targets', {}) for c in parallel_children(pipeline, n.node_id)) > 1:
                    errors.append(f'{n.node_id}: в блоке допускается только одна нода делегирования субагентам')
            if n.type_id == TRANSFORM and not n.settings.get('operations'):
                errors.append(f'{n.node_id}: no transform operations')
            module, tool = identity(n)
            if enabled is not None and module not in {None, 'memory'} and module not in enabled:
                errors.append(f'{n.node_id}: требуемый модуль {module} отключён или недоступен в сессии')
            if enabled is not None:
                for database in n.settings.get('databases', []):
                    owner = str(database).split('::', 1)[0]
                    if owner.startswith(('system.', 'custom.')) and owner.split('.', 1)[1] not in enabled:
                        errors.append(f'{n.node_id}: модуль базы данных {owner} отключён')
            if tool and tools is not None and tool not in tools:
                errors.append(f'{n.node_id}: инструмент {tool} недоступен')
            for section in ('inputs', 'outputs'):
                items = definitions(n, section)
                names = [x['name'] for x in items]
                if len(names) != len(set(names)) or any(not x.isidentifier() or keyword.iskeyword(x) for x in names):
                    errors.append(f'{n.node_id}: имена Arg должны быть уникальными идентификаторами')
            if n.type_id in {GRAPH_LOOP_TYPE, GRAPH_ROUTER_TYPE}:
                key = 'conditions' if n.type_id == GRAPH_LOOP_TYPE else 'mechanisms'
                normal = normalize_loop_conditions if key == 'conditions' else normalize_router_mechanisms
                mechanisms = normal(n.settings.get(key))
                if not any(x['enabled'] for x in mechanisms):
                    errors.append(f'{n.node_id}: не включён ни один механизм')
                if int(n.settings.get('max_repeats', 10)) < 1:
                    errors.append(f'{n.node_id}: max_repeats должен быть положительным')
                for x in mechanisms:
                    if not x['enabled']:
                        continue
                    if x['type'] in {'error', 'always'} and n.type_id == GRAPH_ROUTER_TYPE:
                        if normalize_router_route(x.get('route'), int(n.settings.get('route_count', 1))) is None:
                            errors.append(f'{n.node_id}: неверный маршрут {x.get("route")}')
                    if x['type'] == 'subagent' and tools is not None and 'delegate_to_subagents' not in tools:
                        errors.append(f'{n.node_id}: модуль субагентов отключён')
                    if x['type'] == 'python':
                        compile(x.get('code') or '', '<condition>', 'exec')
            if n.type_id == PYTHON_CODE_TYPE:
                compile(n.settings.get('code') or '', '<pipeline>', 'exec')
        except Exception as exc:
            errors.append(f'{n.node_id}: {exc}')
    seen_in, seen_out = set(), set()
    from .extensions import node_contract, connection_error
    for e in pipeline.edges:
        if e.target_id not in live or e.source_id not in live:
            continue
        si, ti = ports.get(e.source_id), ports.get(e.target_id)
        if si is None or ti is None:
            continue
        if e.source_port not in si[1] or e.target_port not in ti[0]:
            errors.append(f'Неверный порт {e.source_id}.{e.source_port} → {e.target_id}.{e.target_port}')
        if stream(e.source_port) != stream(e.target_port):
            errors.append('Соединены несовместимые потоки')
        issue = connection_error(contracts[nodes[e.source_id].type_id], e.source_port,
            nodes[e.target_id].type_id, e.target_port,
            (edge.source_port for edge in index.outgoing.get(e.source_id, ())))
        if issue:
            errors.append(f'{e.source_id}.{e.source_port}: {issue}')
        source_owner, target_owner = owners.get(e.source_id), owners.get(e.target_id)
        if stream(e.source_port) == 'exec' and (source_owner or target_owner):
            if e.source_port != HANDOFF or not source_owner or source_owner != target_owner:
                errors.append('Parallel jobs may only connect Exec through subagent handoff')
        a, b = (e.source_id, e.source_port), (e.target_id, e.target_port)
        if stream(e.source_port) == 'exec' and a in seen_out:
            errors.append(f'Exec fan-out запрещён: {a}')
        if stream(e.target_port) != 'exec' and b in seen_in:
            errors.append(f'Вход Arg/Prompt занят: {b}')
        seen_in.add(b); seen_out.add(a)
    for issue in parallel_data_issues(pipeline, index=index, owners=owners):
        errors.append(issue['node_id'] + ': ' + issue['message'])
    if first_cycle(index.adjacency(live, data=True)):
        errors.append('Циклическая зависимость Arg/Prompt')
    if first_cycle(index.adjacency(live, omit_repeat=True)):
        errors.append('Обратный Exec-путь должен проходить через выход Repeat ноды Loop. Continue не ограничивает число повторов.')
    for key,outputs in unfinished_exec(pipeline, index=index, ports=ports, owners=owners, live=live).items():
        warnings.append(f"{key}: незавершённые ветки: {', '.join(outputs)}")
    return list(dict.fromkeys(errors)), warnings, live
