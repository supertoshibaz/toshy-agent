"""MCP-owned package metadata and explicit graph dependencies."""
import json
from pathlib import Path
from .store import McpStore, tool_name


def sync_package(store, module, data):
    servers = {}
    for path in (store.path(module) / 'mcp').glob('*/server.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('schema_version') == 1:
                servers[record['id']] = {'path': f"mcp/{record['id']}", 'source': record.get('source', {})}
        except (OSError, ValueError, KeyError):
            continue
    data['mcp_servers'] = servers
    deps = set(data.get('dependencies', [])) - {module}
    if servers:
        deps.add('mcp_client')
    elif not any('mcp_client' in p.get('dependencies', []) for p in data.get('pipelines', {}).values()):
        deps.discard('mcp_client')
    data['dependencies'] = sorted(deps)


def copy_bindings(store, module, definition, cleanup):
    """Keep server ownership explicit; copying graphs never copies environments."""
    mcp_store = McpStore(store.root.parent)
    for node in definition.nodes:
        if not node.type_id.startswith('mcp.tool.'):
            continue
        owner = node.settings.get('_mcp_owner')
        sid = node.settings.get('_mcp_server')
        if not owner or not sid or not node.settings.get('_mcp_tool'):
            raise ValueError('Обновите MCP-ноду: отсутствует владелец или сервер')
        mcp_store.read(owner, sid)


def node_identity(node):
    if node.type_id.startswith('mcp.tool.'):
        return 'mcp_client', '.'.join(node.type_id.split('.')[3:])


def node_dependencies(node):
    owner = node.settings.get('_mcp_owner')
    return {owner} if node.type_id.startswith('mcp.tool.') and owner not in {None, '', 'global'} else set()

