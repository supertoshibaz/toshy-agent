"""Shared frameless application dialogs. Native file selectors are not involved."""
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QMouseEvent, QPainter, QColor
from PySide6.QtWidgets import (QDialog as QtDialog, QMessageBox as QtMessageBox,
    QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton,
    QLineEdit, QComboBox, QDialogButtonBox, QPlainTextEdit, QSizePolicy)
from ui.i18n import tr, bind_source, bind_caption, i18n
from ui.icons import set_icon, set_label_icon


def equal_buttons(buttons):
    buttons = [b for b in buttons if b is not None and not b.property('iconOnly')]
    if not buttons:
        return
    width = max(b.sizeHint().width() for b in buttons)
    height = max(b.sizeHint().height() for b in buttons)
    for button in buttons:
        button.setFixedSize(max(76, width), height)


class _TitleBar(QFrame):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)


class _DialogBackdrop(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(host.rect())
        host.installEventFilter(self)
        self.show()
        self.raise_()

    def eventFilter(self, watched, event):
        if watched is self.host and event.type() == QEvent.Resize:
            self.setGeometry(self.host.rect())
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 65))
        painter.end()


class Dialog(QtDialog):
    """Keeps the existing content layout; adds only shared window chrome."""
    def __init__(self, parent=None, flags=Qt.WindowFlags(), *, resizable=False):
        super().__init__(parent, flags)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setSizeGripEnabled(False)
        self._chrome_ready = False
        self._backdrop = None
        self._resizable = resizable
        self._fitting = False
        self._dialog_title = None
        self._equalize_pending = False
        i18n.language_changed.connect(self._language_changed)

    def _equalize_actions(self):
        groups = {}
        for button in self.findChildren(QPushButton):
            if button.text() and not button.property('iconOnly'):
                groups.setdefault(button.parentWidget(), []).append(button)
        for buttons in groups.values():
            if 1 < len(buttons) <= 5:
                equal_buttons(buttons)

    def _language_changed(self, *_):
        self._equalize_actions()
        for box in self.findChildren(QDialogButtonBox):
            equal_buttons(box.buttons())
        if self.isVisible():
            QTimer.singleShot(0, self._fit_contents)

    def _install_chrome(self):
        if self._chrome_ready:
            return
        self._chrome_ready = True
        body = QWidget(self)
        body.setObjectName('ToshyDialogContent')
        previous = self.layout()
        if previous is not None:
            # QWidget.setLayout transfers ownership from the former widget.
            body.setLayout(previous)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        surface = QFrame(self)
        surface.setObjectName('ToshyDialogSurface')
        outer.addWidget(surface)
        content = QVBoxLayout(surface)
        content.setContentsMargins(12, 8, 12, 12)
        content.setSpacing(8)
        titlebar = _TitleBar(surface)
        titlebar.setObjectName('ToshyDialogTitleBar')
        row = QHBoxLayout(titlebar)
        row.setContentsMargins(2, 0, 0, 0)
        self._dialog_title = QLabel(titlebar)
        self._dialog_title.setObjectName('ToshyDialogTitle')
        self._dialog_title.setText(self.windowTitle())
        self.windowTitleChanged.connect(self._dialog_title.setText)
        row.addWidget(self._dialog_title, 1)
        close = QToolButton(titlebar)
        close.setFixedSize(26, 26)
        bind_source(close, 'Close', setter='setToolTip')
        set_icon(close, 'close', size=16)
        close.clicked.connect(self.reject)
        row.addWidget(close)
        content.addWidget(titlebar)
        content.addWidget(body)

    def _fit_contents(self):
        if self._resizable or self._fitting:
            return
        self._fitting = True
        try:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.adjustSize()
            available = self.screen().availableGeometry().size()
            hint = self.sizeHint()
            width = min(max(280, hint.width()), max(280, available.width() - 48))
            height = min(hint.height(), max(180, available.height() - 48))
            self.setFixedSize(width, height)
        finally:
            self._fitting = False

    def showEvent(self, event):
        parent = self.parentWidget()
        if self._backdrop is None and parent is not None and parent.window() is not self:
            self._backdrop = _DialogBackdrop(parent.window())
            self.destroyed.connect(self._backdrop.deleteLater)
        self._install_chrome()
        for box in self.findChildren(QDialogButtonBox):
            for standard, label in MessageBox._labels.items():
                button = box.button(QDialogButtonBox.StandardButton(int(standard.value)))
                if button is not None and not button.property('toshyStandardCaption'):
                    bind_source(button, label)
                    button.setProperty('toshyStandardCaption', True)
        self._equalize_actions()
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_contents)

    def hideEvent(self, event):
        if self._backdrop is not None:
            self._backdrop.hide()
            self._backdrop.deleteLater()
            self._backdrop = None
        super().hideEvent(event)


