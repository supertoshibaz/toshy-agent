"""Content and directed connections only; Board geometry is UI metadata."""

def model_canvas_context(context):
    return {
        'schema': context.get('schema', 'toshy.canvas-context.v1'),
        'explicitNodes': list(context.get('explicitNodes') or []),
        'nodes': [{k: v for k, v in node.items() if k in {'id', 'type', 'text', 'file', 'url', 'label'}}
                  for node in context.get('nodes', []) if isinstance(node, dict)],
        'edges': [{k: v for k, v in edge.items() if k in {'id', 'fromNode', 'toNode', 'label', 'fromEnd', 'toEnd'}}
                  for edge in context.get('edges', []) if isinstance(edge, dict)],
    }
