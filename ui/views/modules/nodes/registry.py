from __future__ import annotations

import importlib
import pkgutil
from collections import OrderedDict
from dataclasses import replace
from typing import Dict, Iterable, List, Protocol

from .specs import PipelineNodeSpec


# Category labels are presentation only; serialized node IDs stay unchanged.
BUILTIN_CATEGORIES = {
    'graph.router': 'routing',
    'graph.loop': 'routing',
    'graph.parallel': 'routing',
    'graph.human_in_loop': 'controls',
    'graph.notification': 'controls',
    'graph.end_subgraph': 'controls',
    'graph.shutdown': 'controls',
    'advanced.python_code': 'processing',
    'advanced.agent_decision': 'processing',
    'content.transform': 'processing',
    'pipeline.call': 'processing',
}


class NodeProvider(Protocol):
    provider_id: str

    def collect(self) -> Iterable[PipelineNodeSpec]:
        ...


class NodeRegistry:
    """Single source of truth for all node types visible in Modules.

    Built-in node files are discovered automatically inside the trusted
    ``nodes.builtins`` package. Dynamic sources (modules, MCP, etc.) are
    explicit providers and may be refreshed at runtime.
    """

    def __init__(self):
        self._builtins: "OrderedDict[str, PipelineNodeSpec]" = OrderedDict()
        self._providers: "OrderedDict[str, NodeProvider]" = OrderedDict()
        self._provider_specs: Dict[str, "OrderedDict[str, PipelineNodeSpec]"] = {}

    def register(self, spec: PipelineNodeSpec) -> None:
        if not isinstance(spec, PipelineNodeSpec):
            raise TypeError("registry accepts PipelineNodeSpec instances")
        if not spec.type_id:
            raise ValueError("node type_id must not be empty")
        category = BUILTIN_CATEGORIES.get(spec.type_id)
        if category is not None:
            spec = replace(spec, category_path=(category,))
        elif spec.type_id == 'content.prompt' or (spec.category_path and spec.category_path[0] in {
            'Graph', 'Logical nodes', 'Advanced', 'Advanced Nodes', 'Content nodes',
        }):
            spec = replace(spec, category_path=())
        self._builtins[spec.type_id] = spec

    def register_many(self, specs: Iterable[PipelineNodeSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def add_provider(self, provider: NodeProvider) -> None:
        provider_id = str(getattr(provider, "provider_id", "") or "").strip()
        if not provider_id:
            raise ValueError("node provider must define provider_id")
        self._providers[provider_id] = provider

    def discover_builtins(self) -> None:
        package = importlib.import_module("ui.views.modules.nodes.builtins")
        prefix = package.__name__ + "."
        names = sorted(
            item.name
            for item in pkgutil.walk_packages(package.__path__, prefix)
            if not item.ispkg
        )
        for module_name in names:
            module = importlib.import_module(module_name)
            register_nodes = getattr(module, "register_nodes", None)
            if callable(register_nodes):
                register_nodes(self)

    def refresh_providers(self) -> None:
        refreshed: Dict[str, "OrderedDict[str, PipelineNodeSpec]"] = {}
        for provider_id, provider in self._providers.items():
            bucket: "OrderedDict[str, PipelineNodeSpec]" = OrderedDict()
            try:
                specs = provider.collect()
            except Exception as exc:
                print(f"[Modules NodeRegistry] Provider '{provider_id}' failed: {exc}")
                specs = ()
            for spec in specs or ():
                if not isinstance(spec, PipelineNodeSpec) or not spec.type_id:
                    continue
                bucket[spec.type_id] = spec
            refreshed[provider_id] = bucket
        self._provider_specs = refreshed

    def specs(self) -> List[PipelineNodeSpec]:
        merged: "OrderedDict[str, PipelineNodeSpec]" = OrderedDict(self._builtins)
        for bucket in self._provider_specs.values():
            for type_id, spec in bucket.items():
                merged[type_id] = spec
        return list(merged.values())

    def get(self, type_id: str) -> PipelineNodeSpec | None:
        key = str(type_id or "")
        for spec in self.specs():
            if spec.type_id == key:
                return spec
        return None


def build_default_node_registry(mcp_owner_getter=None) -> NodeRegistry:
    # Imports stay local so registry.py itself remains independent from concrete
    # dynamic providers and can be reused by tests/extensions.
    from .providers.mcp import McpNodeProvider
    from .providers.modules import ModuleNodeProvider

    registry = NodeRegistry()
    registry.discover_builtins()
    registry.add_provider(ModuleNodeProvider())
    registry.add_provider(McpNodeProvider(owner_getter=mcp_owner_getter))
    registry.refresh_providers()
    return registry
