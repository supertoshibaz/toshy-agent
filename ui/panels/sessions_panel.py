from ui.dock_system.dock_panel import DockPanel
from ui.widgets.sessions_widget import SessionsWidget

class SessionsPanel(DockPanel):
    def __init__(self, parent=None):
        super().__init__(
            panel_id="sessions_panel",
            title_key="nav.sessions",
            content_widget=SessionsWidget(),
            parent=parent
        )
