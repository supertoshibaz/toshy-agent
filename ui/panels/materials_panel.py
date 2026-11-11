from ui.i18n import tr
from ui.dock_system.dock_panel import DockPanel
from ui.widgets.materials_widget import MaterialsWidget


class MaterialsPanel(DockPanel):
    def __init__(self, parent=None):
        super().__init__(panel_id='materials_panel', title_key=tr('ui.materials_widget.materials_9de9'),
                         content_widget=MaterialsWidget(), parent=parent)
