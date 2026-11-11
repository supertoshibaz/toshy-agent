from ui.i18n import bind_caption, tr, i18n
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Signal

from .types import DockSlotPosition
from ui.i18n.manager import LocalizedText


class DockPanel(QWidget):
    """Базовый класс для всех панелей/модулей интерфейса."""
    request_move = Signal(str, DockSlotPosition)  # panel_id, new_position

    def __init__(self, panel_id: str, title_key: str, content_widget: QWidget = None, parent=None):
        super().__init__(parent)
        self.setProperty("toshyDockPanel", True)
        self.panel_id = panel_id
        self.title_key = title_key
        self.current_position = DockSlotPosition.LEFT_TOP

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._placeholder = content_widget is None
        if content_widget is not None:
            self.content = content_widget
        else:
            self.content = bind_caption(QLabel('', self), tr('panel_content_placeholder') + self.get_title())
        layout.addWidget(self.content)

        i18n.language_changed.connect(self._retranslate)

    def get_title(self) -> str:
        # Built-in panels pass resource keys; modules/plugins may supply their
        # own display names, which must remain literal and must not log errors.
        if isinstance(self.title_key, LocalizedText) or self.title_key in i18n._translations.get('en', {}):
            return tr(self.title_key)
        return str(self.title_key)

    def _retranslate(self):
        if self._placeholder:
            bind_caption(self.content, tr('panel_content_placeholder') + self.get_title())
