from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from core.memory.descriptor import MEMORY_MODULE_COLOR, MEMORY_MODULE_DISPLAY_NAME, MEMORY_MODULE_ID

from ..specs import PipelineNodeSpec, PipelinePortSpec, arg_in, arg_result, exec_in, exec_out, prompt_in


CATEGORY_PATH = ("System modules", "Memory")


# Memory is an always-on core system module rather than an enable/disable package
# under modules/system. Its pipeline-facing module/tool specs live here so the
# node registry can expose the same hierarchy without making Memory toggleable.
# Tool names and argument names intentionally match core/memory/tools.py.
_MEMORY_TOOLS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "remember_this",
        "description": "Save an important fact, rule, or preference to permanent memory.",
        "args": (
            ("text", {"type": "string"}, True),
        ),
    },
    {
        "name": "forget_this",
        "description": "Delete one permanent-memory fact by its ID.",
        "args": (
            ("fact_id", {"type": "string"}, True),
        ),
    },
    {
        "name": "search_memory",
        "description": "Search permanent vector memory for facts relevant to a semantic query.",
        "args": (
            ("query", {"type": "string"}, True),
        ),
    },
    {
        "name": "list_memories",
        "description": "List all saved permanent-memory facts and their IDs.",
        "args": (),
    },
    {
        "name": "search_temp_memory",
        "description": (
            "Search the current session temporary memory. Provide both semantic_query and keywords; "
            "runtime uses the representation required by the active memory mode."
        ),
        "args": (
            ("semantic_query", {"type": "string"}, True),
            ("keywords", {"type": "array", "items": {"type": "string"}}, True),
        ),
    },
    {
        "name": "list_knowledge_bases",
        "description": "Return the current catalog of knowledge bases available to this run.",
        "args": (),
    },
    {
        "name": "search_knowledge_base",
        "description": (
            "Search one or more explicitly selected knowledge bases. Databases selected in the inspector "
            "are fixed by the graph; leave the selection empty when database choice should remain a "
            "runtime/model argument."
        ),
        "args": (
            ("databases", {"type": "array", "items": {"type": "string"}}, True),
            ("semantic_query", {"type": "string"}, True),
            ("keywords", {"type": "array", "items": {"type": "string"}}, True),
        ),
        "metadata": {
            "inspector_type": "knowledge_search",
            "default_settings": {"databases": []},
            "fixed_args_from_settings": {"databases": "databases"},
        },
    },
)


def _argument_ports(raw_args: Iterable[Tuple[str, Dict[str, Any], bool]]) -> Tuple[PipelinePortSpec, ...]:
    return tuple(
        arg_in(name, schema, required=required)
        for name, schema, required in raw_args
    )


def _memory_tool_spec(definition: Dict[str, Any]) -> PipelineNodeSpec:
    name = str(definition["name"])
    metadata = {
        "module": MEMORY_MODULE_ID,
        "module_category": "core",
        "tool": name,
        "core_tool": True,
    }
    metadata.update(dict(definition.get("metadata") or {}))

    return PipelineNodeSpec(
        type_id=f"core.tool.{name}",
        title=name,
        kind="tool",
        category_path=CATEGORY_PATH,
        description=str(definition.get("description") or ""),
        inputs=(
            exec_in(),
            prompt_in(),
            *_argument_ports(definition.get("args") or ()),
        ),
        outputs=(exec_out(), arg_result()),
        implemented=True,
        metadata=metadata,
    )


def register_nodes(registry) -> None:
    tool_names = tuple(str(item.get("name") or "") for item in _MEMORY_TOOLS if str(item.get("name") or ""))
    registry.register(
        PipelineNodeSpec(
            type_id="core.module.memory",
            title=MEMORY_MODULE_DISPLAY_NAME,
            kind="module",
            category_path=CATEGORY_PATH,
            description=(
                "Always-on core Memory module. At runtime the model can choose among the memory "
                "and knowledge-base tools exposed by this component."
            ),
            inputs=(exec_in(), prompt_in()),
            outputs=(exec_out(), arg_result()),
            implemented=True,
            metadata={
                "module": MEMORY_MODULE_ID,
                "module_category": "core",
                "core_module": True,
                "required": True,
                "color": MEMORY_MODULE_COLOR,
                "tools": tool_names,
            },
        )
    )
    for definition in _MEMORY_TOOLS:
        spec = _memory_tool_spec(definition)
        metadata = dict(spec.metadata)
        metadata.setdefault("color", MEMORY_MODULE_COLOR)
        registry.register(
            PipelineNodeSpec(
                type_id=spec.type_id,
                title=spec.title,
                kind=spec.kind,
                category_path=spec.category_path,
                description=spec.description,
                inputs=spec.inputs,
                outputs=spec.outputs,
                implemented=spec.implemented,
                metadata=metadata,
            )
        )
