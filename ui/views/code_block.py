from __future__ import annotations
from ui.styles.binding import apply_style
from ui.themes.manager import theme_manager
from ui.icons import set_icon, symbol_button
from ui.i18n import bind_caption, tr, set_text


from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QSizePolicy,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
)


class CodeBlockWidget(QFrame):
    """Shared fenced-code card used by chat and read-only Markdown views."""
    presentation_changed = Signal()

    def __init__(self, code_text: str, language: str = "", parent=None, *, font_size=None):
        super().__init__(parent)
        self.setObjectName("ChatCodeBlockCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        theme_manager.theme_changed.connect(self._theme_changed)
        self.code_text = ""
        self.language = ""
        self._code_initialized = False
        self._updating_code = False
        self._font_size = font_size
        self._preview_requested = False
        self.folded = False
        self.svg_preview = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignTop)

        header = QFrame(self)
        header.setObjectName("ChatCodeBlockHeader")
        header.setFixedHeight(30 if font_size else 26)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(10, 0, 8, 0)

        self.lang_label = QLabel(header)
        apply_style(self.lang_label, "color: @text.secondary@; font-family: 'Fira Code', monospace; font-size: 11px; font-weight: bold; background: transparent;")

        self.copy_btn = QToolButton(header)
        self.copy_btn.setObjectName("ChatCodeCopyBtn")
        set_icon(self.copy_btn, 'copy', size=18 if font_size else 16)
        set_text(self.copy_btn, 'ui.console.copy_1ecd', setter='setToolTip')
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy_code)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1500)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_icon)

        h_layout.addWidget(self.lang_label)
        h_layout.addStretch(1)
        self.render_btn = QToolButton(header)
        self.render_btn.setObjectName('ChatCodeCopyBtn')
        set_icon(self.render_btn, 'photo', size=18 if font_size else 16)
        set_text(self.render_btn, 'ui.code_block.render_76ba', setter='setToolTip')
        self.render_btn.setCursor(Qt.PointingHandCursor)
        self.render_btn.clicked.connect(self._toggle_preview)
        self.render_btn.hide()
        h_layout.addWidget(self.render_btn)
        h_layout.addWidget(self.copy_btn)
        self.fold_btn = QToolButton(header)
        self.fold_btn.setObjectName('ChatCodeCopyBtn')
        self.fold_btn.setCursor(Qt.PointingHandCursor)
        self.fold_btn.clicked.connect(lambda: self.set_folded(not self.folded))
        symbol_button(self.fold_btn, '▾')
        set_text(self.fold_btn, 'ui.code_block.collapse_code_block_7205', setter='setToolTip')
        h_layout.addWidget(self.fold_btn)
        self.header = header
        self._button_side = 26 if font_size else 22
        for button in (self.copy_btn,self.render_btn,self.fold_btn):
            button.setFixedSize(self._button_side,self._button_side)
            apply_style(button, 'QToolButton {padding:0;border:0;border-radius:4px;background:transparent;} QToolButton:hover {background:@control.hover@;}')
        layout.addWidget(header)

        self.code_edit = QTextEdit(self)
        self.code_edit.setObjectName("ChatCodeBlockBody")
        self.code_edit.setReadOnly(True)
        self.code_edit.setLineWrapMode(QTextEdit.NoWrap)
        self.code_edit.setUndoRedoEnabled(False)
        self.code_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.code_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        font = QFont("Fira Code", 10)
        font.setStyleHint(QFont.Monospace)
        self.code_edit.setFont(font)
        if font_size:
            self.code_edit.setStyleSheet(f'font-size:{font_size}px;')
            self.code_edit.horizontalScrollBar().rangeChanged.connect(self._resize_code_height)

        layout.addWidget(self.code_edit)
        self.preview_error = QLabel(self)
        self.preview_error.setWordWrap(True)
        self.preview_error.setTextFormat(Qt.PlainText)
        self.preview_error.setStyleSheet('color:#fbbf24; padding:8px;')
        self.preview_error.hide()
        layout.addWidget(self.preview_error)
        self.set_code(code_text, language)

    def _theme_changed(self, *_):
        self.code_edit.viewport().update()

    def set_code(self, code_text: str, language: str | None = None):
        new_text = code_text
        language_changed = language is not None and language.strip() != self.language
        if language is not None:
            new_language = language.strip()
            label = new_language.lower() if new_language else "code"
            if self.lang_label.text() != label:
                bind_caption(self.lang_label, label)
            self.language = new_language
            self.render_btn.setVisible(new_language.casefold() == 'svg')

        if self._code_initialized and self.code_text == new_text and not language_changed:
            return
        previous = self.code_text
        self._updating_code = True
        try:
            # A full reset briefly turns a long document into one empty line.
            # Horizontal scrollbar signals then shrink/regrow the whole card.
            # Append through an independent cursor without moving the user's
            # selection, focus, or horizontal scroll position.
            cursor = QTextCursor(self.code_edit.document())
            cursor.beginEditBlock()
            try:
                if self._code_initialized and new_text.startswith(previous):
                    cursor.movePosition(QTextCursor.End)
                    cursor.insertText(new_text[len(previous):])
                elif self._code_initialized and previous.startswith(new_text):
                    # Closing a Markdown fence removes its structural newline.
                    # Trim only that tail, preserving selections in the code.
                    cursor.setPosition(len(new_text.encode('utf-16-le')) // 2)
                    cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
                    cursor.removeSelectedText()
                else:
                    cursor.select(QTextCursor.Document)
                    cursor.insertText(new_text)
            finally:
                cursor.endEditBlock()
            self.code_text = new_text
            self._code_initialized = True
        finally:
            self._updating_code = False
        self._resize_code_height()
        if self.language.casefold() != 'svg':
            self._preview_requested = False
        self._refresh_preview()

    def _toggle_preview(self):
        self._preview_requested = not self._preview_requested
        self.folded = False
        self._refresh_preview()

    def set_folded(self, folded):
        self.folded = bool(folded)
        self._refresh_preview()

    def _refresh_preview(self):
        self.preview_error.hide()
        shown = False
        if self._preview_requested and not self.folded:
            try:
                if self.svg_preview is None:
                    from ui.views.svg_preview import SvgPreview
                    self.svg_preview = SvgPreview(self)
                    self.layout().addWidget(self.svg_preview)
                self.svg_preview.set_source(self.code_text)
                shown = True
            except (ValueError, ImportError) as error:
                bind_caption(self.preview_error, str(error))
                self.preview_error.show()
        if self.svg_preview is not None:
            self.svg_preview.setVisible(shown)
        self.code_edit.setVisible(not shown and not self.folded)
        if self.folded:
            self.preview_error.hide()
        bind_caption(self.render_btn, tr('ui.caption.1b3a63b288a4') if self._preview_requested else tr('ui.code_block.render_76ba'), setter='setToolTip')
        set_icon(self.fold_btn, ('right' if self.folded else 'down'), size=18 if self._font_size else 16)
        bind_caption(self.fold_btn, tr('ui.caption.588c288d64c7') if self.folded else tr('ui.code_block.collapse_code_block_7205'), setter='setToolTip')
        bind_caption(self.fold_btn, self.fold_btn.toolTip(), setter='setAccessibleName')
        self.presentation_changed.emit()

    def presentation_height(self, width):
        if self.folded:
            return self.header.height()
        if self.svg_preview is not None and not self.svg_preview.isHidden():
            return self.header.height() + min(640, max(100, round(max(1, width - 24) / self.svg_preview._aspect) + 24))
        error = max(24, self.preview_error.heightForWidth(width)) if not self.preview_error.isHidden() else 0
        return self.header.height() + self.code_edit.height() + error

    def _resize_code_height(self, *_args):
        if self._updating_code:
            return
        lines = max(1, self.code_edit.document().blockCount())
        spacing = self.code_edit.fontMetrics().lineSpacing() if self._font_size else 18
        bar = self.code_edit.horizontalScrollBar()
        extra = bar.sizeHint().height() if self._font_size and bar.maximum() > 0 else 0
        height = int(lines * spacing + 16 + extra)
        if self.code_edit.height() != height:
            self.code_edit.setFixedHeight(height)

    def _copy_code(self):
        QGuiApplication.clipboard().setText(self.code_text)
        set_icon(self.copy_btn, 'check', size=18 if self._font_size else 16)
        set_text(self.copy_btn, 'ui.code_block.copied_aeaa', setter='setToolTip')
        self._copy_feedback_timer.start()

    def _reset_copy_icon(self):
        set_icon(self.copy_btn, 'copy', size=18 if self._font_size else 16)
        set_text(self.copy_btn, 'ui.console.copy_1ecd', setter='setToolTip')
