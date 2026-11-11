"""Stable host extension contracts. Each point defines its own composition rule."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Target:
    kind: str
    domain: str
    strategy: str = 'component'
    version: str = '1.0'
    provides: dict = field(default_factory=dict)


def host_targets():
    result = {}
    for name in ('chat', 'projects', 'files', 'board', 'paint', 'startup', 'graph', 'pipelines', 'module', 'log'):
        result['workspace.' + name] = Target('workspace', 'ui')
    for name in ('file_manager', 'sessions', 'tasks', 'subagents', 'mcp', 'terminal', 'web_search'):
        result['widget.' + name] = Target('widget', 'ui')
    result['topbar.main'] = Target('topbar', 'ui')
    for name in ('ui.topbar.left', 'ui.topbar.right', 'ui.chat.input.actions', 'ui.chat.message.actions'):
        result[name] = Target('slot', 'ui', 'slot')
    result['workspace.chat'] = Target('workspace', 'ui', provides={
        'ui.chat.input.actions': '1.0', 'ui.chat.message.actions': '1.0'})
    result['topbar.main'] = Target('topbar', 'ui', provides={
        'ui.topbar.left': '1.0', 'ui.topbar.right': '1.0'})
    result['ui.ready'] = Target('event', 'ui', 'event')
    result['core.ready'] = Target('event', 'core', 'event')
    result['ui.workspace.changed'] = Target('event', 'ui', 'event')
    result['core.agent'] = Target('core', 'core', provides={'core.run_events': '1.0'})
    result['core.run_events'] = Target('event', 'core', 'event')
    result['core.tool.execute'] = Target('operation', 'core', 'operation')
    result['core.model.providers'] = Target('core', 'core')
    result['ui.model.providers'] = Target('core', 'ui')
    result['core.plugin.services'] = Target('core', 'core')
    result['core.context.sections'] = Target('core', 'core')
    try:
        from core.paths import APP_PATHS
    except ModuleNotFoundError:
        return result  # Standalone repository tooling has no Toshy module tree.
    for category in ('system', 'custom'):
        for source in ((APP_PATHS.code_root / 'modules' if category == 'system' else APP_PATHS.modules) / category).glob('*/__init__.py'):
            if not source.parent.name.startswith('.'):
                result[f'module.{category}.{source.parent.name}'] = Target(category + '-module', 'core')
    return result


OPERATIONS = {
    'event': {'listen'},
    'operation': {'before', 'after', 'filter', 'around'},
    'slot': {'slot'},
    'component': set(),
}
