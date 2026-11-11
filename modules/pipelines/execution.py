"""Shared, Qt-free execution policies and parallel graph ownership."""
from __future__ import annotations

NOTIFICATION = 'graph.notification'
PARALLEL = 'graph.parallel'
TRANSFORM = 'content.transform'
HANDOFF = 'exec:subagent'


def parallel_children(pipeline, container_id):
    return sorted((n for n in pipeline.nodes if n.settings.get('_parallel_parent') == container_id),
                  key=lambda n: (int(n.settings.get('_parallel_order', 0)), n.node_id))


def is_delegation(type_id):
    from .extensions import node_contract
    return HANDOFF in node_contract(type_id).get('output_targets', {})


def parallel_mode(settings):
    mode = settings.get('_parallel_mode', 'concurrent')
    if mode not in {'concurrent', 'sequential'}:
        raise ValueError('Неизвестный режим запросов блока: ' + str(mode))
    return mode


def policy(settings):
    raw = settings.get('_error_policy')
    if not isinstance(raw, dict):
        return None  # Saved graphs keep their original behaviour.
    mode = raw.get('mode', 'stop')
    if mode not in {'stop', 'retry', 'skip', 'branch'}:
        raise ValueError('Unknown node error policy: ' + str(mode))
    exhausted = raw.get('exhausted', 'stop')
    if exhausted not in {'stop', 'skip', 'branch'}:
        raise ValueError('Unknown exhausted action')
    return dict(mode=mode, retries=max(0, min(100, int(raw.get('retries', 1)))),
                delay=max(0, min(3600, float(raw.get('delay', 0)))), exhausted=exhausted)


def error_ports(settings):
    p = policy(settings)
    return bool(p and (p['mode'] == 'branch' or p['mode'] == 'retry' and p['exhausted'] == 'branch'))


def result_error(result, tool=None):
    detector = getattr(tool, 'get_error', None)
    if callable(detector):
        error = detector(result)
        if error: return str(error)
    if isinstance(result, str) and result.lstrip().startswith('{'):
        import json
        try:
            decoded = json.loads(result)
            if isinstance(decoded, dict): return result_error(decoded)
        except (ValueError, TypeError): pass
    if isinstance(result, dict):
        if result.get('error'):
            return str(result['error'])
        if result.get('status') in {'error', 'failed', 'timed_out'} or result.get('success') is False or result.get('isError') is True:
            return str(result.get('message') or result)
    if isinstance(result, str) and result.startswith(('Ошибка', 'Error:', 'System Error', 'System: Error', 'КРИТИЧЕСКАЯ')):
        return result
    return None


def parallel_owners(pipeline, *, contracts=None):
    """Derive chain membership, never trust a stale serialized chain flag."""
    nodes = {n.node_id: n for n in pipeline.nodes}
    owners, handoffs = {}, {}
    for edge in pipeline.edges:
        if edge.source_port == HANDOFF: handoffs.setdefault(edge.source_id, []).append(edge)
    contracts = {} if contracts is None else contracts
    for n in pipeline.nodes:
        owner = n.settings.get('_parallel_parent')
        if owner:
            if owner not in nodes or nodes[owner].type_id != PARALLEL:
                raise ValueError(f'{n.node_id}: missing parallel container {owner}')
            owners[n.node_id] = owner
    for root, owner in list(owners.items()):
        current, seen = root, set()
        while True:
            if current in seen:
                raise ValueError('Subagent handoff cycle')
            seen.add(current)
            edges = handoffs.get(current, [])
            if not edges:
                break
            if len(edges) != 1:
                raise ValueError('Subagent handoff fan-out')
            target = edges[0].target_id
            if target in owners and target != root:
                raise ValueError('Subagent belongs to more than one parallel job')
            from .extensions import node_contract, connection_error
            kind = nodes[current].type_id
            if kind not in contracts: contracts[kind] = node_contract(kind)
            contract = contracts[kind]
            if (target not in nodes or HANDOFF not in contract.get('output_targets', {})
                    or connection_error(contract, HANDOFF, nodes[target].type_id, edges[0].target_port, (HANDOFF,))):
                raise ValueError('Handoff must connect subagent tool nodes')
            owners[target] = owner
            current = target
    return owners
