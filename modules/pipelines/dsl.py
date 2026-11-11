from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .control import GRAPH_START_NODE_ID, GRAPH_START_TYPE


@dataclass
class PipelineNodeDefinition:
    node_id: str
    type_id: str
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineEdgeDefinition:
    source_id: str
    source_port: str
    target_id: str
    target_port: str


class Pipeline:
    """Declarative builder used by ``modules/pipelines/*/pipeline.py`` files.

    Executing a pipeline file builds this object; it does not execute the
    pipeline itself. The runtime and the visual editor can consume the same
    definition later.
    """

    def __init__(self, name: str = ""):
        self.name = str(name or "")
        self.nodes: List[PipelineNodeDefinition] = []
        self.edges: List[PipelineEdgeDefinition] = []
        self._nodes_by_id: Dict[str, PipelineNodeDefinition] = {}
        self._generated_node_ids = False

    def node(
        self,
        type_id: str,
        *,
        node_id: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> PipelineNodeDefinition:
        type_id = str(type_id or "").strip()
        if not type_id:
            raise ValueError("pipeline node type_id must not be empty")

        if type_id == GRAPH_START_TYPE:
            if any(node.type_id == GRAPH_START_TYPE for node in self.nodes):
                raise ValueError("pipeline may contain only one Graph Start node")
            if node_id is None or not str(node_id).strip():
                node_id = GRAPH_START_NODE_ID
                self._generated_node_ids = True
            else:
                node_id = str(node_id).strip()
        elif node_id is None or not str(node_id).strip():
            node_id = uuid.uuid4().hex[:16]
            self._generated_node_ids = True
        else:
            node_id = str(node_id).strip()

        if not node_id:
            raise ValueError("pipeline node_id must not be empty")
        if node_id in self._nodes_by_id:
            raise ValueError(f"duplicate pipeline node_id: {node_id}")

        node = PipelineNodeDefinition(
            node_id=node_id,
            type_id=type_id,
            settings=dict(settings or {}),
        )
        self.nodes.append(node)
        self._nodes_by_id[node_id] = node
        return node

    def connect(
        self,
        source: PipelineNodeDefinition,
        source_port: str,
        target: PipelineNodeDefinition,
        target_port: str,
    ) -> PipelineEdgeDefinition:
        if source.node_id not in self._nodes_by_id:
            raise ValueError("source node does not belong to this pipeline")
        if target.node_id not in self._nodes_by_id:
            raise ValueError("target node does not belong to this pipeline")

        source_port = str(source_port)
        target_port = str(target_port)
        duplicate = any(
            edge.source_id == source.node_id
            and edge.source_port == source_port
            and edge.target_id == target.node_id
            and edge.target_port == target_port
            for edge in self.edges
        )
        if duplicate:
            raise ValueError(
                f"duplicate pipeline edge: {source.node_id}.{source_port} -> "
                f"{target.node_id}.{target_port}"
            )

        # Exec is single-target on the output side, but fan-in is explicitly
        # allowed: any number of Exec edges may converge on the same Exec input.
        # Arg/Prompt inputs still have a single producer.
        if not target_port.startswith("exec:") and any(
            edge.target_id == target.node_id and edge.target_port == target_port
            for edge in self.edges
        ):
            raise ValueError(
                f"pipeline input is already connected: {target.node_id}.{target_port}"
            )
        if source_port.startswith("exec:") and any(
            edge.source_id == source.node_id and edge.source_port == source_port
            for edge in self.edges
        ):
            raise ValueError(
                f"Exec fan-out is disabled: {source.node_id}.{source_port} already has a target"
            )

        edge = PipelineEdgeDefinition(
            source_id=source.node_id,
            source_port=source_port,
            target_id=target.node_id,
            target_port=target_port,
        )
        self.edges.append(edge)
        return edge

    @property
    def generated_node_ids(self) -> bool:
        """Whether this build assigned at least one missing node id.

        Pipeline scripts may omit ids when they are authored by an agent or by
        hand. The first successful load can then canonicalize the script and
        persist those generated ids, making them stable for layout.json and
        subsequent edits.
        """
        return bool(self._generated_node_ids)

    def get_node(self, node_id: str) -> Optional[PipelineNodeDefinition]:
        return self._nodes_by_id.get(str(node_id or ""))
