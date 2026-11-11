"""Full durable execution journal and a separate Loop context projection."""
from copy import deepcopy
from .control import GRAPH_LOOP_TYPE


def loop_bodies(pipeline):
    nodes = {n.node_id: n for n in pipeline.nodes}
    result = {}
    for loop in pipeline.nodes:
        if loop.type_id != GRAPH_LOOP_TYPE:
            continue
        start = [e.target_id for e in pipeline.edges if e.source_id == loop.node_id and e.source_port == 'exec:repeat']
        # Include the first pass leading into Loop, not just a later repeat.
        stack, body = list(start), set()
        while stack:
            current = stack.pop()
            if current == loop.node_id or current in body: continue
            body.add(current)
            stack.extend(e.target_id for e in pipeline.edges if e.source_id == current and e.source_port.startswith('exec:'))
        result[loop.node_id] = body | {loop.node_id}
    from .execution import parallel_owners
    owners = parallel_owners(pipeline)
    for body in result.values():
        body.update(nid for nid, owner in owners.items() if owner in body)
    return result


def enter(runner, node):
    for loop, body in runner.loop_bodies.items():
        if node.node_id not in body: continue
        state = runner.state.loop_scopes.setdefault(loop, {'cycle': 0, 'iteration': 0, 'closed': False})
        if state['closed']:
            state.update(cycle=state['cycle'] + 1, iteration=0, closed=False)
            runner.state.iterations[loop] = 0


def append(runner, record):
    scopes = {key: {'cycle': value['cycle'], 'iteration': value['iteration']}
              for key, value in runner.state.loop_scopes.items()
              if runner.state.current_node in runner.loop_bodies.get(key, ()) and not value['closed']}
    runner.state.journal.append(dict(deepcopy(record), node=runner.state.current_node, scopes=scopes))


def leave(runner, node, output):
    if node.type_id == GRAPH_LOOP_TYPE:
        scope = runner.state.loop_scopes.setdefault(node.node_id, {'cycle': 0, 'iteration': 0, 'closed': False})
        if output == 'exec:repeat': scope['iteration'] += 1
        else: scope['closed'] = True


def projection(runner):
    latest, final_records = {}, {}
    for record in runner.state.journal:
        for loop, scope in record.get('scopes', {}).items():
            key = (loop, scope['cycle'])
            latest[key] = max(latest.get(key, 0), scope['iteration'])
            if record.get('kind') == 'node': final_records[(*key, scope['iteration'])] = record
    result, summarized = [], set()
    for record in runner.state.journal:
        hide, summary = False, None
        for loop, scope in record.get('scopes', {}).items():
            mode = runner.nodes[loop].settings.get('context_retention', 'all')
            key = (loop, scope['cycle'])
            if scope['iteration'] < latest[key] and mode != 'all':
                hide = True
                if mode == 'results': summary = (*key, scope['iteration'])
                if mode == 'last': summary = None; break
        if not hide:
            result.append(deepcopy(record))
        elif summary and summary not in summarized:
            # One final result for each prior iteration; no hidden tool/model trace.
            source = final_records.get(summary)
            if source:
                result.append({'kind': 'iteration_result', 'loop': summary[0], 'cycle': summary[1],
                               'iteration': summary[2], 'result': deepcopy(source.get('result')), 'error': source.get('error')})
                summarized.add(summary)
    return result
