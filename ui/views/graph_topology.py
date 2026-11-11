"""Qt-free execution-only projection and sized units for the graph overview."""
from ui.i18n import tr
from modules.pipelines.execution import PARALLEL, HANDOFF
from modules.pipelines.contracts import SCALARS
from .graph_layout import pipeline_positions,GRAPH_NODE_SIDE


TITLES = {
    'graph.start': 'Start', 'graph.end_subgraph': tr('ui.caption.846d02e8334d'), 'graph.shutdown': 'Shutdown',
    'graph.router': 'Router', 'graph.loop': 'Loop', 'graph.human_in_loop': 'Human in loop',
    'graph.notification': tr('ui.caption.1a3cc6cdef6e'), PARALLEL: tr('ui.caption.7c82d6876f44'),
    'content.transform': tr('ui.caption.39ed43979f8d'), 'pipeline.call': 'Nested Pipeline',
    'advanced.python_code': 'Python Code', 'advanced.agent_decision': tr('ui.caption.98387c5b3e12'),
}


def node_title(node):
    title, kind = node.get('title'), node['type']
    if title and title != kind: return title
    if kind in TITLES: return TITLES[kind]
    if kind.endswith(('.delegate_to_subagent', '.delegate_to_subagents')): return tr('ui.caption.55bf9af501f8')
    return kind.rsplit('.', 1)[-1]


def execution_topology(pipeline):
    nodes = {n['id']: n for n in pipeline.get('nodes', ())
             if n['type'] not in SCALARS and n['type'] not in {'content.prompt', 'content.mcp_prompt'}}
    edges = [e for e in pipeline.get('edges', ()) if e.get('source') in nodes and e.get('target') in nodes
             and e.get('source_port', '').startswith('exec:') and e.get('target_port', '').startswith('exec:')]
    children, outgoing = {}, {}
    for node in nodes.values():
        if node.get('parallel_parent') in nodes:
            children.setdefault(node['parallel_parent'], []).append(node['id'])
    for edge in edges: outgoing.setdefault(edge['source'], []).append(edge)
    live, pending = set(), [n['id'] for n in nodes.values() if n['type'] == 'graph.start']
    while pending:
        key = pending.pop()
        if key in live: continue
        live.add(key)
        pending.extend(children.get(key, ()))
        pending.extend(e['target'] for e in outgoing.get(key, ()))
    return {key: node for key, node in nodes.items() if key in live}, [e for e in edges if e['source'] in live and e['target'] in live]


def pipeline_cluster(pipeline, *, padding=24.):
    """Return node boxes relative to the pipeline's entry, including containers.

    A parallel unit reserves its direct tool cards and external handoff chains;
    ordinary control flow resumes beyond the whole reserved unit.
    """
    nodes, edges = execution_topology(pipeline)
    owned, direct = {}, {}
    for node in nodes.values():
        parent = node.get('parallel_parent')
        if parent in nodes and nodes[parent]['type'] == PARALLEL:
            direct.setdefault(parent, []).append(node)
            owned[node['id']] = parent
    handoffs = {e['source']: e['target'] for e in edges if e['source_port'] == HANDOFF}
    units, sizes = {}, {}
    for key, container in nodes.items():
        if container['type'] != PARALLEL: continue
        children = sorted(direct.get(key, ()), key=lambda n: (int(n.get('parallel_order', 0)), n['id']))
        side = GRAPH_NODE_SIDE
        frame_width = side+2*padding
        height = side*max(1,len(children))+padding*max(0,len(children)-1)+2*padding
        parts = {key: (0., 0., frame_width, height)}
        max_right = frame_width/2
        reserved_height = height
        for index, child in enumerate(children):
            y = -height/2 + padding+side/2 + index*(side+padding)
            parts[child['id']] = (0., y, side, side)
            current, chain, seen = child['id'], 0, {child['id']}
            while current in handoffs and handoffs[current] not in seen:
                current = handoffs[current]
                if current in owned: break  # Invalid old graph: do not draw a card twice.
                seen.add(current)
                owned[current] = key
                chain += 1
                x = frame_width/2+75.+side/2+(chain-1)*(side+75.)
                # Keep the handoff chain clear of the container's ordinary
                # continuation, which runs along the unit's centre line.
                clearance = side/2+padding+12.
                chain_y = y if abs(y) >= clearance else (clearance if y >= 0 else -clearance)
                parts[current] = (x, chain_y, side, side)
                reserved_height = max(reserved_height,2*(abs(chain_y)+side/2))
                max_right = max(max_right, x+side/2)
        width = frame_width/2+max_right
        # Unit centre may differ from the container centre when handoffs extend it.
        offset = (max_right-frame_width/2)/2
        units[key] = {nid: (x-offset, y, w, h) for nid, (x, y, w, h) in parts.items()}
        sizes[key] = (width, reserved_height)
    for key in nodes:
        if key not in units and key not in owned:
            units[key] = {key: (0., 0., GRAPH_NODE_SIDE, GRAPH_NODE_SIDE)}
            sizes[key] = (GRAPH_NODE_SIDE, GRAPH_NODE_SIDE)
    logical = [(e['source'], e['target']) for e in edges if e['source'] in units and e['target'] in units]
    positions = pipeline_positions(list(units), logical, sizes=sizes)
    boxes = {}
    for key, parts in units.items():
        x, y = positions[key]
        for nid, (dx, dy, width, height) in parts.items(): boxes[nid] = (x+dx+180., y+dy, width, height)
    return nodes, edges, boxes
