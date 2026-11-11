"""Optional module-owned UI entry point."""
from ui.i18n import tr
try:
    from modules.system.mcp_client.ui.widget import McpWidget, McpClient, last_mcp_catalog
except ImportError:
    from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QPushButton, QLineEdit
    from PySide6.QtCore import Signal
    last_mcp_catalog = {'available': False, 'reason': tr('ui.caption.0399f81e6df1')}
    class McpClient:
        def request(self, *args, **kwargs):
            raise RuntimeError(last_mcp_catalog['reason'])
    class McpWidget(QWidget):
        inventory_changed = Signal(object)
        def __init__(self, parent=None, **kwargs):
            super().__init__(parent)
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel(last_mcp_catalog['reason']))
            self.file_button, self.manual_button = QPushButton(self), QPushButton(self)
            self.reference = QLineEdit(self)
            for widget in (self.file_button, self.manual_button, self.reference):
                widget.hide()
        def set_owner(self, owner):
            pass
        def select_server(self, identity):
            pass
