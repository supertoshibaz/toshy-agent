"""Rich text input overlay for Paint's persistent vector text objects."""
from ui.styles.binding import apply_style
from ui.styles.binding import watch_paint
from ui.themes.manager import theme_manager
from ui.icons import set_icon
from ui.i18n import tr, bind_caption, localize, set_text

import copy
from PySide6.QtCore import Qt,QSignalBlocker,QPoint,QEvent
from PySide6.QtGui import QColor,QFont,QPainter,QPen,QTextCharFormat,QTextCursor
from PySide6.QtWidgets import QFrame,QVBoxLayout,QHBoxLayout,QTextEdit,QToolButton,QFontComboBox,QSpinBox,QColorDialog,QLabel,QApplication,QWidget,QSizePolicy
from shiboken6 import isValid


class TextPanelHandle(QToolButton):
    """Единственная область перетаскивания панели форматирования."""
    def __init__(self, panel):
        super().__init__(panel)
        watch_paint(self)
        self.panel = panel
        self._drag = None
        self.setFixedHeight(18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.SizeAllCursor)
        set_text(self, 'ui.text_editor.move_panel_aff0', setter='setToolTip')

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QPen(theme_manager.color('text.secondary'), 1))
        x, y = self.width() // 2, self.height() // 2
        for dy in (-4, 0, 4):
            painter.drawLine(x - 7, y + dy, x + 7, y + dy)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.panel.pos()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            point = event.globalPosition().toPoint() - self._drag
            self.panel.move(QPoint(max(0,min(point.x(),self.panel.canvas.width()-self.panel.width())),
                                  max(0,min(point.y(),self.panel.canvas.height()-self.panel.height()))))
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None
        event.accept()


