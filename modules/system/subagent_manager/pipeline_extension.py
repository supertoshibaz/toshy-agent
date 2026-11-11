"""Subagent-owned pipeline ports and explicit result handoff; no Qt imports."""
from modules.pipelines.extensions import ToolOutcome, current_node_payload, node_payload_scope

NODE = 'system.tool.subagent_manager.delegate_to_subagents'
LEGACY_NODE = 'system.tool.subagent_manager.delegate_to_subagent'
NODE_TYPES = (NODE, LEGACY_NODE)
HANDOFF = 'exec:subagent'
RESULT = '_subagent_result'
CALL_STATE = '_subagent_call'


def node_contract(type_id):
    if type_id in NODE_TYPES:
        return {
            'outputs': [{'key': HANDOFF, 'label': 'Subagent · Exec', 'stream': 'exec'}],
            'exclusive_outputs': [['exec:out', HANDOFF]],
            'output_targets': {HANDOFF: {'types': list(NODE_TYPES), 'ports': ['exec:in']}},
        }


def incoming_result():
    payload = current_node_payload()
    return payload.get('previous_result') if isinstance(payload, dict) and payload.get('type') in NODE_TYPES else None


def sequential_requests():
    payload = current_node_payload()
    return isinstance(payload, dict) and payload.get('parallel_request_mode') == 'sequential'


def result_message(payload):
    """A separate data message, leaving the next node's own task intact."""
    from langchain_core.messages import HumanMessage
    return HumanMessage(
        content='Result from the previous subagent '
        + str(payload.get('name') or payload.get('agent_id') or '')
        + '. Use it as input data for the next task:\n\n'
        + payload['output'],
        additional_kwargs={'subagent_handoff': {
            key: payload.get(key, '') for key in ('agent_id', 'name', 'id')
        }},
    )


def make_result(text, record=None):
    from modules.system.subagent_manager.definitions import SubagentResult
    result = SubagentResult(text, direct=bool(record and record.get('direct')))
    result.pipeline_result = {
        key: record.get(key, '') for key in ('status', 'output', 'error', 'agent_id', 'name', 'id')
    } if record else {'status': 'error', 'output': '', 'error': str(text)}
    return result


async def execute_tool_node(node, runner, arguments):
    previous = None
    incoming = runner.state.incoming_edge
    if incoming.get('target_id') == node.node_id and incoming.get('source_port') == HANDOFF:
        previous = runner.state.values.get(incoming['source_id'], {}).get(RESULT)
        if not previous or previous.get('status') != 'completed':
            raise RuntimeError('Нет завершённого результата предыдущего субагента; цепочка остановлена.')
    chained = any(edge.source_id == node.node_id and edge.source_port == HANDOFF for edge in runner.pipeline.edges)
    if runner.state.values.get(node.node_id, {}).get(CALL_STATE) == 'running':
        raise RuntimeError('Прерванный вызов субагента не повторяется автоматически. Проверьте историю и запустите новое обращение.')
    runner.state.values[node.node_id] = {CALL_STATE: 'running'}
    runner.save()
    with node_payload_scope({'type': NODE, 'previous_result': previous,
                            'parallel_request_mode': getattr(runner, 'parallel_request_mode', 'concurrent')}):
        result = await runner.invoke_tool('delegate_to_subagents' if node.type_id == NODE else 'delegate_to_subagent', arguments)
    payload = getattr(result, 'pipeline_result', None)
    error = None if payload and payload.get('status') == 'completed' else str(
        (payload or {}).get('error') or 'Субагент не вернул подтверждённый завершённый результат.'
    )
    return ToolOutcome(
        result=result,
        output=HANDOFF if chained else 'exec:out',
        values={RESULT: payload or {'status': 'error', 'error': error}, CALL_STATE: 'completed'},
        error=error,
        halt=bool(error and (chained or previous is not None)),
    )
