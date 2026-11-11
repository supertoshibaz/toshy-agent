from ui.dock_system.dock_panel import DockPanel
from ui.widgets.mcp_widget import McpWidget


class McpPanelContent(McpWidget):
    def __init__(self, parent=None):
        super().__init__(parent, owner=None, all_groups=True)


class McpPanel(DockPanel):
    def __init__(self, parent=None):
        super().__init__(
            panel_id="mcp_panel",
            title_key="panel_mcp",
            content_widget=McpPanelContent(),
            parent=parent
        )
