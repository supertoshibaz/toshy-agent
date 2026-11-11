from ui.i18n import localize

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from ui.dock_system.dock_panel import DockPanel


class TerminalPanelContent(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        try:
            from modules.system.terminal_sandbox.ui.widget import TerminalWidget
            self.terminal = TerminalWidget(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.terminal)
        except ImportError:
            label = localize(QLabel('', self), 'ui.terminal_panel.the_terminal_sandbox_module_is_not_30ad')
            label.setWordWrap(True)
            layout.addWidget(label)


class TerminalPanel(DockPanel):
    def __init__(self, parent=None):
        super().__init__(
            panel_id="terminal_panel",
            title_key="panel_terminal",
            content_widget=TerminalPanelContent(),
            parent=parent
        )
