"""A themed, resize-aware in-window panel shared by management dialogs."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QLabel
from ui.styles.binding import apply_style
from ui.i18n import bind_caption


class OverlayPanel(QWidget):
    def __init__(self, window, title, *, width=800, height=600):
        super().__init__(window)
        self.window = window
        self.target_width, self.target_height = width, height
        self.setAttribute(Qt.WA_StyledBackground)
        self.setObjectName('ManagementOverlay')
        apply_style(self, '''
            QWidget#ManagementOverlay {background:rgba(0,0,0,65);}
            QFrame#ManagementPanel {background:@background.panel@;border:1px solid @border.subtle@;border-radius:8px;}
            QFrame#ManagementPanel QLabel {background:transparent;}
            QPushButton {border:1px solid @border.subtle@;border-radius:5px;padding:5px 10px;}
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.addStretch()
        self.frame = QFrame(self)
        self.frame.setObjectName('ManagementPanel')
        self.content = QVBoxLayout(self.frame)
        self.content.setContentsMargins(16, 16, 16, 16)
        title_label = bind_caption(QLabel('', self.frame), title)
        title_label.setStyleSheet('font-size:15px;font-weight:600;border:none;')
        self.content.addWidget(title_label)
        outer.addWidget(self.frame, 0, Qt.AlignCenter)
        outer.addStretch()
        window.installEventFilter(self)
        self.setFocusPolicy(Qt.StrongFocus)
        self.fit()
        self.show()
        self.raise_()
        self.setFocus()

    def fit(self):
        self.setGeometry(self.window.rect())
        self.frame.setFixedSize(max(1,min(self.target_width,self.width()-32)),
                                max(1,min(self.target_height,self.height()-32)))

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Resize:
            self.fit()
        return False

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(event)

    def dismiss(self):
        self.window.removeEventFilter(self)
        self.hide()
        self.deleteLater()
