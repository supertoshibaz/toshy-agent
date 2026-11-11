from __future__ import annotations

import json
import keyword
import os
import pprint
import re
import ast
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from modules.pipelines.dsl import Pipeline, PipelineEdgeDefinition, PipelineNodeDefinition
from modules.pipelines.control import GRAPH_END_TYPE, GRAPH_START_NODE_ID, GRAPH_START_TYPE


PIPELINE_SCRIPT_NAME = "pipeline.py"
PIPELINE_LAYOUT_NAME = "layout.json"
PIPELINE_LAYOUT_VERSION = 1


class PipelineLoadError(RuntimeError):
    pass


def ensure_pipeline_root(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    init_path = path / "__init__.py"
    if not init_path.exists():
        init_path.write_text(
            '"""Persistent pipeline definitions created by Toshy."""\n',
            encoding="utf-8",
        )
    return path


def pipeline_script_path(pipeline_dir: Path) -> Path:
    return Path(pipeline_dir) / PIPELINE_SCRIPT_NAME


def pipeline_layout_path(pipeline_dir: Path) -> Path:
    return Path(pipeline_dir) / PIPELINE_LAYOUT_NAME


def _ensure_graph_start(pipeline: Pipeline) -> bool:
    """Migrate pre-Graph-Start pipeline files without changing their old flow.

    Existing pipelines get the required singleton on load, but it is left
    unconnected so migration cannot silently choose a previous root. New
    pipelines are created with Start -> End already wired.
    """
    starts = [node for node in pipeline.nodes if node.type_id == GRAPH_START_TYPE]
    if len(starts) > 1:
        raise PipelineLoadError("pipeline contains more than one Graph Start node")
    if starts:
        return False
    pipeline.node(GRAPH_START_TYPE, node_id=GRAPH_START_NODE_ID, settings={})
    return True


def remove_graph_inputs(pipeline):
    """Retire the former Start/Nested input interface, retaining ordinary Arg nodes."""
    changed = False
    starts = {n.node_id for n in pipeline.nodes if n.type_id == GRAPH_START_TYPE}
    calls = {n.node_id for n in pipeline.nodes if n.type_id == 'pipeline.call'}
    for node in pipeline.nodes:
        key = 'outputs' if node.node_id in starts else 'inputs' if node.node_id in calls else None
        if key is not None and key in node.settings:
            node.settings.pop(key); changed = True
    edges = [e for e in pipeline.edges if not (
        e.source_id in starts and e.source_port.startswith('arg:start:')
        or e.target_id in calls and e.target_port.startswith('arg:call:in:'))]
    if len(edges) != len(pipeline.edges): pipeline.edges[:] = edges; changed = True
    return changed


def load_pipeline_script(path: Path, *, migrate: bool = True) -> Pipeline:
    path = Path(path)
    if not path.is_file():
        raise PipelineLoadError(f"pipeline script does not exist: {path}")

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        namespace = {}
        candidate = None
        def literal(node):
            return ast.literal_eval(node)
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom):
                if (statement.module != "modules.pipelines.dsl" or statement.level
                        or len(statement.names) != 1 or statement.names[0].name != "Pipeline"
                        or statement.names[0].asname):
                    raise ValueError("разрешён только импорт Pipeline из modules.pipelines.dsl")
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue
            call = statement.value if isinstance(statement, (ast.Assign, ast.Expr)) else None
            if not isinstance(call, ast.Call):
                raise ValueError("разрешены только Pipeline(), pipeline.node() и pipeline.connect()")
            if isinstance(call.func, ast.Name) and call.func.id == "Pipeline":
                if candidate is not None:
                    raise ValueError("допускается ровно один Pipeline")
                candidate = Pipeline(*[literal(x) for x in call.args], **{k.arg: literal(k.value) for k in call.keywords})
                value = candidate
            elif isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
                owner = namespace.get(call.func.value.id)
                if owner is not candidate or candidate is None:
                    raise ValueError("неизвестный объект pipeline")
                kwargs = {k.arg: literal(k.value) for k in call.keywords}
                if call.func.attr == "node":
                    value = candidate.node(*[literal(x) for x in call.args], **kwargs)
                elif call.func.attr == "connect" and not kwargs and len(call.args) == 4:
                    source, sp, target, tp = call.args
                    if not isinstance(source, ast.Name) or not isinstance(target, ast.Name):
                        raise ValueError("connect ожидает переменные нод")
                    value = candidate.connect(namespace[source.id], literal(sp), namespace[target.id], literal(tp))
                else:
                    raise ValueError("неизвестная конструкция DSL")
            else:
                raise ValueError("неизвестный вызов DSL")
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    raise ValueError("ожидается простое присваивание")
                key = statement.targets[0].id
                if key in namespace:
                    raise ValueError("повторное присваивание переменной DSL")
                namespace[key] = value
        if candidate is None or namespace.get("pipeline", namespace.get("PIPELINE")) is not candidate:
            raise ValueError("файл должен объявить pipeline = Pipeline(...)")
    except Exception as exc:
        raise PipelineLoadError(f"Не удалось прочитать {path.name}: {exc}") from exc

    # A script authored by an agent/user may intentionally omit node_id.
    # Pipeline.node() assigns one while building the definition. Older pipeline
    # files are also migrated once to contain the mandatory Graph Start.
    migrated_graph_start = _ensure_graph_start(candidate)
    removed_inputs = remove_graph_inputs(candidate)
    if migrate and (candidate.generated_node_ids or migrated_graph_start or removed_inputs):
        try:
            save_pipeline_script(path, candidate)
        except Exception as exc:
            raise PipelineLoadError(
                f"loaded {path.name}, but failed to persist canonical pipeline data: {exc}"
            ) from exc

    return candidate


