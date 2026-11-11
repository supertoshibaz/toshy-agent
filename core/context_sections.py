"""Optional plugin-owned text partitions for context usage estimates."""
import re


def register(context, key, classifier, *, labels, color, tool_names=()):
    if not key.startswith(context.plugin_id + '.') or not callable(classifier):
        raise ValueError('Context section must own its plugin namespace.')
    if not isinstance(labels, dict) or not labels.get('en') or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('A context section needs English labels and a hex color.')
    sections = getattr(context.runtime, 'context_sections', None)
    if sections is None:
        context.runtime.context_sections = sections = {}
    if key in sections:
        raise ValueError('Context section already registered: ' + key)
    sections[key] = {'classifier': classifier, 'labels': dict(labels), 'color': color,
                     'tool_names': frozenset(tool_names)}
    context.register_cleanup(lambda: sections.pop(key, None))


def sections():
    from core.plugins.runtime import get_runtime
    return dict(getattr(get_runtime(), 'context_sections', {}))


def partition(text, default, registry):
    parts = [(default, text)]
    for key, section in registry.items():
        result = []
        for category, fragment in parts:
            if category in registry:
                result.append((category, fragment))
                continue
            try:
                split = list(section['classifier'](fragment))
                if len(split) > 4096 or ''.join(piece for _, piece in split) != fragment:
                    raise ValueError('Context classifier must preserve the complete input text.')
                result.extend((key if owned else category, piece) for owned, piece in split)
            except Exception:
                result.append((category, fragment))  # An estimate cannot interrupt inference.
        parts = result
    return parts
