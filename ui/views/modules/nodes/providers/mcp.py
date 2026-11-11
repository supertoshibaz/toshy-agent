"""Optional module-owned node provider entry point."""
try:
    from modules.system.mcp_client.ui.nodes import McpNodeProvider
except ImportError:
    class McpNodeProvider:
        provider_id = 'mcp'
        def __init__(self, **kwargs):
            pass
        def collect(self):
            return []
