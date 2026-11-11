from ui.dock_system.dock_panel import DockPanel
from ui.widgets.tasks_widget import TasksWidget


class TasksPanel(DockPanel):
    def __init__(self, parent=None):
        super().__init__(
            panel_id="tasks_panel",
            title_key="panel_tasks",
            content_widget=TasksWidget(),
            parent=parent,
        )