def _identifier_base(type_id: str) -> str:
    raw = str(type_id or "").split(".")[-1].casefold()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if not raw:
        raw = "node"
    if raw[0].isdigit():
        raw = f"node_{raw}"
    if keyword.iskeyword(raw):
        raw += "_node"
    return raw


def _node_variable_names(nodes: Iterable[PipelineNodeDefinition]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    used = set()
    counters: Dict[str, int] = {}
    for node in nodes:
        base = _identifier_base(node.type_id)
        counters[base] = counters.get(base, 0) + 1
        candidate = base if counters[base] == 1 else f"{base}_{counters[base]}"
        while candidate in used:
            counters[base] += 1
            candidate = f"{base}_{counters[base]}"
        used.add(candidate)
        result[node.node_id] = candidate
    return result


def render_pipeline_script(pipeline: Pipeline) -> str:
    nodes = sorted(pipeline.nodes, key=lambda node: node.node_id)
    edges = sorted(
        pipeline.edges,
        key=lambda edge: (
            edge.source_id,
            edge.source_port,
            edge.target_id,
            edge.target_port,
        ),
    )
    names = _node_variable_names(nodes)

    lines = [
        "from modules.pipelines.dsl import Pipeline",
        "",
        "",
        f"pipeline = Pipeline({pipeline.name!r})",
    ]

    if nodes:
        lines.append("")

    for index, node in enumerate(nodes):
        variable = names[node.node_id]
        settings_text = pprint.pformat(
            dict(node.settings or {}),
            sort_dicts=True,
            width=88,
            compact=False,
        )
        lines.extend(
            [
                f"{variable} = pipeline.node(",
                f"    {node.type_id!r},",
                f"    node_id={node.node_id!r},",
            ]
        )
        settings_lines = settings_text.splitlines() or ["{}"]
        settings_prefix = "    settings="
        lines.append(settings_prefix + settings_lines[0])
        continuation_prefix = " " * len(settings_prefix)
        for continuation in settings_lines[1:]:
            lines.append(continuation_prefix + continuation)
        lines[-1] += ","
        lines.append(")")
        if index != len(nodes) - 1:
            lines.append("")

    if edges:
        lines.extend(["", ""])
        for index, edge in enumerate(edges):
            source = names.get(edge.source_id)
            target = names.get(edge.target_id)
            if source is None or target is None:
                continue
            lines.extend(
                [
                    "pipeline.connect(",
                    f"    {source}, {edge.source_port!r},",
                    f"    {target}, {edge.target_port!r},",
                    ")",
                ]
            )
            if index != len(edges) - 1:
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def save_pipeline_script(path: Path, pipeline: Pipeline) -> None:
    _atomic_write_text(Path(path), render_pipeline_script(pipeline))


def load_layout(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_layout(path: Path, payload: Mapping[str, Any]) -> None:
    normalized = dict(payload or {})
    normalized["version"] = PIPELINE_LAYOUT_VERSION
    text = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(Path(path), text)


def create_empty_pipeline(pipeline_dir: Path, name: Optional[str] = None) -> Pipeline:
    pipeline_dir = Path(pipeline_dir)
    pipeline_dir.mkdir(parents=True, exist_ok=False)
    pipeline = Pipeline(name or pipeline_dir.name)
    start = pipeline.node(GRAPH_START_TYPE, node_id=GRAPH_START_NODE_ID, settings={})
    end = pipeline.node(GRAPH_END_TYPE, settings={"message": ""})
    pipeline.connect(start, "exec:out", end, "exec:in")
    save_pipeline_script(pipeline_script_path(pipeline_dir), pipeline)
    save_layout(
        pipeline_layout_path(pipeline_dir),
        {
            "nodes": {
                start.node_id: {"x": 0.0, "y": 0.0},
                end.node_id: {"x": 336.0, "y": 0.0},
            },
            "viewport": {"zoom": 1.0, "center": {"x": 168.0, "y": 0.0}},
        },
    )
    return pipeline
