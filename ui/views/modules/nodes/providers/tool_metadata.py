"""Read constructor-forwarded tool metadata without running module code."""
from __future__ import annotations

import ast

UNKNOWN = object()


def _value(node, bindings):
    if isinstance(node, ast.Name):
        return bindings.get(node.id, UNKNOWN)
    if isinstance(node, ast.Dict):
        result = {}
        typed_schema = any(isinstance(key, ast.Constant) and key.value == 'type'
                           and isinstance(value, ast.Constant) and value.value in ('string', 'integer', 'number', 'boolean')
                           for key, value in zip(node.keys, node.values))
        for key, item in zip(node.keys, node.values):
            key = _value(key, bindings)
            value = _value(item, bindings)
            # Dynamic enum choices are optional UI hints. Keep the argument's
            # type/required flag; the live tool still validates actual choices.
            if typed_schema and key == 'enum' and value is UNKNOWN:
                continue
            if key is UNKNOWN or value is UNKNOWN:
                return UNKNOWN
            try:
                result[key] = value
            except TypeError:
                return UNKNOWN
        return result
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_value(item, bindings) for item in node.elts]
        if any(value is UNKNOWN for value in values):
            return UNKNOWN
        return tuple(values) if isinstance(node, ast.Tuple) else values
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {'list', 'tuple'} and len(node.args) == 1 and not node.keywords):
        value = _value(node.args[0], bindings)
        if isinstance(value, (list, tuple, dict, str)):
            return list(value) if node.func.id == 'list' else tuple(value)
        return UNKNOWN
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return UNKNOWN


def _constructor(cls):
    return next((node for node in cls.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '__init__'), None)


def _bind(function, arguments, keywords):
    positional = [*function.args.posonlyargs, *function.args.args]
    # __init__ is an instance method. Its first formal parameter is the instance.
    positional = positional[1:]
    result = {arg.arg: UNKNOWN for arg in positional}
    defaults = function.args.defaults
    for arg, default in zip(positional[len(positional) - len(defaults):], defaults):
        result[arg.arg] = _value(default, {})
    for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        result[arg.arg] = _value(default, {})
    if len(arguments) > len(positional):
        return None
    for arg, value in zip(positional, arguments):
        result[arg.arg] = value
    for name, value in keywords.items():
        if name not in result:
            return None
        result[name] = value
    return result


def _is_super(call):
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == '__init__'
            and isinstance(func.value, ast.Call) and isinstance(func.value.func, ast.Name)
            and func.value.func.id == 'super' and not func.value.args and not func.value.keywords)


def _metadata(arguments, keywords):
    values = dict(zip(('name', 'description', 'args_schema'), arguments))
    values.update(keywords)
    name, schema = values.get('name'), values.get('args_schema')
    if not isinstance(name, str) or not name.strip() or not isinstance(schema, dict):
        return None
    description = values.get('description', '')
    return {'name': name.strip(), 'description': description if isinstance(description, str) else '',
            'args_schema': schema}


def tool_metadata(cls, classes, bindings=None, stack=()):
    """Follow only local single-inheritance constructors and BaseTool calls.

    Unknown expressions stay unknown. Calls other than literal list/tuple
    construction are never evaluated, and imported modules are never loaded.
    """
    if cls.name in stack or len(stack) >= 12:
        return
    stack = (*stack, cls.name)
    function = _constructor(cls)
    if function is None:
        return
    bindings = {} if bindings is None else bindings
    for call in ast.walk(function):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute) or call.func.attr != '__init__':
            continue
        arguments = [_value(arg, bindings) for arg in call.args]
        keywords = {arg.arg: _value(arg.value, bindings) for arg in call.keywords if arg.arg is not None}
        # Existing keyword declarations remain discoverable, including those
        # whose base class is defined outside the module's own source files.
        direct = _metadata([], keywords)
        if direct is not None:
            yield direct
            continue
        if not _is_super(call) or len(cls.bases) != 1 or not isinstance(cls.bases[0], ast.Name):
            continue
        base_name = cls.bases[0].id
        if base_name == 'BaseTool':
            direct = _metadata(arguments, keywords)
            if direct is not None:
                yield direct
        elif base_name in classes:
            parent = classes[base_name]
            constructor = _constructor(parent)
            if constructor is not None:
                forwarded = _bind(constructor, arguments, keywords)
                if forwarded is not None:
                    yield from tool_metadata(parent, classes, forwarded, stack)
