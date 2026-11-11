"""Board colour picker using the Sketch wheel and editable saved presets."""
from PySide6.QtCore import Qt, Signal
from ui.settings import QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QToolButton, QPushButton

from ui.i18n import set_text
from ui.icons import set_icon
from ui.styles.binding import apply_style
from ui.themes.manager import theme_manager
from ui.views.paint.toolbox import ColourWheel, DEFAULT_SWATCHES


class ColourPalette(QWidget):
    applied = Signal(object)

    def __init__(self, colour=None, parent=None):
        super().__init__(parent)
        self.settings = QSettings('Toshy', 'Toshy')
        saved = self.settings.value('board/colour_presets', list(DEFAULT_SWATCHES[:7]))
        self.swatches = (list(saved) if isinstance(saved, (list, tuple)) and len(saved) == 7
                         and all(isinstance(c, str) and QColor(c).isValid() for c in saved)
                         else list(DEFAULT_SWATCHES[:7]))
        self.active = None
        self.colour = QColor(colour or self.swatches[0])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.wheel = ColourWheel(self)
        self.wheel.set_colour(self.colour, False)
        self.wheel.changed.connect(self.set_colour)
        layout.addWidget(self.wheel, 0, Qt.AlignCenter)
        self.hex = QLineEdit(self.colour.name(), self)
        self.hex.setMaxLength(7)
        self.hex.setAlignment(Qt.AlignCenter)
        self.hex.editingFinished.connect(self._hex_changed)
        layout.addWidget(self.hex)
        presets = QHBoxLayout()
        presets.setSpacing(3)
        self.buttons = []
        for index in range(7):
            button = QToolButton(self)
            button.setFixedSize(20, 20)
            set_text(button, 'ui.toolbox.colour_v0_4bca', setter='setToolTip', v0=index + 1)
            button.clicked.connect(lambda _checked=False, i=index: self.select(i))
            self.buttons.append(button)
            presets.addWidget(button)
        self.reset_button = set_icon(QToolButton(self), 'refresh', size=16)
        self.reset_button.setFixedSize(20, 20)
        apply_style(self.reset_button, 'QToolButton {padding:0;border:1px solid @border.subtle@;border-radius:3px;background:@background.surface@;} QToolButton:hover {background:@control.hover@;}')
        set_text(self.reset_button, 'ui.paint.reset_colors', setter='setToolTip')
        self.reset_button.clicked.connect(self.reset_presets)
        presets.addWidget(self.reset_button)
        layout.addLayout(presets)
        actions = QHBoxLayout()
        self.default_button = QPushButton(self)
        set_text(self.default_button, 'ui.api_settings.default_fd05')
        self.default_button.clicked.connect(lambda: self.applied.emit(None))
        self.apply_button = QPushButton(self)
        set_text(self.apply_button, 'common.apply')
        self.apply_button.clicked.connect(lambda: self.applied.emit(self.colour.name()))
        actions.addWidget(self.default_button)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        theme_manager.theme_changed.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        for index, button in enumerate(self.buttons):
            border = theme_manager.value('accent.primary' if index == self.active else 'border.subtle')
            button.setStyleSheet(f'QToolButton {{background:{self.swatches[index]};border:2px solid {border};border-radius:3px;padding:0;}}')

    def select(self, index):
        self.active = index
        self.set_colour(QColor(self.swatches[index]))

    def set_colour(self, colour):
        colour = QColor(colour)
        if not colour.isValid():
            return
        self.colour = colour
        self.wheel.set_colour(colour, False)
        self.hex.setText(colour.name())
        if self.active is not None:
            self.swatches[self.active] = colour.name()
            self.settings.setValue('board/colour_presets', self.swatches)
        self.refresh()

    def _hex_changed(self):
        if QColor(self.hex.text()).isValid():
            self.set_colour(QColor(self.hex.text()))
        else:
            self.hex.setText(self.colour.name())

    def reset_presets(self):
        self.swatches = list(DEFAULT_SWATCHES[:7])
        self.settings.setValue('board/colour_presets', self.swatches)
        self.active = None
        self.refresh()
