from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from core.paths import APP_PATHS
from .tool_metadata import tool_metadata

from ..specs import (
    PipelineNodeSpec,
    PipelinePortSpec,
    arg_in,
    arg_result,
    exec_in,
    exec_out,
    prompt_in,
)


def _pretty_name(raw: str) -> str:
    aliases = {
        "mcp": "MCP",
        "api": "API",
        "ui": "UI",
        "llm": "LLM",
    }
    parts = str(raw or "").replace("-", "_").split("_")
    return " ".join(aliases.get(part.casefold(), part.capitalize()) for part in parts if part)


def _read_module_ui_config(module_dir: Path) -> Dict[str, Any]:
    config_path = module_dir / "ui" / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _module_display_name(module_dir: Path) -> str:
    payload = _read_module_ui_config(module_dir)
    value = str(payload.get("display_name") or "").strip()
    if value:
        return value
    return _pretty_name(module_dir.name)


def _module_color(module_dir: Path) -> str:
    payload = _read_module_ui_config(module_dir)
    return str(payload.get("color") or "").strip()


def _discover_tools(module_dir: Path) -> List[Dict[str, Any]]:
    """Read BaseTool metadata without importing or executing module code."""
    init_path = module_dir / "__init__.py"
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    trees = [tree]
    for declaration in tree.body:
        if isinstance(declaration, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PIPELINE_TOOL_SOURCES' for t in declaration.targets):
            try:
                sources = ast.literal_eval(declaration.value)
                for name in sources:
                    path = (module_dir / name).resolve()
                    if path.is_relative_to(module_dir.resolve()) and path.suffix == '.py':
                        trees.append(ast.parse(path.read_text(encoding='utf-8')))
            except (OSError, ValueError, TypeError, SyntaxError):
                pass
    tools: List[Dict[str, Any]] = []
    seen = set()
    classes = {node.name: node for source in trees for node in source.body if isinstance(node, ast.ClassDef)}
    for class_node in (node for source in trees for node in source.body if isinstance(node, ast.ClassDef)):
        title = ''
        library_hidden = False
        for declaration in class_node.body:
            if isinstance(declaration, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'pipeline_title' for t in declaration.targets):
                try:
                    value = ast.literal_eval(declaration.value)
                    if isinstance(value, str): title = value.strip()[:120]
                except (ValueError, TypeError):
                    pass
        for flag in ('catalog_hidden', 'pipeline_hidden'):
            for declaration in class_node.body:
                if isinstance(declaration, ast.Assign) and any(isinstance(t, ast.Name) and t.id == flag for t in declaration.targets):
                    try: library_hidden = ast.literal_eval(declaration.value) is True
                    except (ValueError, TypeError): pass
        for metadata in tool_metadata(class_node, classes):
            if metadata['name'] in seen:
                continue
            seen.add(metadata['name'])
            tools.append(dict(metadata, title=title, library_hidden=library_hidden))
    return tools


def _discover_modules(category: str) -> List[Tuple[str, str, str, List[Dict[str, Any]]]]:
    from core.plugins.modules import module_locations
    result = []
    for (_, name), module_dir in sorted(module_locations(category=category).items()):
        result.append(
            (
                name,
                _module_display_name(module_dir),
                _module_color(module_dir),
                _discover_tools(module_dir),
            )
        )
    return result


def _tool_arg_ports(tool: Dict[str, Any]) -> Tuple[PipelinePortSpec, ...]:
    schema = tool.get("args_schema") if isinstance(tool, dict) else {}
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = {
        str(name)
        for name in (schema.get("required") or [])
        if isinstance(name, str)
    }

    ports: List[PipelinePortSpec] = []
    for name, arg_schema in properties.items():
        clean_name = str(name)
        ports.append(
            arg_in(
                clean_name,
                arg_schema if isinstance(arg_schema, dict) else {},
                required=clean_name in required,
            )
        )
    return tuple(ports)


class ModuleNodeProvider:
    provider_id = "modules"

    def collect(self) -> Iterable[PipelineNodeSpec]:
        specs: List[PipelineNodeSpec] = []
        for category, root_label in (
            ("system", "System modules"),
            ("custom", "Custom modules"),
        ):
            for module_name, display_name, module_color, tools in _discover_modules(category):
                specs.append(
                    PipelineNodeSpec(
                        type_id=f"{category}.module.{module_name}",
                        title=display_name,
                        kind="module",
                        category_path=(root_label, display_name),
                        description=(
                            "Executable module node. At runtime the model will receive "
                            "the tools exposed by this module."
                        ),
                        inputs=(exec_in(), prompt_in()),
                        outputs=(exec_out(), arg_result()),
                        implemented=True,
                        metadata={
                            "library_hidden": module_name == "mcp_client",
                            "module": module_name,
                            "module_category": category,
                            "color": module_color,
                            "tools": tuple(
                                str(tool.get("name") or "")
                                for tool in tools
                                if str(tool.get("name") or "")
                            ),
                        },
                    )
                )

                for tool in tools:
                    tool_name = str(tool.get("name") or "").strip()
                    if not tool_name:
                        continue
                    from modules.pipelines.extensions import node_contract
                    type_id = f"{category}.tool.{module_name}.{tool_name}"
                    contract = node_contract(type_id)
                    extra_outputs = tuple(PipelinePortSpec(**port) for port in contract.get('outputs', ()))
                    specs.append(
                        PipelineNodeSpec(
                            type_id=type_id,
                            title=str(tool.get("title") or tool_name),
                            kind="tool",
                            category_path=(root_label, display_name),
                            description=(
                                str(tool.get("description") or "")
                                or f"Tool node from {display_name}."
                            ),
                            inputs=(exec_in(), prompt_in(), *_tool_arg_ports(tool)),
                            outputs=(exec_out(), *extra_outputs, arg_result()),
                            implemented=True,
                            metadata={
                                "module": module_name,
                                "module_category": category,
                                "tool": tool_name,
                                "library_hidden": bool(tool.get("library_hidden")),
                                "color": module_color,
                                "args_schema": dict(tool.get("args_schema") or {}),
                                "pipeline_contract": contract,
                            },
                        )
                    )
        return specs
