"""Plan once, execute the batch in the selected mode, join before leaving."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import asdict
from .contracts import identity, stream
from .execution import HANDOFF, parallel_children, parallel_mode, is_delegation


async def execute_parallel(runner, container):
    from .runtime import PipelineRunner, RunState, MISSING, PipelineShutdown, typed
    from core.reasoning_control import FinalizationRequested, ensure_work_allowed
    key = f'{container.node_id}:{runner.state.steps}'
    record = runner.state.parallel.setdefault(key, {'jobs': {}, 'plan': None})
    children = parallel_children(runner.pipeline, container.node_id)
    if not children: raise ValueError('Parallel block has no tools')
    mode = parallel_mode(container.settings)
    if sum(is_delegation(c.type_id) for c in children) > 1:
        raise ValueError('В блоке допускается только одна нода делегирования субагентам')
    if record['plan'] is None:
        fixed, schemas, prompts, needs_model = {}, {}, {}, False
        for child in children:
            name = identity(child)[1]
            if name not in runner.tools: raise ValueError('Unavailable parallel tool: ' + str(name))
            schema = runner.tools[name].args_schema
            values = dict(child.settings.get('arguments') or {})
            for arg, prop in schema.get('properties', {}).items():
                v = runner.incoming(child, 'arg:' + arg)
                if v is not MISSING: values[arg] = typed(v, prop.get('type', ''))
                elif 'default' in prop: values.setdefault(arg, prop['default'])
            if name == 'search_knowledge_base' and child.settings.get('databases'):
                values['databases'] = child.settings['databases']
            limit = int(child.settings.get('_parallel_max_calls', 1))
            missing = [x for x in schema.get('required', ()) if x not in values]
            needs_model |= bool(missing or limit != 1)
            fixed[child.node_id] = values
            item = {'type': 'object', 'properties': {k: v for k, v in schema.get('properties', {}).items() if k not in values},
                    'required': missing, 'additionalProperties': False}
            schemas[child.node_id] = dict(type='array', items=item, minItems=1, **({'maxItems': limit} if limit else {}))
            prompts[child.node_id] = '\n'.join(str(runner.value(e.source_id, e.source_port)) for e in runner.pipeline.edges
                if e.target_id == child.node_id and stream(e.target_port) == 'prompt')
        if needs_model:
            if not runner.model: raise ValueError('Parallel arguments require a model')
            plan = await runner.model({'type': 'object', 'properties': schemas, 'required': list(schemas), 'additionalProperties': False},
                'Plan independent tool calls in a single JSON object. Each node ID maps to an array of argument objects. '
                'Use the supplied node prompts and tool schemas. Fixed arguments must not change. '
                'Results of other jobs are not available at planning time. Respect each maxItems; use only as many calls as needed.',
                {'parallel_fixed_arguments': fixed, 'node_prompts': prompts,
                 'tool_schemas': {c.node_id: runner.tools[identity(c)[1]].args_schema for c in children}}, runner)
            from jsonschema import validate
            validate(plan, {'type': 'object', 'properties': schemas, 'required': list(schemas), 'additionalProperties': False})
            plan = {nid: [dict(args, **fixed[nid]) for args in jobs] for nid, jobs in plan.items()}
        else:
            plan = {nid: [args] for nid, args in fixed.items()}
        from jsonschema import validate
        for child in children:
            for args in plan[child.node_id]: validate(args, runner.tools[identity(child)[1]].args_schema)
        if sum(len(calls) for calls in plan.values()) + runner.state.steps > 10000:
            raise ValueError('Превышен общий лимит 10000 шагов pipeline')
        record['plan'] = plan
        runner.save()

    container_attempt = runner.state.attempts.get(f'{container.node_id}:{runner.state.steps}', 0)
    if container_attempt > record.get('attempt', 0):
        for saved in record['jobs'].values():
            if saved['status'] == 'failed':
                record.setdefault('failures', []).append(deepcopy(saved))
                saved.update(status='new', error=None, retry=container_attempt)
        record['attempt'] = container_attempt
        record['merged'] = False
        runner.save()

    async def job(child, index, args):
        job_key = f'{child.node_id}:{index}'
        saved = record['jobs'].setdefault(job_key, {'status': 'new'})
        if saved['status'] in {'completed', 'failed', 'skipped'}:
            return saved
        branch = PipelineRunner(runner.pipeline, tools=runner.tools, enabled=runner.enabled, model=runner.model,
            invoke_tool=runner.invoke_tool, broker=runner.broker, emit=runner.emit, execution_context=runner.execution_context,
            resolve_call=runner.resolve_call, depth=runner.depth)
        branch.context_text, branch.inputs = runner.context_text, deepcopy(runner.inputs)
        branch.parallel_request_mode = mode
        branch.state = RunState(**deepcopy(saved['state'])) if saved.get('state') else RunState(
            run_id=f'{runner.state.run_id}:{key}:{job_key}', current_node=child.node_id,
            values=deepcopy(runner.state.values), journal=deepcopy(runner.state.journal),
            loop_scopes=deepcopy(runner.state.loop_scopes))
        if saved.get('retry'):
            branch.state.run_id = f'{runner.state.run_id}:{key}:{job_key}:retry{saved["retry"]}'
        saved.setdefault('journal_prefix', len(branch.state.journal))
        branch.tool_overrides[child.node_id] = args
        def save_branch():
            saved['state'] = asdict(branch.state)
            runner.save()
        branch.save = save_branch
        try:
            if saved.get('in_flight'):
                response = await branch.ask('parallel_interrupted',
                    'The previous tool call may have completed before interruption. Retry can repeat side effects.',
                    actions=['retry', 'skip'])
                if response['action'] == 'skip':
                    branch.state.values[branch.state.current_node] = {}
                    saved.update(status='failed', error='Interrupted tool call skipped; outcome unknown', result=None, in_flight=False)
                    save_branch(); return saved
                branch.state.values.pop(branch.state.current_node, None)
            saved['status'] = 'running'
            result = branch.state.last_result
            while branch.state.current_node:
                ensure_work_allowed(runner.execution_context)
                if branch.state.steps >= 10000: raise ValueError('Превышен лимит шагов ветки pipeline')
                node = branch.nodes[branch.state.current_node]
                saved['in_flight'] = True
                save_branch()
                output, result = await branch.execute_checked(node)
                saved['in_flight'] = False
                branch.state.last_result = result
                branch.state.node_results[node.node_id] = {'result': result, 'error': branch.state.last_error, 'exec': output}
                branch.state.steps += 1
                branch.state.visits[node.node_id] = branch.state.visits.get(node.node_id, 0) + 1
                from .history import append
                append(branch, dict(kind='node', **branch.state.node_results[node.node_id]))
                if branch.state.last_error:
                    from .execution import policy
                    config = policy(node.settings)
                    skip = bool(config and (config['mode'] == 'skip' or config['mode'] == 'retry' and config['exhausted'] == 'skip'))
                    saved.update(status='skipped' if skip else 'failed', error=str(branch.state.last_error), result=result)
                    save_branch(); return saved
                edge = next((e for e in runner.pipeline.edges if e.source_id == node.node_id and e.source_port == HANDOFF), None) if output == HANDOFF else None
                branch.state.current_node = edge.target_id if edge else ''
                branch.state.incoming_edge = asdict(edge) if edge else {}
                save_branch()
            saved.update(status='completed', result=result, error=None)
            save_branch()
        except (asyncio.CancelledError, PipelineShutdown, FinalizationRequested):
            save_branch(); raise
        except Exception as exc:
            saved.update(status='failed', error=str(exc), in_flight=False)
            save_branch()
        return saved

    calls = [(child, index, args) for child in children
             for index, args in enumerate(record['plan'][child.node_id])]
    # Both modes consume the same precomputed plan. A completed call never
    # triggers another argument-planning request for the following tool.
    if mode == 'sequential':
        for call in calls:
            await job(*call)
    else:
        tasks = [asyncio.create_task(job(*call)) for call in calls]
        # Ordinary failures are collected inside job; control exceptions stop the whole run.
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            runner.save()
            raise
    results, failures = {}, []
    for child in children:
        calls = [record['jobs'][f'{child.node_id}:{i}'] for i in range(len(record['plan'][child.node_id]))]
        values = [call.get('result') if call['status'] == 'completed' else {'error': call.get('error'), 'result': call.get('result')} for call in calls]
        multiple = int(child.settings.get('_parallel_max_calls', 1)) != 1
        results[child.node_id] = values if multiple else values[0]
        per_node = {}
        for call in calls:
            for nid, outputs in call.get('state', {}).get('values', {}).items():
                if nid == child.node_id or nid in call.get('state', {}).get('node_results', {}):
                    per_node.setdefault(nid, []).append(outputs)
        for nid, outputs in per_node.items():
            runner.state.values[nid] = {port: [v.get(port) for v in outputs] for port in set().union(*(v.keys() for v in outputs))} if multiple else outputs[0]
        failures.extend(f'{child.node_id}: {c.get("error")}' for c in calls if c['status'] == 'failed')
        if not record.get('merged'):
            for call in calls:
                journal = call.get('state', {}).get('journal', [])
                runner.state.journal.extend(deepcopy(journal[call.get('journal_merged', call.get('journal_prefix', 0)):]))
                call['journal_merged'] = len(journal)
    record['merged'] = True
    runner.state.values[container.node_id] = {'arg:result': results}
    runner.state.last_error = '\n'.join(failures) or None
    if failures and container.settings.get('_error_policy') is None:
        raise RuntimeError(runner.state.last_error)
    return 'exec:out', results
