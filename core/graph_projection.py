"""Данные кастомных пайплайнов для обзора, без выполнения и миграции файлов."""
from functools import lru_cache
import hashlib
import re
from modules.pipelines.packages import PackageStore
from modules.pipelines.storage import load_pipeline_script


@lru_cache(maxsize=128)
def _read_definition(path, signature):
    return load_pipeline_script(path, migrate=False)


def attach_pipelines(snapshot):
    store = PackageStore()
    for module in snapshot.get('modules', []):
        if module.get('category') == 'Системный':
            continue
        try:
            package = store.read(module['name'])
        except (ValueError, OSError):
            continue
        module['display_name'] = package.get('name') or module['name']
        module['color'] = package.get('color') or '#ec4899'
        module['pipelines'] = []
        for identity, entry in sorted(package.get('pipelines', {}).items()):
            item = {'id': identity, 'name': entry.get('name') or identity, 'mode': entry.get('mode'),
                    'description': entry.get('description') or '', 'nodes': [], 'edges': []}
            try:
                path = store.resolve(module['name'], identity) / 'pipeline.py'
                stamp = path.stat()
                definition = _read_definition(path, (stamp.st_mtime_ns, stamp.st_size, stamp.st_ino))
                tool_name = f"pipeline_{module['name']}_{identity}"
                if len(tool_name) > 64 or not re.fullmatch(r'[A-Za-z0-9_-]+', tool_name):
                    label = re.sub(r'[^A-Za-z0-9_]+', '_', module['name']).strip('_') or 'module'
                    digest = hashlib.sha256(f"{module['name']}\0{identity}".encode()).hexdigest()[:16]
                    tool_name = f'pipeline_{label[:30]}_{digest}'
                item['tool_name'] = tool_name if entry.get('mode') == 'tool' else None
                ids = {node.node_id: str(index) if definition.generated_node_ids else node.node_id
                       for index, node in enumerate(definition.nodes)}
                item['nodes'] = [{'id': ids[node.node_id], 'type': node.type_id,
                                  'title': node.settings.get('title') or node.type_id,
                                  'parallel_parent': ids.get(node.settings.get('_parallel_parent'), ''),
                                  'parallel_order': node.settings.get('_parallel_order', 0),
                                  'parallel_mode': node.settings.get('_parallel_mode', 'concurrent'),
                                  'pipeline': str(node.settings.get('pipeline') or '') if node.type_id == 'pipeline.call' else ''}
                                 for node in definition.nodes]
                item['edges'] = [{'source': ids[edge.source_id], 'target': ids[edge.target_id],
                                  'source_port': edge.source_port, 'target_port': edge.target_port}
                                 for edge in definition.edges]
            except (ValueError, OSError, RuntimeError, KeyError) as error:
                item['error'] = str(error)
            module['pipelines'].append(item)
    return snapshot
