"""Indexed, iterative structural checks shared by runtime and editor."""
from collections import defaultdict
from .control import GRAPH_LOOP_TYPE


class GraphIndex:
    def __init__(self, pipeline):
        self.nodes = {n.node_id: n for n in pipeline.nodes}
        self.outgoing, self.incoming = defaultdict(list), defaultdict(list)
        self.by_input, self.by_output = defaultdict(list), defaultdict(list)
        for edge in pipeline.edges:
            self.outgoing[edge.source_id].append(edge)
            self.incoming[edge.target_id].append(edge)
            self.by_input[edge.target_id,edge.target_port].append(edge)
            self.by_output[edge.source_id,edge.source_port].append(edge)

    def adjacency(self, live, *, data=False, omit_repeat=False):
        result = {key: [] for key in live}
        for key in result:
            for edge in self.outgoing.get(key, ()):
                if edge.target_id not in result: continue
                if edge.source_port.startswith('exec:') == data: continue
                if omit_repeat and edge.source_port == 'exec:repeat' and self.nodes[key].type_id == GRAPH_LOOP_TYPE: continue
                result[key].append(edge.target_id)
        return result


def first_cycle(adjacent):
    """A DFS with explicit frames: O(V+E), no dependency on recursion depth."""
    color = {}
    for root in adjacent:
        if color.get(root): continue
        color[root] = 1
        path, positions = [root], {root:0}
        stack = [iter(adjacent[root])]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                color[path.pop()] = 2
                positions.popitem()
                stack.pop()
            elif color.get(child) == 1:
                return path[positions[child]:] + [child]
            elif not color.get(child):
                color[child] = 1
                positions[child] = len(path)
                path.append(child)
                stack.append(iter(adjacent.get(child, ())))
    return []


def parallel_data_issues(pipeline, *, index=None, owners=None):
    from .contracts import is_data
    from .execution import parallel_owners, HANDOFF
    index = index or GraphIndex(pipeline)
    owners = parallel_owners(pipeline) if owners is None else owners
    issues = []
    for target, owner in owners.items():
        predecessors, current = set(), target
        while True:
            edge = next((e for e in index.incoming.get(current, ()) if e.source_port == HANDOFF), None)
            if edge is None or edge.source_id in predecessors: break
            predecessors.add(edge.source_id); current = edge.source_id
        for inlet in index.incoming.get(target, ()):
            if inlet.source_port.startswith('exec:'): continue
            todo, seen = [inlet.source_id], set()
            while todo:
                source = todo.pop()
                if source in seen or source not in index.nodes: continue
                seen.add(source)
                if source == owner:
                    issues.append(dict(node_id=target, source_id=owner, edge=inlet,
                        message='Инструмент зависит от результата собственного незавершённого блока.'))
                elif is_data(index.nodes[source]):
                    todo.extend(e.source_id for e in index.incoming.get(source, ()) if not e.source_port.startswith('exec:'))
                elif source in owners and source not in predecessors:
                    issues.append(dict(node_id=target, source_id=source, edge=inlet,
                        message='Задание зависит от ещё не завершённого параллельного вызова.'))
    return issues


def unfinished_exec(pipeline, *, index=None, ports=None, owners=None, live=None):
    from .contracts import port_sets
    from .execution import parallel_owners, HANDOFF
    index = index or GraphIndex(pipeline)
    owners = parallel_owners(pipeline) if owners is None else owners
    result,contracts = {},{}
    from .extensions import node_contract
    for key,node in index.nodes.items():
        if key in owners or live is not None and key not in live: continue
        try:
            if node.type_id not in contracts: contracts[node.type_id]=node_contract(node.type_id)
            outputs = ports[key][1] if ports is not None and key in ports else port_sets(node,contract=contracts[node.type_id])[1]
        except (ValueError,TypeError): continue
        # Alternative/spare Exec ports are not unfinished branches when the
        # card already has a continuation (Router also grows a spare port).
        if any(e.source_port in outputs and e.source_port.startswith('exec:') and e.target_id in index.nodes
               for e in index.outgoing.get(key, ())):continue
        for port in sorted(outputs):
            if not port.startswith('exec:') or port == HANDOFF: continue
            if port == 'exec:out' and index.by_output.get((key,HANDOFF)): continue
            if not index.by_output.get((key,port)): result.setdefault(key, []).append(port)
    return result