class ToolDialog(Dialog):
    def __init__(self, parent=None, flags=Qt.WindowFlags()):
        super().__init__(parent, flags, resizable=True)
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        # Existing independent tools retain resize support through Qt's window
        # system API, including Wayland. Small standard dialogs stay fixed.
        pos = event.position()
        edges = Qt.Edges()
        if pos.x() < 6: edges |= Qt.LeftEdge
        if pos.x() > self.width() - 6: edges |= Qt.RightEdge
        if pos.y() < 6: edges |= Qt.TopEdge
        if pos.y() > self.height() - 6: edges |= Qt.BottomEdge
        handle = self.windowHandle()
        if edges and event.button() == Qt.LeftButton and handle is not None:
            handle.startSystemResize(edges)
            event.accept()
            return
        super().mousePressEvent(event)


class MessageBox(Dialog):
    StandardButton = QtMessageBox.StandardButton
    ButtonRole = QtMessageBox.ButtonRole
    Icon = QtMessageBox.Icon
    NoButton = QtMessageBox.NoButton
    Ok, Cancel, Yes, No = QtMessageBox.Ok, QtMessageBox.Cancel, QtMessageBox.Yes, QtMessageBox.No
    Save, Discard, Close = QtMessageBox.Save, QtMessageBox.Discard, QtMessageBox.Close
    Apply, Reset, Retry, Abort, Ignore = QtMessageBox.Apply, QtMessageBox.Reset, QtMessageBox.Retry, QtMessageBox.Abort, QtMessageBox.Ignore
    AcceptRole, RejectRole, DestructiveRole = QtMessageBox.AcceptRole, QtMessageBox.RejectRole, QtMessageBox.DestructiveRole
    ActionRole, YesRole, NoRole = QtMessageBox.ActionRole, QtMessageBox.YesRole, QtMessageBox.NoRole
    Warning, Critical, Information, Question = QtMessageBox.Warning, QtMessageBox.Critical, QtMessageBox.Information, QtMessageBox.Question
    _labels = {Ok:'OK', Cancel:'Cancel', Yes:'Yes', No:'No', Save:'Save', Discard:'Discard',
               Close:'Close', Apply:'Apply', Reset:'Reset', Retry:'Retry', Abort:'Abort', Ignore:'Ignore'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clicked = None
        self._escape = None
        self._buttons = {}
        self._roles = {}
        self._default = None
        self._check_box = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        self._message = QLabel(self)
        self._message.setWordWrap(True)
        self._message.setTextFormat(Qt.PlainText)
        self._message.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self._message.setMinimumWidth(260)
        self._message.setMaximumWidth(560)
        message_row = QHBoxLayout()
        message_row.setSpacing(8)
        self._severity = QLabel(self)
        self._severity.setFixedSize(20, 20)
        self._severity.hide()
        message_row.addWidget(self._severity, 0, Qt.AlignTop)
        message_row.addWidget(self._message, 1)
        layout.addLayout(message_row)
        self._informative = QLabel(self)
        self._informative.setWordWrap(True)
        self._informative.setMaximumWidth(560)
        self._informative.setTextFormat(Qt.PlainText)
        self._informative.hide()
        layout.addWidget(self._informative)
        self._box = QDialogButtonBox(self)
        self._box.setCenterButtons(False)
        layout.addWidget(self._box)
        self._box.clicked.connect(self._finish_button)
        i18n.language_changed.connect(self._resize_buttons)

    def setIcon(self, icon):
        self.setProperty('messageKind', str(icon))
        name = {self.Warning:'warning', self.Critical:'error', self.Information:'info', self.Question:'help'}.get(icon)
        if name:
            set_label_icon(self._severity, name, size=18)
        self._severity.setVisible(bool(name))

    def setText(self, text):
        bind_caption(self._message, text)

    def text(self):
        return self._message.text()

    def setInformativeText(self, text):
        bind_caption(self._informative, text)
        self._informative.setVisible(bool(text))

    def setTextFormat(self, value):
        self._message.setTextFormat(value)

    def setCheckBox(self, checkbox):
        if self._check_box is not None:
            self._check_box.hide()
            self._check_box.deleteLater()
        self._check_box = checkbox
        if checkbox is not None:
            host = self._box.parentWidget()
            checkbox.setParent(host)
            layout = host.layout()
            layout.insertWidget(max(0, layout.indexOf(self._box)), checkbox)

    def checkBox(self):
        return self._check_box

    def addButton(self, value, role=None):
        if isinstance(value, str):
            button = self._box.addButton(value, QDialogButtonBox.ButtonRole(int(role.value)))
            bind_caption(button, value)
            standard = self.NoButton
        else:
            standard = value
            button = self._box.addButton(QDialogButtonBox.StandardButton(int(value.value)))
            bind_source(button, self._labels.get(value, button.text()))
            role = QtMessageBox.ButtonRole(int(self._box.buttonRole(button).value))
        self._buttons[button] = standard
        self._roles[button] = role
        if role in (self.RejectRole, self.NoRole): self._escape = button
        self._resize_buttons()
        return button

    def setStandardButtons(self, values):
        for button in list(self._buttons):
            self._box.removeButton(button)
            button.deleteLater()
        self._buttons.clear()
        self._roles.clear()
        for standard in self._labels:
            if values & standard: self.addButton(standard)

    def setDefaultButton(self, value):
        button = value if isinstance(value, QPushButton) else self.button(value)
        if button is not None:
            button.setDefault(True)
            button.setFocus()
            self._default = button

    def setEscapeButton(self, value):
        self._escape = value if isinstance(value, QPushButton) else self.button(value)

    def button(self, standard):
        return next((button for button, value in self._buttons.items() if value == standard), None)

    def clickedButton(self):
        return self._clicked

    def standardButton(self, button):
        return self._buttons.get(button, self.NoButton)

    def _resize_buttons(self, *_):
        equal_buttons(list(self._buttons))

    def _finish_button(self, button):
        role = self._roles.get(button)
        if role == self.ActionRole:
            return
        self._clicked = button
        value = self._buttons.get(button, self.NoButton)
        self.done(int(value.value) if value != self.NoButton else int(QtDialog.Accepted))

    def reject(self):
        if self._escape is not None:
            self._clicked = self._escape
            value = self._buttons.get(self._escape, self.Cancel)
            self.done(int(value.value) if value != self.NoButton else int(QtDialog.Rejected))
        else:
            self._clicked = None
            self.done(int(self.Cancel.value))

    @classmethod
    def _show(cls, parent, title, text, buttons=None, defaultButton=None, *, kind=None):
        dialog = cls(parent)
        bind_caption(dialog, title, setter='setWindowTitle')
        dialog.setText(text)
        if kind is not None: dialog.setIcon(kind)
        dialog.setStandardButtons(buttons if buttons is not None else cls.Ok)
        if defaultButton is not None: dialog.setDefaultButton(defaultButton)
        elif dialog._buttons: dialog.setDefaultButton(next(iter(dialog._buttons)))
        result = dialog.exec()
        dialog.deleteLater()
        return cls.StandardButton(result)

    @classmethod
    def question(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(parent, title, text, cls.Yes | cls.No if buttons is None else buttons, defaultButton, kind=cls.Question)

    @classmethod
    def information(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(parent, title, text, buttons, defaultButton, kind=cls.Information)

    @classmethod
    def warning(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(parent, title, text, buttons, defaultButton, kind=cls.Warning)

    @classmethod
    def critical(cls, parent, title, text, buttons=None, defaultButton=None):
        return cls._show(parent, title, text, buttons, defaultButton, kind=cls.Critical)


class InputDialog:
    @staticmethod
    def getText(parent, title, label, echo=QLineEdit.Normal, text='', flags=Qt.WindowFlags(), **kwargs):
        dialog = Dialog(parent, flags)
        bind_caption(dialog, title, setter='setWindowTitle')
        layout = QVBoxLayout(dialog)
        heading = bind_caption(QLabel(dialog), label)
        layout.addWidget(heading)
        edit = QLineEdit(str(text), dialog)
        edit.setEchoMode(echo)
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        bind_source(buttons.button(QDialogButtonBox.Ok), 'OK')
        bind_source(buttons.button(QDialogButtonBox.Cancel), 'Cancel')
        equal_buttons(buttons.buttons())
        edit.selectAll()
        edit.setFocus()
        accepted = dialog.exec() == QtDialog.Accepted
        value = edit.text()
        dialog.deleteLater()
        return value, accepted

    @staticmethod
    def getItem(parent, title, label, items, current=0, editable=True, flags=Qt.WindowFlags(), **kwargs):
        dialog = Dialog(parent, flags)
        bind_caption(dialog, title, setter='setWindowTitle')
        layout = QVBoxLayout(dialog)
        layout.addWidget(bind_caption(QLabel(dialog), label))
        combo = QComboBox(dialog)
        combo.addItems(items)
        combo.setCurrentIndex(current)
        combo.setEditable(editable)
        layout.addWidget(combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        bind_source(buttons.button(QDialogButtonBox.Ok), 'OK')
        bind_source(buttons.button(QDialogButtonBox.Cancel), 'Cancel')
        equal_buttons(buttons.buttons())
        accepted = dialog.exec() == QtDialog.Accepted
        value = combo.currentText()
        dialog.deleteLater()
        return value, accepted
