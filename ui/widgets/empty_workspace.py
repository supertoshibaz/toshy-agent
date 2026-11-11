"""Two centered, equally sized actions on the workspace background."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLayout
from ui.i18n import set_text
from ui.styles.binding import apply_style


class EmptyWorkspace(QWidget):
    def __init__(self, parent=None, *, new_key, create, open_file):
        super().__init__(parent)
        self.setObjectName('EmptyWorkspace')
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.actions = QWidget(self)
        self.actions.setObjectName('EmptyWorkspaceActions')
        actions = QVBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        # The wider translated caption determines both button widths. Qt
        # recomputes this when the language, font or theme changes.
        actions.setSizeConstraint(QLayout.SetFixedSize)
        self.new_button = QPushButton(self.actions)
        set_text(self.new_button, new_key)
        self.new_button.clicked.connect(create)
        self.open_button = QPushButton(self.actions)
        set_text(self.open_button, 'ui.paint_view.open_1f1d')
        self.open_button.clicked.connect(open_file)
        actions.addWidget(self.new_button)
        actions.addWidget(self.open_button)
        layout.addWidget(self.actions, 0, Qt.AlignCenter)
        apply_style(self, 'QWidget#EmptyWorkspace {background:@workspace.background@;border:0;border-radius:0;}'
                    'QWidget#EmptyWorkspaceActions {background:transparent;border:0;border-radius:0;}')
