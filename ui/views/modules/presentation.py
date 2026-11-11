"""Translate built-in node captions at presentation boundaries only."""
from ui.i18n import i18n, tr
from .nodes.registry import BUILTIN_CATEGORIES


BUILTIN_CAPTIONS = set(BUILTIN_CATEGORIES) | {
    'graph.start', 'content.prompt', 'content.string', 'content.integer',
    'content.float', 'content.binary', 'content.array', 'core.module.memory',
}


def node_caption(spec, attribute='title'):
    source = getattr(spec, attribute)
    if (spec.type_id in BUILTIN_CAPTIONS
            or spec.type_id.startswith('system.tool.')):
        key = i18n.source_key(source)
        if key:
            return tr(key)
    return source


def metadata_caption(spec, name, default=''):
    source = str(spec.metadata.get(name) or default)
    if spec.type_id in BUILTIN_CAPTIONS:
        key = i18n.source_key(source)
        if key:
            return tr(key)
    return source


def category_caption(source):
    key = {'System modules':'nav.system_modules', 'Custom modules':'nav.custom_modules',
           'routing':'nodes.routing', 'controls':'nodes.controls',
           'processing':'nodes.processing', 'Arguments':'nodes.arguments'}.get(source)
    return tr(key) if key else source
