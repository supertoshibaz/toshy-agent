"""Детерминированное размещение звёзд и ориентированных графов без физики."""
import math

GRAPH_NODE_SIDE = 104.
GRAPH_MODULE_SIDE = 128.
GRAPH_CORE_SIDE = 160.


def ring_centers(radii, center_radius=100., gap=60.):
    if not radii:
        return []
    def positions(radius):
        half = [math.asin(min(.99999, (value + gap/2) / radius)) for value in radii]
        total = 2 * sum(half)
        if total > 2 * math.pi:
            return None
        extra = (2 * math.pi - total) / len(radii)
        angle = -math.pi/2
        result = []
        for h in half:
            angle += h + extra/2
            result.append((radius * math.cos(angle), radius * math.sin(angle)))
            angle += h + extra/2
        return result
    low = max(center_radius + max(radii) + gap, max(radii) + gap)
    high = low
    while positions(high) is None:
        high *= 1.5
    if high > low:
        for _ in range(40):
            mid = (low + high)/2
            if positions(mid) is None: low = mid
            else: high = mid
    return positions(high)


def pipeline_positions(nodes, edges, *, sizes=None):
    """Сначала компоненты связности циклов, затем слои направленного графа."""
    ids = sorted(nodes)
    adjacent = {key: set() for key in ids}
    reverse = {key: set() for key in ids}
    for a, b in edges:
        if a in adjacent and b in adjacent:
            adjacent[a].add(b)
            reverse[b].add(a)
    # Итеративный обход не ограничивает размер графа глубиной стека Python.
    seen, order = set(), []
    for root in ids:
        if root in seen: continue
        stack = [(root, False)]
        while stack:
            key, done = stack.pop()
            if done:
                order.append(key)
            elif key not in seen:
                seen.add(key)
                stack.append((key, True))
                stack.extend((child, False) for child in sorted(adjacent[key], reverse=True) if child not in seen)
    groups, component = [], {}
    for root in reversed(order):
        if root in component: continue
        number = len(groups)
        group, stack = [], [root]
        component[root] = number
        while stack:
            key = stack.pop()
            group.append(key)
            for child in sorted(reverse[key]):
                if child not in component:
                    component[child] = number
                    stack.append(child)
        groups.append(sorted(group))
    links = {i: set() for i in range(len(groups))}
    degree = {i: 0 for i in links}
    for a in ids:
        for b in adjacent[a]:
            x, y = component[a], component[b]
            if x != y and y not in links[x]:
                links[x].add(y); degree[y] += 1
    queue = sorted(i for i in links if degree[i] == 0)
    rank = dict.fromkeys(links, 0)
    for i in queue:
        for child in sorted(links[i]):
            rank[child] = max(rank[child], rank[i] + 1)
            degree[child] -= 1
            if degree[child] == 0: queue.append(child)
    columns = {}
    for number, group in enumerate(groups):
        columns.setdefault(rank[number], []).extend(group)
    result, right = {}, 0.
    sizes = sizes or {}
    for rank, column in sorted(columns.items()):
        # Barycentre ordering reduces crossings while IDs keep ties deterministic.
        def order_key(key):
            parents = [result[p][1] for p in reverse[key] if p in result]
            return (sum(parents)/len(parents) if parents else 0., key)
        column = sorted(column, key=order_key)
        width = max(sizes.get(key, (GRAPH_NODE_SIDE,GRAPH_NODE_SIDE))[0] for key in column)
        x = right+width/2
        height = sum(sizes.get(key, (GRAPH_NODE_SIDE,GRAPH_NODE_SIDE))[1] for key in column)+63.*(len(column)-1)
        y = -height/2
        for key in column:
            h = sizes.get(key, (GRAPH_NODE_SIDE,GRAPH_NODE_SIDE))[1]
            result[key] = (x, y+h/2)
            y += h+63.
        right += width+108.
    return result


def box_bounds(boxes):
    values = list(boxes.values())
    return (min(x-w/2 for x,y,w,h in values), min(y-h/2 for x,y,w,h in values),
            max(x+w/2 for x,y,w,h in values), max(y+h/2 for x,y,w,h in values))


def radial_boxes(groups, *, gap=65.):
    """Pack upright rectangular branches outward, without circular reservations.

    Length along a branch never inflates its distance from the centre. AABB
    reservations are conservative, deterministic, and prevent card overlaps.
    Quarter turns keep container frames and their contents aligned.
    """
    core_margin = GRAPH_CORE_SIDE/2+15.
    result, occupied = {}, [(-core_margin,-core_margin,core_margin,core_margin)]
    for index, (_, boxes) in enumerate(groups):
        angle = -math.pi/2 + 2*math.pi*index/max(1,len(groups))
        turns = int(round(angle/(math.pi/2))) % 4
        cosine, sine = ((1,0),(0,1),(-1,0),(0,-1))[turns]
        oriented = {key: (cosine*x-sine*y, sine*x+cosine*y, h if turns%2 else w, w if turns%2 else h)
                    for key,(x,y,w,h) in boxes.items()}
        bounds = box_bounds(oriented)
        dx,dy = math.cos(angle),math.sin(angle)
        distance = 245.
        while True:
            left,top,right,bottom = (bounds[0]+dx*distance,bounds[1]+dy*distance,bounds[2]+dx*distance,bounds[3]+dy*distance)
            collision = next((b for b in occupied if left < b[2]+gap and right > b[0]-gap and top < b[3]+gap and bottom > b[1]-gap),None)
            if collision is None: break
            moves = []
            if dx > 1e-8: moves.append((collision[2]+gap-left)/dx)
            elif dx < -1e-8: moves.append((right-collision[0]+gap)/-dx)
            if dy > 1e-8: moves.append((collision[3]+gap-top)/dy)
            elif dy < -1e-8: moves.append((bottom-collision[1]+gap)/-dy)
            distance += max(1.,min(moves))+1.
        occupied.append((left,top,right,bottom))
        result.update({key: (x+dx*distance,y+dy*distance,w,h) for key,(x,y,w,h) in oriented.items()})
    return result


def star_positions(groups):
    """Каждая группа содержит локальные позиции узлов вокруг своего модуля."""
    sizes = [max((math.hypot(x, y) + radius for x, y, radius in values.values()), default=80.)
             for _, values in groups]
    result = {}
    for (_, values), (cx, cy) in zip(groups, ring_centers(sizes, center_radius=110.)):
        result.update({key: (cx+x, cy+y) for key, (x, y, _) in values.items()})
    return result
