from ui.styles.binding import apply_style
from ui.i18n import bind_caption, localize, set_text
from ui.icons import symbol_button, set_icon

from PySide6.QtCore import Qt, Signal, QPoint, QEvent
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QToolButton, QLabel
)


class SearchReplacePopup(QFrame):
    """Small movable search/replace surface shared by editor-like views."""

    search_changed = Signal(str, bool, bool)
    next_requested = Signal(bool)  # backwards
    replace_requested = Signal(str)
    replace_all_requested = Signal(str)
    closed = Signal()

    def __init__(self, parent=None, *, replace_enabled=True):
        super().__init__(parent)
        self.setObjectName("SearchReplacePopup")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(320)
        self.setMaximumWidth(520)
        apply_style(self, '\n            QFrame#SearchReplacePopup {\n                background-color:@background.panel@;\n                border:1px solid @border.subtle@;\n                border-radius:8px;\n            }\n            QLineEdit {\n                color:@text.primary@;\n                background:@background.window@;\n                border:1px solid @border.subtle@;\n                border-radius:5px;\n                padding:5px 7px;\n            }\n            QLineEdit:focus { border-color:@text.disabled@; }\n            QToolButton {\n                color:@text.primary@;\n                background:@background.surface@;\n                border:1px solid @border.subtle@;\n                border-radius:5px;\n                padding:0;\n                min-width:26px;max-width:26px;min-height:26px;max-height:26px;\n            }\n            QToolButton:hover { background:@control.hover@; color:@text.primary@; }\n            QToolButton:checked { background:@control.selected@; border-color:@accent.primary@; color:@text.primary@; }\n            QLabel { color:@text.secondary@; background:transparent; font-size:10px; }\n            QLabel#SearchPopupTitle { color:@text.primary@; font-size:11px; font-weight:600; }\n        ')
        self._drag_global = None
        self._drag_origin = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(9, 7, 9, 8)
        outer.setSpacing(6)

        # A real drag handle. Previously the only draggable strip sat under the
        # search controls, so in practice there was almost nowhere to grab it.
        header = QHBoxLayout()
        header.setContentsMargins(1, 0, 0, 0)
        header.setSpacing(5)
        self.title_label = localize(QLabel('', self), 'ui.search_popup.search_180f')
        self.title_label.setObjectName("SearchPopupTitle")
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header.addWidget(self.title_label, 1)
        self.close_btn = QToolButton(self)
        symbol_button(self.close_btn, "×")
        set_text(self.close_btn, 'ui.top_bar.close_a7a4', setter='setToolTip')
        self.close_btn.setFixedSize(22, 22)
        header.addWidget(self.close_btn)
        outer.addLayout(header)

        row = QHBoxLayout()
        row.setSpacing(5)
        self.find_edit = QLineEdit(self)
        set_text(self.find_edit, 'ui.file_manager_widget.search_cf30', setter='setPlaceholderText')
        row.addWidget(self.find_edit, 1)

        self.prev_btn = QToolButton(self); symbol_button(self.prev_btn, "↑"); set_text(self.prev_btn, 'ui.search_popup.previous_match_bff4', setter='setToolTip')
        self.next_btn = QToolButton(self); symbol_button(self.next_btn, "↓"); set_text(self.next_btn, 'ui.search_popup.next_match_7a5c', setter='setToolTip')
        self.case_btn = QToolButton(self); set_text(self.case_btn, 'ui.search_popup.aa_81ac'); self.case_btn.setCheckable(True); set_text(self.case_btn, 'ui.search_popup.match_case_e169', setter='setToolTip')
        self.word_btn = QToolButton(self); set_text(self.word_btn, 'ui.search_popup.w_fcb5'); self.word_btn.setCheckable(True); set_text(self.word_btn, 'ui.search_popup.whole_words_ce78', setter='setToolTip')
        for b in (self.prev_btn, self.next_btn, self.case_btn, self.word_btn):
            row.addWidget(b)
        outer.addLayout(row)

        self.replace_row = QFrame(self)
        row2 = QHBoxLayout(self.replace_row); row2.setContentsMargins(0,0,0,0); row2.setSpacing(5)
        self.replace_edit = QLineEdit(self); set_text(self.replace_edit, 'ui.search_popup.replace_6447', setter='setPlaceholderText')
        row2.addWidget(self.replace_edit, 1)
        self.replace_btn = QToolButton(self); set_text(self.replace_btn, 'ui.transform_settings.replace_c201')
        self.replace_all_btn = QToolButton(self); set_text(self.replace_all_btn, 'ui.search_popup.all_2158')
        row2.addWidget(self.replace_btn); row2.addWidget(self.replace_all_btn)
        outer.addWidget(self.replace_row)

        footer = QHBoxLayout(); footer.setContentsMargins(1, 0, 1, 0)
        self.status_label = localize(QLabel('', self), 'ui.search_popup.0_matches_95b2')
        footer.addWidget(self.status_label); footer.addStretch(1)
        outer.addLayout(footer)

        self.find_edit.textChanged.connect(self._emit_search)
        self.case_btn.toggled.connect(lambda _v: self._emit_search())
        self.word_btn.toggled.connect(lambda _v: self._emit_search())
        self.find_edit.returnPressed.connect(lambda: self.next_requested.emit(False))
        self.prev_btn.clicked.connect(lambda: self.next_requested.emit(True))
        self.next_btn.clicked.connect(lambda: self.next_requested.emit(False))
        self.replace_btn.clicked.connect(lambda: self.replace_requested.emit(self.replace_edit.text()))
        self.replace_all_btn.clicked.connect(lambda: self.replace_all_requested.emit(self.replace_edit.text()))
        self.close_btn.clicked.connect(self.close_popup)

        for button, icon in ((self.close_btn,'close'), (self.prev_btn,'up'), (self.next_btn,'down'),
                             (self.case_btn,'text-size'), (self.word_btn,'letter-t'),
                             (self.replace_btn,'replace'), (self.replace_all_btn,'list-check')):
            set_icon(button, icon, size=17)
            button.setFixedSize(28, 28)
        set_text(self.replace_btn, 'ui.transform_settings.replace_c201', setter='setToolTip')
        set_text(self.replace_all_btn, 'ui.search_popup.replace_all', setter='setToolTip')
        self.set_replace_enabled(replace_enabled)
        self.hide()

    def set_replace_enabled(self, enabled):
        self.replace_enabled = bool(enabled)
        self.replace_row.setVisible(self.replace_enabled)
        for widget in (self.replace_edit,self.replace_btn,self.replace_all_btn):
            widget.setEnabled(self.replace_enabled)

    def showEvent(self, event):
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def hideEvent(self, event):
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape and self.isVisible():
            self.close_popup()
            return True
        return False

    def _emit_search(self):
        self.search_changed.emit(self.find_edit.text(), self.case_btn.isChecked(), self.word_btn.isChecked())

    def set_status(self, current: int, total: int):
        if total <= 0:
            set_text(self.status_label, 'ui.search_popup.0_matches_95b2')
        else:
            bind_caption(self.status_label, f"{max(1, current + 1)} / {total}")

    def open_at(self, pos: QPoint | None = None):
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            if pos is None:
                pos = QPoint(max(8, parent.width() - self.width() - 18), 36)
            x = max(6, min(pos.x(), parent.width() - self.width() - 6))
            y = max(6, min(pos.y(), parent.height() - self.height() - 6))
            self.move(x, y)
        self.show(); self.raise_(); self.find_edit.setFocus(); self.find_edit.selectAll()
        self._emit_search()

    def close_popup(self):
        if not self.isVisible():
            return
        self.hide(); self.closed.emit()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() <= 31:
            target = self.childAt(event.position().toPoint())
            if target is not self.close_btn:
                self._drag_global = event.globalPosition().toPoint()
                self._drag_origin = self.pos()
                self.raise_()
                event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_global is not None and (event.buttons() & Qt.LeftButton):
            parent = self.parentWidget()
            delta = event.globalPosition().toPoint() - self._drag_global
            target = self._drag_origin + delta
            if parent is not None:
                target.setX(max(0, min(target.x(), parent.width() - self.width())))
                target.setY(max(0, min(target.y(), parent.height() - self.height())))
            self.move(target); event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_global = None; self._drag_origin = None
        super().mouseReleaseEvent(event)
