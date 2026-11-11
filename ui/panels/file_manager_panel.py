from ui.dock_system.dock_panel import DockPanel
from ui.widgets.file_manager_widget import FileManagerWidget


class FileManagerPanel(DockPanel):
    """Стандартная док-панель файлового проводника."""
    def __init__(self, parent=None):
        super().__init__(
            panel_id="file_manager_panel",
            title_key="panel_file_manager",
            content_widget=FileManagerWidget(),
            parent=parent
        )
