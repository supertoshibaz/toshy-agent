"""Assign a tree selection without leaving overlapping terminal ownership."""
def catalog_nodes(catalog):
    folders = catalog.get('folders', {})
    if isinstance(folders, list):
        folders = {f['id']:f for f in folders}
    locations = catalog.get('session_folders', {})
    children = {}
    for identity, folder in folders.items():
        parent = folder.get('parent_id')
        children.setdefault(parent, []).append({'kind':'folder' if parent else 'project', 'target':identity})
    for identity, folder in locations.items():
        children.setdefault(folder, []).append({'kind':'session', 'target':identity})
    return folders, locations, children


def covers(parent, child, catalog):
    if parent == child:
        return True
    if parent['kind'] == 'session':
        return False
    folders, locations, _ = catalog_nodes(catalog)
    current = locations.get(child['target']) if child['kind'] == 'session' else child['target']
    visited = set()
    while current and current not in visited:
        if current == parent['target']:
            return True
        visited.add(current)
        current = folders.get(current, {}).get('parent_id')
    return False


def subtract(selection, claimed, catalog):
    _, _, children = catalog_nodes(catalog)
    def visit(node, seen):
        marker = (node['kind'],node['target'])
        if marker in seen or any(covers(other,node,catalog) for other in claimed):
            return []
        if not any(covers(node,other,catalog) for other in claimed):
            return [node]
        result = []
        for child in children.get(node['target'],[]) if node['kind'] != 'session' else []:
            result.extend(visit(child, seen|{marker}))
        return result
    return [node for selected in selection for node in visit(selected,set())]