class PaintTextEditor(QFrame):
    def __init__(self,canvas,item,apply,cancel):
        super().__init__(canvas)
        self.canvas = canvas
        self._disposed = False
        self._choosing_color = False
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        self.item=copy.deepcopy(item);self.setObjectName('PaintTextEditor')
        apply_style(self, 'QFrame#PaintTextEditor {background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;}')
        layout=QVBoxLayout(self)
        layout.setContentsMargins(6,4,6,6)
        layout.setSpacing(3)
        self.handle = TextPanelHandle(self)
        layout.addWidget(self.handle)
        row=QHBoxLayout();layout.addLayout(row)
        row.setSpacing(3)
        self.family=QFontComboBox(self);self.family.setFixedWidth(135);row.addWidget(self.family)
        self.size_spin=QSpinBox(self);self.size_spin.setRange(6,512);self.size_spin.setValue(24);self.size_spin.setFixedWidth(66);bind_caption(self.size_spin, ' pt', setter='setSuffix');row.addWidget(self.size_spin)
        self.size_spin.setKeyboardTracking(False)
        self.edit=QTextEdit(self);apply_style(self.edit, 'QTextEdit {background:@text.primary@;color:#111;}')
        font=QFont(item.get('font','Sans Serif'));font.setPointSize(24);self.edit.setFont(font)
        self.edit.document().setDocumentMargin(0)
        self.edit.setHtml(item.get('html',''))
        self.edit.hide()
        self.edit.setLineWrapMode(QTextEdit.FixedPixelWidth)
        self.edit.setLineWrapColumnOrWidth(round(item.get('width',420)))
        if not item.get('html') and hasattr(canvas,'_text_default_char'):
            self.edit.setCurrentCharFormat(QTextCharFormat(canvas._text_default_char))
            cursor=self.edit.textCursor();cursor.setBlockFormat(canvas._text_default_block)
            self.edit.setTextCursor(cursor)
        if not item.get('html'):
            # Начальный цвет берём из инструмента, даже если раньше редактировали другой текст.
            self.edit.setTextColor(QColor(item.get('color','#000000')))
        self.family.currentFontChanged.connect(lambda f:self.merge(fontFamily=f.family()))
        self.size_spin.valueChanged.connect(lambda value:self.merge(fontPointSize=value))
        self._style_buttons = {}
        for label,property,value in [('B','fontWeight',int(QFont.Bold)),('I','fontItalic',True),('U','fontUnderline',True),('S','fontStrikeOut',True)]:
            b=QToolButton(self);set_icon(b, {'B':'bold','I':'italic','U':'underline','S':'strikethrough'}[label], size=18, square=True);b.setCheckable(True);self._style_buttons[property]=b
            b.toggled.connect(lambda checked,p=property,v=value:self.merge(**{p:v if checked else int(QFont.Normal) if p=='fontWeight' else False}));row.addWidget(b)
        color=QToolButton(self);set_text(color, 'ui.text_editor.color_6b73');color.clicked.connect(self.color);row.addWidget(color);self.color_button=color
        second=QHBoxLayout();layout.addLayout(second)
        second.setSpacing(3)
        for label,alignment in [('Left',Qt.AlignLeft),('Center',Qt.AlignHCenter),('Right',Qt.AlignRight)]:
            b=QToolButton(self);set_icon(b, {'Left':'align-left','Center':'align-center','Right':'align-right'}[label], size=18, square=True);b.clicked.connect(lambda _,a=alignment:self.align(a));second.addWidget(b)
        second.addWidget(localize(QLabel('',self), 'ui.text_editor.x_4b68'))
        self.width_spin=QSpinBox(self);self.width_spin.setRange(16,32768);self.width_spin.setValue(round(item.get('width',420)));bind_caption(self.width_spin, ' px', setter='setSuffix');self.width_spin.setFixedWidth(83);second.addWidget(self.width_spin)
        set_text(self.width_spin, 'ui.text_editor.text_frame_width_wrap_lines_without_b834', setter='setToolTip')
        self.width_spin.setKeyboardTracking(False)
        self.width_spin.valueChanged.connect(self.change_width)
        second.addWidget(localize(QLabel('',self), 'ui.text_editor.y_18f5'))
        self.height_spin=QSpinBox(self);self.height_spin.setRange(1,32768);self.height_spin.setValue(max(1,round(item.get('height',0))));bind_caption(self.height_spin, ' px', setter='setSuffix');self.height_spin.setFixedWidth(83);second.addWidget(self.height_spin)
        set_text(self.height_spin, 'ui.text_editor.text_frame_height_e669', setter='setToolTip')
        self.height_spin.setKeyboardTracking(False)
        self.height_spin.valueChanged.connect(self.change_height)
        second.addStretch()
        actions=QHBoxLayout();actions.setSpacing(3);layout.addLayout(actions)
        for label,callback in [(tr('common.apply'),apply),(tr('common.cancel'),cancel)]:
            b=QToolButton(self);bind_caption(b, label);b.clicked.connect(callback);actions.addWidget(b)
        actions.addStretch()
        self.adjustSize()
        from .text_canvas import CanvasTextView
        self.view = CanvasTextView(self)
        self.view.item.setTextCursor(self.edit.textCursor())
        self.edit.currentCharFormatChanged.connect(self.sync_controls)
        self.sync_controls()
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if self._disposed or self._choosing_color:
            return False
        if event.type() == QEvent.MouseButtonPress and isinstance(watched, QWidget):
            if (self.canvas._text_box is self and watched not in (self,self.view)
                    and not self.isAncestorOf(watched) and not self.view.isAncestorOf(watched)):
                self.canvas.commit_text()
        return super().eventFilter(watched, event)

    def sync_cursor(self):
        self.edit.setTextCursor(self.view.item.textCursor())
        self.sync_controls()

    def focus_canvas(self):
        if not self._alive():
            return
        self.view.item.setTextCursor(self.edit.textCursor())
        self.view.setFocus()
        self.view.item.setFocus()

    def align(self, alignment):
        self.edit.setAlignment(alignment)
        self.focus_canvas()

    def change_width(self, width):
        self.resize_frame(width, self.view.item.boundingRect().height())

    def change_height(self, height):
        self.resize_frame(self.view.item.textWidth(), height)

    def resize_frame(self, width, height):
        cursor = self.view.item.textCursor()
        fmt = QTextCharFormat(self.edit.currentCharFormat())
        self.view.item.prepareGeometryChange()
        self.edit.setLineWrapColumnOrWidth(round(width))
        self.view.item.setTextWidth(width)
        height = max(float(height), self.view.item.document().size().height())
        self.item.update(width=float(width), height=height)
        self.edit.setTextCursor(cursor)
        if not cursor.hasSelection():
            self.edit.setCurrentCharFormat(fmt)
        self.focus_canvas()
        self.view.item.update()
        self.view.viewport().update()
        self.sync_frame_controls()
        return width, height

    def sync_frame_controls(self):
        with QSignalBlocker(self.width_spin):
            self.width_spin.setValue(round(self.view.item.textWidth()))
        with QSignalBlocker(self.height_spin):
            self.height_spin.setValue(max(1,round(self.view.item.boundingRect().height())))

    def dispose(self):
        self._disposed = True
        QApplication.instance().removeEventFilter(self)
        self.canvas.camera_changed.disconnect(self.view.sync_camera)
        self.view.hide()
        self.view.deleteLater()

    def _alive(self):
        return (not self._disposed and isValid(self) and isValid(self.edit)
                and isValid(self.view))

    def mousePressEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def contextMenuEvent(self, event):
        event.accept()

    def sync_controls(self, *_):
        fmt = self.edit.currentCharFormat()
        font = self.edit.currentFont()
        with QSignalBlocker(self.family):
            self.family.setCurrentFont(font)
        with QSignalBlocker(self.size_spin):
            self.size_spin.setValue(round(font.pointSizeF()) if font.pointSizeF() > 0 else 24)
        values = dict(fontWeight=fmt.fontWeight() >= int(QFont.DemiBold),
                      fontItalic=fmt.fontItalic(), fontUnderline=fmt.fontUnderline(), fontStrikeOut=fmt.fontStrikeOut())
        for key, button in self._style_buttons.items():
            with QSignalBlocker(button):button.setChecked(values[key])
        self.color_button.setStyleSheet('border-bottom:3px solid '+self.edit.textColor().name()+';')
        self.sync_frame_controls()
        self.view.viewport().update()


    def merge(self,**values):
        fmt=QTextCharFormat()
        for key,value in values.items():getattr(fmt,'set'+key[0].upper()+key[1:])(value)
        self.edit.mergeCurrentCharFormat(fmt);self.focus_canvas()

    def color(self):
        if not self._alive():
            return
        self._choosing_color = True
        try:
            color=QColorDialog.getColor(self.edit.textColor(),self,'Text color',
                                       QColorDialog.ShowAlphaChannel | QColorDialog.DontUseNativeDialog)
        finally:
            self._choosing_color = False
        if not self._alive():
            return
        if color.isValid():
            self.apply_color(color)
        self.focus_canvas()

    def apply_color(self, color):
        if not self._alive():
            return
        original = self.view.item.textCursor()
        cursor = QTextCursor(original)
        # Без выделения окрашиваем весь объект; с выделением — только выбранный фрагмент.
        if not cursor.hasSelection():
            cursor.select(QTextCursor.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        cursor.mergeCharFormat(fmt)
        self.edit.setTextCursor(original)
        self.edit.mergeCurrentCharFormat(fmt)
        self.focus_canvas()
        self.sync_controls()

    def result(self):
        return dict(self.item,html=self.edit.toHtml(),width=self.view.item.textWidth())
