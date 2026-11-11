from __future__ import annotations

from typing import List

from core.llm_client import LlmClient
from core.registry import BaseModule, BaseTool
from .manager import MemoryManager
from .tools import (
    ForgetThisTool,
    ListKnowledgeBasesTool,
    ListMemoriesTool,
    RememberThisTool,
    SearchKnowledgeBaseTool,
    SearchMemoryTool,
    SearchTempMemoryTool,
)

from .descriptor import MEMORY_MODULE_COLOR, MEMORY_MODULE_DISPLAY_NAME, MEMORY_MODULE_ID


class CoreMemoryModule(BaseModule):
    """Module-shaped facade for the non-optional core memory subsystem.

    Memory storage/session integration and model-facing tools intentionally live
    together under ``core/memory``. Unlike modules/system/* this
    component is always loaded and has no enable/disable lifecycle.
    """

    module_id = MEMORY_MODULE_ID
    display_name = MEMORY_MODULE_DISPLAY_NAME
    color = MEMORY_MODULE_COLOR
    required = True

    def __init__(self, memory: MemoryManager, client: LlmClient):
        self.memory = memory
        self.client = client

    def get_tools(self) -> List[BaseTool]:
        return [
            RememberThisTool(self.memory, self.client),
            ForgetThisTool(self.memory),
            SearchMemoryTool(self.memory, self.client),
            ListMemoriesTool(self.memory),
            SearchTempMemoryTool(self.memory, self.client),
            ListKnowledgeBasesTool(self.memory),
            SearchKnowledgeBaseTool(self.memory, self.client),
        ]
