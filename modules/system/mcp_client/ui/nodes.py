"""Owner-scoped cached MCP schemas, available even while a server is stopped."""
from __future__ import annotations
from ui.i18n import tr
from typing import Callable, Optional
from ui.views.modules.nodes.specs import PipelineNodeSpec, arg_in, arg_result, exec_in, exec_out, prompt_in


class McpNodeProvider:
    provider_id = 'mcp'

    def __init__(self, snapshot_getter=None, owner_getter: Optional[Callable] = None):
        self.snapshot_getter = snapshot_getter
        self.owner_getter = owner_getter or (lambda: 'global')

    def collect(self):
        pipeline_owner = self.owner_getter() or 'global'
        if self.snapshot_getter is not None:
            records = self.snapshot_getter() or []
        else:
            from modules.system.mcp_client.store import McpStore, tool_name
            store = McpStore()
            records = []
            # Keep cached schemas for loading existing graphs. Menu visibility
            # is scoped separately so moving a graph never deletes its nodes.
            for actual_owner, server in store.records():
                from modules.system.mcp_client import McpManager
                for tool in McpManager.all_tools(server):
                    if tool['name'] in server.get('disabled_tools', []):
                        continue
                    records.append({'server': server['id'], 'server_name': server['name'], 'owner': actual_owner,
                        'name': tool['name'], 'title': tool.get('title') or tool['name'], 'operation': tool.get('_operation', 'call_tool'), 'description': tool.get('description', ''),
                        'args_schema': tool.get('inputSchema', {}), 'runtime_name': tool_name(actual_owner, server['id'], tool['name'])})
        # Only add a server level when an owner has multiple visible servers.
        servers_by_owner = {}
        for raw in records:
            server, tool = str(raw.get('server') or ''), str(raw.get('name') or '')
            if server and tool:
                servers_by_owner.setdefault(str(raw.get('owner', 'global')), {})[server] = str(raw.get('server_name') or server)
        result = []
        for raw in records:
            server, tool = str(raw.get('server') or ''), str(raw.get('name') or '')
            if not server or not tool:
                continue
            schema = raw.get('args_schema') or {}
            runtime_name = raw.get('runtime_name') or tool
            owner = raw.get('owner', 'global')
            owner_name = raw.get('owner_name') or owner
            if not raw.get('owner_name'):
                from core.plugins.modules import module_locations
                from ui.views.modules.nodes.providers.modules import _module_display_name
                owner_dir = module_locations(category='custom').get(('custom', owner))
                if owner_dir is not None: owner_name = _module_display_name(owner_dir)
            servers = servers_by_owner[str(owner)]
            server_path = ()
            if len(servers) > 1:
                label = servers[server]
                if sum(name == label for name in servers.values()) > 1:
                    label = f'{label} ({server})'
                server_path = (label,)
            operation_group = ('Prompts' if raw.get('operation') == 'get_prompt' else
                'Resources' if raw.get('operation') in {'read_resource', 'list_resources', 'list_resource_templates'} else 'Tools')
            result.append(PipelineNodeSpec(
                type_id=f'mcp.tool.{server}.{runtime_name}', title=raw.get('title') or tool, kind='tool',
                category_path=('Custom modules', str(owner_name), *server_path, operation_group),
                description=raw.get('description', ''),
                inputs=(exec_in(), prompt_in(), *[arg_in(name, spec, required=name in schema.get('required', []))
                                                 for name, spec in schema.get('properties', {}).items()]),
                outputs=(exec_out(), arg_result()), implemented=True,
                metadata={'mcp_server': server, 'mcp_owner': owner, 'tool': runtime_name, 'args_schema': schema,
                    'library_hidden': pipeline_owner == 'global' or owner != pipeline_owner,
                    'default_settings': {'_mcp_owner': owner, '_mcp_server': server, '_mcp_tool': tool}}))
        from ui.views.modules.nodes.specs import PipelinePortSpec, prompt_node_out
        result.append(PipelineNodeSpec(type_id='content.mcp_prompt', title='MCP messages → Prompt',
            kind='content', category_path=('Custom modules', 'MCP'),
            description=tr('ui.caption.5ac2ce9ca799'),
            inputs=(PipelinePortSpec('arg:messages', 'Arg · messages', 'arg'),),
            outputs=(prompt_node_out(),), implemented=True))
        return result
