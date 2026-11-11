"""Shared category names and colours for structured application events."""
import logging


class UiPollAccessFilter(logging.Filter):
    """Skip successful UI polling access lines, preserving failed requests."""
    POLL_PATHS = frozenset({
        '/v1/subagents', '/v1/models', '/v1/projects', '/v1/graph', '/v1/pipelines/pending',
        '/v1/modules/mcp_client/service',
        '/v1/modules/terminal_sandbox/service',
    })

    def filter(self, record):
        # Uvicorn supplies (client, method, path+query, HTTP version, status).
        # Unknown record formats pass through rather than hiding diagnostics.
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, target, _, status = args
        try:
            status = int(status)
        except (TypeError, ValueError):
            return True
        path = str(target).partition('?')[0]
        terminal_output = path.startswith('/v1/modules/terminal_sandbox/service/jobs/')
        token_usage = path.startswith('/v1/sessions/') and path.endswith('/token-usage')
        terminal_feed = method == 'POST' and path in {'/v1/modules/terminal_sandbox/service/feed', '/v1/modules/terminal_sandbox/service/terminal-feed'}
        return not (((method == 'GET' and (path in self.POLL_PATHS or terminal_output or token_usage)) or terminal_feed)
                    and 200 <= status < 300)


CATEGORIES = {
    'generation': '#22c55e', 'model': '#38bdf8', 'context': '#a78bfa',
    'tools': '#fbbf24', 'pipelines': '#fb923c', 'subagents': '#f472b6',
    'memory': '#2dd4bf', 'modules': '#818cf8', 'storage': '#a3e635',
    'resources': '#60a5fa', 'stats': '#c084fc', 'graph': '#94a3b8',
    'app': '#d4d4d8',
}


def category_for(tag):
    tag = tag.upper()
    for prefix, category in [('GRAPH', 'graph'), ('SUBAGENT', 'subagents'),
        ('TERMINAL', 'tools'), ('MCP', 'tools'), ('WEB', 'tools'), ('SEARCH', 'tools'), ('CRAWL', 'tools'),
        ('PIPELINE', 'pipelines'), ('LLM', 'model'), ('TOOL', 'tools'),
        ('EMBED', 'memory'), ('MEMORY', 'memory'), ('KB', 'memory'),
        ('MODULE', 'modules'), ('SESSION', 'storage'), ('CHAT', 'generation'),
        ('LSP', 'resources'), ('FILE', 'storage'), ('TAB', 'resources')]:
        if tag.startswith(prefix):
            return category
    return 'app'
