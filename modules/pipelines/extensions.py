"""Optional module contributions to package manifests and graph bindings."""
import importlib
import importlib.util
import hashlib
import sys
import types
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from core.paths import APP_PATHS


def package_extensions():
    from core.plugins.modules import module_locations
    for (_, name), folder in sorted(module_locations(category='system').items()):
        if (folder / 'package_extension.py').is_file():
            yield importlib.import_module(f'modules.system.{name}.package_extension')


def extension_identity(node):
    for extension in package_extensions():
        handler = getattr(extension, 'node_identity', None)
        value = handler(node) if handler else None
        if value:
            return value
    return None, None


def extension_dependencies(node):
    result = set()
    for extension in package_extensions():
        handler = getattr(extension, 'node_dependencies', None)
        if handler:
            result.update(handler(node))
    return result


@dataclass
class ToolOutcome:
    result: Any
    output: str | None = 'exec:out'
    values: dict = field(default_factory=dict)
    error: Any = None
    halt: bool = False


_node_payload = ContextVar('pipeline_node_payload', default=None)


@contextmanager
def node_payload_scope(payload):
    token = _node_payload.set(payload)
    try:
        yield
    finally:
        _node_payload.reset(token)


def current_node_payload():
    return _node_payload.get()


def node_extensions():
    """Import lightweight contracts without activating their owning modules."""
    from core.plugins.modules import module_locations
    for _, folder in sorted(module_locations().items()):
        source = folder / 'pipeline_extension.py'
        if not source.is_file():
            continue
        prefix = '_toshy_pipeline_' + hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:20]
        name = prefix + '.pipeline_extension'
        if name not in sys.modules:
            package = types.ModuleType(prefix)
            package.__path__ = [str(folder)]
            package.__package__ = prefix
            package.__spec__ = importlib.util.spec_from_loader(prefix, loader=None, is_package=True)
            sys.modules[prefix] = package
            spec = importlib.util.spec_from_file_location(name, source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        yield sys.modules[name]


def node_extension(type_id):
    for extension in node_extensions():
        handler = getattr(extension, 'node_contract', None)
        contract = handler(type_id) if handler else None
        if contract:
            return extension, contract
    return None, {}


def node_contract(type_id):
    return node_extension(type_id)[1]


def connection_error(contract, source_port, target_type, target_port, used_outputs=()):
    used_outputs = tuple(used_outputs)
    for group in contract.get('exclusive_outputs', ()):
        if source_port in group and any(port != source_port and port in group for port in used_outputs):
            return 'Взаимоисключающие Exec-выходы нельзя подключить одновременно'
    target = contract.get('output_targets', {}).get(source_port)
    if target and (target_type not in target['types'] or target_port not in target['ports']):
        return 'Этот выход можно подключить только к Exec-входу совместимой ноды'
    return None


async def execute_tool_node(node, runner, arguments):
    extension, _ = node_extension(node.type_id)
    handler = getattr(extension, 'execute_tool_node', None)
    return await handler(node, runner, arguments) if handler else None
